"""AU universe: ASX ordinary equities.

Primary source is the ASX company directory CSV served by ASX's research API;
the legacy ``ASXListedCompanies.csv`` path is kept as a second attempt because
ASX has moved this file more than once.  Both are plain CSV with a header row
containing ``ASX code``, so one tolerant parser handles either.
"""

from __future__ import annotations

import csv
import io

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

ASX_DIRECTORY_URLS = (
    "https://asx.api.markitdigital.com/asx-research/1.0/companies/directory/file"
    "?access_token=83ff96335c2d45a094df02a206a39ff4",
    "https://www.asx.com.au/asx/research/ASXListedCompanies.csv",
)

_CODE_KEYS = ("asx code", "code", "ticker", "symbol")
_NAME_KEYS = ("company name", "name", "company")
_INDUSTRY_KEYS = ("gics industry group", "gics industry", "industry group", "industry", "sector")


def _find_header(rows: list[list[str]]) -> int | None:
    """Locate the header row (ASX prefixes the file with disclaimer lines)."""
    for i, row in enumerate(rows[:15]):
        lowered = [c.strip().lower() for c in row]
        if any(key in lowered for key in _CODE_KEYS) and any(key in lowered for key in _NAME_KEYS):
            return i
    return None


def _pick(row: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        if key in row and row[key] is not None and str(row[key]).strip():
            return str(row[key]).strip()
    return ""


def parse_asx_directory(text: str) -> tuple[list[UniverseEntry], list[dict]]:
    rows = list(csv.reader(io.StringIO(text)))
    header_index = _find_header(rows)
    if header_index is None:
        raise UniverseFetchError("ASX directory: no recognisable header row")

    header = [c.strip().lower() for c in rows[header_index]]
    entries: list[UniverseEntry] = []
    excluded: list[dict] = []
    for raw in rows[header_index + 1 :]:
        if not raw or not any(cell.strip() for cell in raw):
            continue
        row = dict(zip(header, [c.strip() for c in raw], strict=False))
        code = _pick(row, _CODE_KEYS).upper()
        name = _pick(row, _NAME_KEYS)
        industry = _pick(row, _INDUSTRY_KEYS)
        if not code:
            continue
        # ASX ordinary shares use 3-letter codes; longer codes are options,
        # warrants, notes, instalment receipts and similar.
        if len(code) != 3 or not code.isalnum():
            excluded.append(
                {"exchange_symbol": code, "name": name, "reason": "non_ordinary_code", "exchange": "ASX"}
            )
            continue
        reason = classify_security(name, code)
        if reason is None and industry and "not applic" in industry.lower():
            reason = "fund"
        if reason:
            excluded.append({"exchange_symbol": code, "name": name, "reason": reason, "exchange": "ASX"})
            continue
        yahoo = to_yahoo_symbol(code, "AU", "ASX")
        if not yahoo:
            excluded.append(
                {"exchange_symbol": code, "name": name, "reason": "symbol_unmappable", "exchange": "ASX"}
            )
            continue
        entries.append(
            UniverseEntry(
                market="AU",
                exchange="ASX",
                exchange_symbol=code,
                yahoo_symbol=yahoo,
                name=name,
                industry=industry or None,
                source="asx:company-directory",
            )
        )
    return entries, excluded


class AUUniverseAdapter(UniverseAdapter):
    market = "AU"

    def fetch(self) -> UniverseResult:
        result = UniverseResult(market="AU")
        errors: list[str] = []
        for url in ASX_DIRECTORY_URLS:
            try:
                text = http_get(url).decode("utf-8-sig", errors="replace")
                entries, excluded = parse_asx_directory(text)
                if not entries:
                    raise UniverseFetchError("parsed zero ASX entries")
                result.entries = dedupe_entries(entries, result)
                result.excluded.extend(excluded)
                result.sources.append(url)
                return result
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{url}: {exc}")
        raise UniverseFetchError("AU universe sources all failed: " + "; ".join(errors))
