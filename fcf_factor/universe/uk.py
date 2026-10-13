"""UK universe: London Stock Exchange Main Market and AIM.

LSE publishes an issuer list as a spreadsheet.  The exact filename has changed
several times, so a list of candidate URLs is tried in order and the parser
sniffs for the header row rather than assuming a fixed layout.  Rows are kept
only when the ``Market`` column names the Main Market or AIM -- the Professional
Securities Market and the order book for retail bonds are debt venues and are
dropped.

UK tickers need particular care downstream: Yahoo quotes ``.L`` shares in pence
while reporting market cap in pounds.  That is handled in
:mod:`fcf_factor.currency` and :mod:`fcf_factor.factor.quality`, not here.
"""

from __future__ import annotations

import csv
import io
import re

from .base import (
    UniverseAdapter,
    UniverseEntry,
    UniverseFetchError,
    UniverseResult,
    dedupe_entries,
    http_get,
)
from .filters import classify_security
from .symbols import to_yahoo_symbol

LSE_REPORTS_BASE = "https://docs.londonstockexchange.com/sites/default/files/reports/"
#: LSE re-uploads its monthly instrument list as ``Instrument list_<N>.xlsx``
#: with N rising each month; the un-numbered and ``_0`` URLs froze in 2020.
LSE_INSTRUMENT_STEM = "Instrument%20list"
#: Upload number known to exist when this was written (September 2026).  Only a
#: starting point: the newest upload is discovered from here on every run.
LSE_INSTRUMENT_HINT = 84
#: A file whose "As at" date is older than this is rejected as stale.
MAX_SOURCE_AGE_DAYS = 70
#: Consecutive missing numbers that end the search for newer uploads.
DISCOVERY_MISS_LIMIT = 6
#: Kept for the diagnostic probe only; these addresses stopped updating in 2020.
LSE_ISSUER_URLS = (
    f"{LSE_REPORTS_BASE}Instrument%20list_0.xlsx",
    f"{LSE_REPORTS_BASE}Issuer%20list_0.xlsx",
)

_TIDM_KEYS = ("tidm", "mnemonic", "ticker", "symbol", "trading symbol")
_NAME_KEYS = ("issuer name", "company", "company name", "name", "issuer")
_MARKET_KEYS = ("lse market", "market", "mkt", "exchange")
_INSTRUMENT_NAME_KEYS = ("instrument name", "security name", "instrument")
_MIFIR_KEYS = ("mifir identifier code",)
_SEGMENT_KEYS = ("market segment code",)
_LISTING_CATEGORY_KEYS = ("fca listing category",)

#: MiFIR instrument type for ordinary shares.  The LSE instrument list also
#: carries ETFS (exchange-traded funds) and DPRS (depositary receipts).
_ORDINARY_MIFIR = "SHRS"
#: FCA listing categories that are funds or non-equity, by keyword.
#: Covers both the pre-2024 names and the UK Listing Rules categories that
#: replaced them ("Equity shares (shell companies)" and so on).
_REJECTED_CATEGORY = (
    ("investment fund", "investment_company"),
    ("investment compan", "investment_company"),
    ("shell compan", "shell"),
    ("non-equity", "non_ordinary_instrument"),
    ("non-voting", "non_ordinary_instrument"),
    ("secondary listing", "secondary_listing"),
    ("depositary receipt", "depositary_receipt"),
    ("depository receipt", "depositary_receipt"),
    ("gdr", "depositary_receipt"),
    ("debt", "debt"),
    ("misc", "other"),
)
#: Preference-share and other non-ordinary wording in an LSE instrument name.
_NON_ORDINARY_INSTRUMENT_RE = re.compile(
    r"\bprf\b|\bpref\b|\bprefs?\b|\bwts?\b|\bwarrants?\b|\bnts\b|\bbds\b|\bloan\b|\bdeb\b|\bnil pd\b",
    re.IGNORECASE,
)
_INDUSTRY_KEYS = ("icb industry", "industry", "icb super-sector", "sector", "icb sector")

#: Markets this strategy covers.
_ACCEPTED_MARKET_RE = re.compile(r"main\s*market|aim\b|\bhgs\b|international main", re.IGNORECASE)
#: Debt / specialist venues that must never enter the equity universe.
_REJECTED_MARKET_RE = re.compile(
    r"professional securities|retail bond|orb\b|debt|gilt|specialist fund|\bsfs\b"
    r"|closed[- ]end|admission to trading only",
    re.IGNORECASE,
)

