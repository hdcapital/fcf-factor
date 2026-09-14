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

LSE_ISSUER_URLS = (
    "https://docs.londonstockexchange.com/sites/default/files/reports/Issuer%20list_0.xlsx",
    "https://docs.londonstockexchange.com/sites/default/files/reports/Issuer%20list.xlsx",
    "https://docs.londonstockexchange.com/sites/default/files/reports/List%20of%20all%20companies_0.xlsx",
)

_TIDM_KEYS = ("tidm", "mnemonic", "ticker", "symbol", "trading symbol")
_NAME_KEYS = ("issuer name", "company", "company name", "name", "issuer")
_MARKET_KEYS = ("market", "mkt", "market segment", "exchange")
_INDUSTRY_KEYS = ("icb industry", "industry", "icb super-sector", "sector", "icb sector")

#: Markets this strategy covers.
_ACCEPTED_MARKET_RE = re.compile(r"main\s*market|aim\b|\bhgs\b|international main", re.IGNORECASE)
#: Debt / specialist venues that must never enter the equity universe.
_REJECTED_MARKET_RE = re.compile(
    r"professional securities|retail bond|orb\b|debt|gilt|specialist fund|closed[- ]end", re.IGNORECASE
)

_TIDM_RE = re.compile(r"^[A-Z0-9]{2,4}(\.[A-Z])?$")


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
        if not tidm:
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
        reason = classify_security(name, tidm)
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
                source="lse:issuer-list",
            )
        )
    return entries, excluded


class UKUniverseAdapter(UniverseAdapter):
    market = "UK"

    def fetch(self) -> UniverseResult:
        result = UniverseResult(market="UK")
        errors: list[str] = []
        for url in LSE_ISSUER_URLS:
            try:
                payload = http_get(url)
                rows = _rows_from_xlsx(payload) if url.endswith(".xlsx") else _rows_from_csv(payload)
                entries, excluded = parse_lse_rows(rows)
                if not entries:
                    raise UniverseFetchError("parsed zero LSE entries")
                result.entries = dedupe_entries(entries, result)
                result.excluded.extend(excluded)
                result.sources.append(url)
                return result
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{url}: {exc}")
        raise UniverseFetchError("UK universe sources all failed: " + "; ".join(errors))
