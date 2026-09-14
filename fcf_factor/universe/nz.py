"""NZ universe: NZX Main Board (NZSX) ordinary equities.

NZX does not publish a machine-readable issuer file, so the Main Board
securities page is parsed for instrument links.  Company names are optional
here -- they are filled in from provider metadata during screening -- which
keeps the parser resilient to NZX's frequent page restyling.
"""

from __future__ import annotations

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

NZX_URLS = (
    "https://www.nzx.com/markets/NZSX/securities",
    "https://www.nzx.com/markets/NZSX",
)

#: ``/instruments/NZSX/AIR`` or ``/companies/AIR`` style links.  Codes are
#: captured loosely (letters and digits) so that debt and fund lines are
#: *recorded* as exclusions rather than vanishing from the parse.
_INSTRUMENT_RE = re.compile(r"/instruments/NZSX/([A-Z][A-Z0-9]{1,7})")
_COMPANY_RE = re.compile(r"/companies/([A-Z][A-Z0-9]{1,7})")
#: Row-level name capture, used opportunistically.
_ROW_RE = re.compile(
    r"/instruments/NZSX/([A-Z][A-Z0-9]{1,7})\"[^>]*>\s*([^<]{0,80}?)\s*<", re.IGNORECASE
)

#: NZX debt and fund instruments use longer codes with numeric suffixes
#: (``ARG010``, ``WKSFA``); ordinary equity codes are 2-4 letters.
_ORDINARY_CODE_RE = re.compile(r"^[A-Z]{2,4}$")


def parse_nzx_page(html: str) -> tuple[list[UniverseEntry], list[dict]]:
    names: dict[str, str] = {}
    for code, name in _ROW_RE.findall(html):
        cleaned = name.strip()
        if cleaned and code.upper() not in names:
            names[code.upper()] = cleaned

    codes = {c.upper() for c in _INSTRUMENT_RE.findall(html)}
    codes |= {c.upper() for c in _COMPANY_RE.findall(html)}

    entries: list[UniverseEntry] = []
    excluded: list[dict] = []
    for code in sorted(codes):
        name = names.get(code, "")
        if not _ORDINARY_CODE_RE.match(code):
            excluded.append(
                {"exchange_symbol": code, "name": name, "reason": "non_ordinary_code", "exchange": "NZX"}
            )
            continue
        reason = classify_security(name, code)
        if reason:
            excluded.append({"exchange_symbol": code, "name": name, "reason": reason, "exchange": "NZX"})
            continue
        yahoo = to_yahoo_symbol(code, "NZ", "NZX")
        if not yahoo:
            excluded.append(
                {"exchange_symbol": code, "name": name, "reason": "symbol_unmappable", "exchange": "NZX"}
            )
            continue
        entries.append(
            UniverseEntry(
                market="NZ",
                exchange="NZX",
                exchange_symbol=code,
                yahoo_symbol=yahoo,
                name=name or None,
                source="nzx:main-board",
            )
        )
    return entries, excluded


class NZUniverseAdapter(UniverseAdapter):
    market = "NZ"

    def fetch(self) -> UniverseResult:
        result = UniverseResult(market="NZ")
        errors: list[str] = []
        for url in NZX_URLS:
            try:
                html = http_get(url).decode("utf-8", errors="replace")
                entries, excluded = parse_nzx_page(html)
                if not entries:
                    raise UniverseFetchError("parsed zero NZX entries")
                result.entries = dedupe_entries(entries, result)
                result.excluded.extend(excluded)
                result.sources.append(url)
                return result
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{url}: {exc}")
        raise UniverseFetchError("NZ universe sources all failed: " + "; ".join(errors))