#: TIDMs are two to four characters, optionally with a class letter after a
#: dot (``BT.A``) -- or a bare trailing dot, which many large companies have
#: (``BP.``, ``BA.`` for BAE Systems, ``RR.`` for Rolls-Royce).
_TIDM_RE = re.compile(r"^[A-Z0-9]{2,4}(\.[A-Z]?)?$")


def _rows_from_xlsx(payload: bytes) -> list[list[str]]:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    rows: list[list[str]] = []
    for sheet in workbook.worksheets:
        sheet_rows = [
            ["" if cell is None else str(cell).strip() for cell in row]
            for row in sheet.iter_rows(values_only=True)
        ]
        if _find_header(sheet_rows) is not None:
            rows = sheet_rows
            break
        if not rows:
            rows = sheet_rows
    workbook.close()
    return rows


def describe_workbook(payload: bytes, max_rows: int = 8) -> str:
    """Summarise a spreadsheet's sheets and first rows for an error message.

    When an exchange changes its file layout the parser fails, and this text
    in the log is what lets the parser be fixed without guessing.
    """
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        return f"(not a readable workbook: {exc}; first bytes {payload[:60]!r})"
    parts = []
    for sheet in workbook.worksheets:
        shown = 0
        lines = []
        for row in sheet.iter_rows(values_only=True):
            cells = ["" if c is None else str(c).strip() for c in row]
            if not any(cells):
                continue
            lines.append(" | ".join(cells)[:220])
            shown += 1
            if shown >= max_rows:
                break
        parts.append(f"[sheet {sheet.title!r}] " + " // ".join(lines))
    workbook.close()
    return " ;; ".join(parts)


def _rows_from_csv(payload: bytes) -> list[list[str]]:
    text = payload.decode("utf-8-sig", errors="replace")
    return [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(text))]


def _find_header(rows: list[list[str]]) -> int | None:
    for i, row in enumerate(rows[:40]):
        lowered = [c.strip().lower() for c in row]
        if any(k in lowered for k in _TIDM_KEYS) and any(k in lowered for k in _NAME_KEYS):
            return i
    return None


def _pick(row: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if value:
            return str(value).strip()
    return ""


#: Classification columns whose values are summarised by the universe check.
_DIAGNOSTIC_COLUMNS = (
    "lse market",
    "mifir identifier code",
    "mifir identifier description",
    "market segment code",
    "fca listing category",
)


def _column_value_counts(rows: list[list[str]], header_index: int) -> dict:
    from collections import Counter

    header = [c.strip().lower() for c in rows[header_index]]
    out: dict = {}
    for column in _DIAGNOSTIC_COLUMNS:
        if column not in header:
            continue
        position = header.index(column)
        counts = Counter(
            (row[position].strip() if position < len(row) else "") or "(blank)"
            for row in rows[header_index + 1 :]
            if any(str(c).strip() for c in row)
        )
        out[column] = dict(counts.most_common(25))
    return out


def _instrument_rejection(mifir: str, segment: str, category: str, instrument_name: str) -> str | None:
    """Reject non-ordinary lines using the type columns the LSE file provides.

    These are checked before any name-based heuristic, because the file states
    each line's type directly.
    """
    if mifir and mifir != _ORDINARY_MIFIR:
        return {"ETFS": "etf", "DPRS": "depositary_receipt"}.get(mifir, f"instrument_type:{mifir}")
    if segment.startswith("IOB"):
        return "international_order_book"
    for key, reason in _REJECTED_CATEGORY:
        if key in category:
            return reason
    if instrument_name and _NON_ORDINARY_INSTRUMENT_RE.search(instrument_name):
        return "non_ordinary_instrument"
    return None


def parse_lse_rows(rows: list[list[str]]) -> tuple[list[UniverseEntry], list[dict]]:
    header_index = _find_header(rows)
    if header_index is None:
        raise UniverseFetchError("LSE issuer list: no recognisable header row")
    header = [c.strip().lower() for c in rows[header_index]]

    entries: list[UniverseEntry] = []
    excluded: list[dict] = []
    for raw in rows[header_index + 1 :]:
        if not raw or not any(str(cell).strip() for cell in raw):
            continue
        row = dict(zip(header, [str(c).strip() for c in raw], strict=False))
        tidm = _pick(row, _TIDM_KEYS).upper()
        name = _pick(row, _NAME_KEYS)
        market_text = _pick(row, _MARKET_KEYS)
        industry = _pick(row, _INDUSTRY_KEYS)
        instrument_name = _pick(row, _INSTRUMENT_NAME_KEYS)
        mifir = _pick(row, _MIFIR_KEYS).upper()
        segment = _pick(row, _SEGMENT_KEYS).upper()
        category = _pick(row, _LISTING_CATEGORY_KEYS).lower()
        if not tidm:
            continue

        type_reason = _instrument_rejection(mifir, segment, category, instrument_name)
        if type_reason:
            excluded.append({"exchange_symbol": tidm, "name": name, "reason": type_reason, "exchange": "LSE"})
            continue
        if market_text:
            if _REJECTED_MARKET_RE.search(market_text) or not _ACCEPTED_MARKET_RE.search(market_text):
                excluded.append(
                    {
                        "exchange_symbol": tidm,
                        "name": name,
                        "reason": f"market_not_targeted:{market_text[:40]}",
                        "exchange": "LSE",
                    }
                )
                continue
        if not _TIDM_RE.match(tidm):
            excluded.append(
                {"exchange_symbol": tidm, "name": name, "reason": "non_ordinary_code", "exchange": "LSE"}
            )
            continue
        reason = classify_security(name, tidm) or classify_security(instrument_name)
        if reason:
            excluded.append({"exchange_symbol": tidm, "name": name, "reason": reason, "exchange": "LSE"})
            continue
        yahoo = to_yahoo_symbol(tidm, "UK", "LSE")
        if not yahoo:
            excluded.append(
                {"exchange_symbol": tidm, "name": name, "reason": "symbol_unmappable", "exchange": "LSE"}
            )
            continue
        entries.append(
            UniverseEntry(
                market="UK",
                exchange="LSE",
                exchange_symbol=tidm,
                yahoo_symbol=yahoo,
                name=name or None,
                industry=industry or None,
                source="lse:instrument-list",
            )
        )
    return entries, excluded


_MONTHS = {
    m: i
    for i, m in enumerate(
        ("january", "february", "march", "april", "may", "june", "july", "august",
         "september", "october", "november", "december"),
        start=1,
    )
}
_AS_AT_RE = re.compile(r"as at\s+(\d{1,2})\s+([a-z]+)\s+(\d{4})", re.IGNORECASE)


def as_at_date(rows: list[list[str]]):
    """The "As at <day> <Month> <year>" date LSE prints above its tables."""
    from datetime import date

    for row in rows[:12]:
        for cell in row:
            match = _AS_AT_RE.search(str(cell))
            if match and match.group(2).lower() in _MONTHS:
                return date(int(match.group(3)), _MONTHS[match.group(2).lower()], int(match.group(1)))
    return None


def discover_latest_upload(hint: int, exists, miss_limit: int = DISCOVERY_MISS_LIMIT) -> int | None:
    """Highest ``N`` for which ``exists(N)`` is true, searching from ``hint``.

    Walks upwards until ``miss_limit`` consecutive numbers are missing, so a
    gap in the numbering does not stop the search early.  If the hint itself
    has vanished, walks downwards to the nearest upload that still exists.
    """
    best = hint if exists(hint) else None
    if best is None:
        for n in range(hint - 1, max(0, hint - 40), -1):
            if exists(n):
                best = n
                break
    start = best if best is not None else hint
    misses = 0
    n = start + 1
    while misses < miss_limit:
        if exists(n):
            best = n
            misses = 0
        else:
            misses += 1
        n += 1
    return best


def _stored_hint() -> int:
    """Upload number used last time, read from the stored universe metadata."""
    from ..storage import read_json, universe_dir

    meta = read_json(universe_dir("UK") / "latest.json") or {}
    for url in meta.get("sources") or []:
        match = re.search(rf"{LSE_INSTRUMENT_STEM}_(\d+)\.xlsx", str(url))
        if match:
            return max(int(match.group(1)), LSE_INSTRUMENT_HINT)
    return LSE_INSTRUMENT_HINT


class UKUniverseAdapter(UniverseAdapter):
    market = "UK"

    def fetch(self) -> UniverseResult:
        from datetime import date

        import requests

        from .base import USER_AGENT

        def exists(n: int) -> bool:
            url = f"{LSE_REPORTS_BASE}{LSE_INSTRUMENT_STEM}_{n}.xlsx"
            try:
                response = requests.head(
                    url, timeout=20, headers={"User-Agent": USER_AGENT}, allow_redirects=True
                )
            except Exception:  # noqa: BLE001
                return False
            return response.status_code == 200

        number = discover_latest_upload(_stored_hint(), exists)
        if number is None:
            raise UniverseFetchError("no LSE instrument list upload could be found")
        url = f"{LSE_REPORTS_BASE}{LSE_INSTRUMENT_STEM}_{number}.xlsx"
        payload = http_get(url)
        rows = _rows_from_xlsx(payload)

        as_at = as_at_date(rows)
        if as_at is None:
            raise UniverseFetchError(
                f"{url}: no 'As at' date found, so its freshness cannot be checked. "
                f"Layout: {describe_workbook(payload)}"
            )
        age = (date.today() - as_at).days
        if age > MAX_SOURCE_AGE_DAYS:
            raise UniverseFetchError(
                f"{url} is dated {as_at} ({age} days old); refusing a stale universe"
            )

        try:
            entries, excluded = parse_lse_rows(rows)
        except UniverseFetchError as exc:
            raise UniverseFetchError(f"{exc}. Layout: {describe_workbook(payload)}") from exc
        if not entries:
            raise UniverseFetchError(f"{url}: parsed zero ordinary shares")

        result = UniverseResult(market="UK")
        result.entries = dedupe_entries(entries, result)
        result.excluded.extend(excluded)
        result.sources.append(url)
        result.warnings.append(f"LSE instrument list as at {as_at} (upload {number})")
        header_index = _find_header(rows)
        if header_index is not None:
            result.source_columns = [c for c in rows[header_index] if c]
            result.diagnostics = _column_value_counts(rows, header_index)
        return result


def probe_sources(urls: tuple[str, ...] = LSE_ISSUER_URLS) -> list[dict]:
    """Report how current each candidate LSE file is.

    For every URL: HTTP status, Last-Modified, size, the latest date found in
    any date-like column, and the sheet layout.  The latest listing date is the
    tell-tale: a file that stopped being updated years ago still downloads
    cleanly and parses, but silently omits every company listed since.
    """
    import requests

    from .base import USER_AGENT

    out: list[dict] = []
    for url in urls:
        entry: dict = {"url": url}
        try:
            response = requests.get(url, timeout=45, headers={"User-Agent": USER_AGENT})
            entry["status"] = response.status_code
            entry["last_modified"] = response.headers.get("Last-Modified")
            entry["bytes"] = len(response.content)
            if response.ok and url.endswith(".xlsx"):
                rows = _rows_from_xlsx(response.content)
                header_index = _find_header(rows)
                entry["header_found"] = header_index is not None
                entry["layout"] = describe_workbook(response.content, max_rows=5)[:1500]
                entry["latest_dates"] = _latest_dates(rows, header_index)
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)[:200]
        out.append(entry)
    return out


def _latest_dates(rows: list[list[str]], header_index: int | None) -> dict:
    """Latest value in each column whose heading mentions a date."""
    if header_index is None:
        return {}
    header = [c.strip().lower() for c in rows[header_index]]
    latest: dict = {}
    for position, name in enumerate(header):
        if "date" not in name:
            continue
        values = sorted(
            str(row[position])[:10]
            for row in rows[header_index + 1 :]
            if position < len(row) and str(row[position])[:4].isdigit()
        )
        if values:
            latest[name] = values[-1]
    return latest


def scan_numbered_uploads(max_n: int = 160, pause: float = 0.15) -> list[dict]:
    """Find newer copies of the LSE reports.

    The LSE site is built on Drupal, which saves a re-uploaded file with the
    same name as ``name_1.xlsx``, ``name_2.xlsx`` and so on.  The un-numbered
    and ``_0`` URLs stopped changing in 2020, so later monthly reports, if they
    exist, sit at higher numbers.  HEAD requests only, politely paced.
    """
    import time

    import requests

    from .base import USER_AGENT

    base = "https://docs.londonstockexchange.com/sites/default/files/reports/"
    found: list[dict] = []
    for stem in ("Instrument%20list", "Issuer%20list"):
        for n in range(1, max_n + 1):
            url = f"{base}{stem}_{n}.xlsx"
            try:
                response = requests.head(
                    url, timeout=20, headers={"User-Agent": USER_AGENT}, allow_redirects=True
                )
            except Exception:  # noqa: BLE001
                continue
            if response.status_code == 200:
                found.append({"url": url, "last_modified": response.headers.get("Last-Modified")})
            time.sleep(pause)
    return found
