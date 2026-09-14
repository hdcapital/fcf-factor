"""CA universe: Toronto Stock Exchange and TSX Venture Exchange.

Source: the TMX company-directory JSON endpoint, queried once per starting
letter.  Each issuer record carries one or more *instruments*; only the ordinary
share line is kept, and the exchange decides the Yahoo suffix (``.TO`` for TSX,
``.V`` for TSXV).
"""

from __future__ import annotations

import json
import string

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

TMX_URL = "https://www.tsx.com/json/company-directory/search/{venue}/{letter}"
VENUES = (("tsx", "TSX"), ("tsxv", "TSXV"))
LETTERS = tuple(string.ascii_uppercase) + ("0-9",)

def _instrument_exclusion(symbol: str) -> str | None:
    """Reject TMX instrument lines that are not ordinary shares.

    An ordinary class line carries exactly one dot (``CCL.B``).  Preferred
    series, warrants and rights use longer tails (``BCE.PR.A``, ``ABC.WT.B``),
    so more than one dot is itself disqualifying.
    """
    raw = symbol.strip().upper()
    if "." not in raw:
        return None
    parts = raw.split(".")
    tails = parts[1:]
    for tail in tails:
        if tail.startswith(("PR", "PF")):
            return "preferred"
        if tail.startswith("WT"):
            return "warrant"
        if tail.startswith("RT"):
            return "right"
        if tail.startswith("UN"):
            return "unit"
        if tail.startswith(("DB", "NT")):
            return "debt"
    if len(tails) > 1:
        return "other"
    tail = tails[0]
    if len(tail) > 1 or not tail.isalpha():
        return "other"
    return None


def parse_tmx_payload(payload: str | bytes, exchange: str) -> tuple[list[UniverseEntry], list[dict]]:
    data = json.loads(payload)
    entries: list[UniverseEntry] = []
    excluded: list[dict] = []
    for issuer in data.get("results", []) or []:
        issuer_name = (issuer.get("name") or "").strip()
        instruments = issuer.get("instruments") or [
            {"symbol": issuer.get("symbol"), "name": issuer_name}
        ]
        for instrument in instruments:
            symbol = (instrument.get("symbol") or "").strip().upper()
            name = (instrument.get("name") or issuer_name).strip()
            if not symbol:
                continue
            reason = _instrument_exclusion(symbol) or classify_security(name, symbol)
            if reason:
                excluded.append(
                    {"exchange_symbol": symbol, "name": name, "reason": reason, "exchange": exchange}
                )
                continue
            yahoo = to_yahoo_symbol(symbol, "CA", exchange)
            if not yahoo:
                excluded.append(
                    {"exchange_symbol": symbol, "name": name, "reason": "symbol_unmappable", "exchange": exchange}
                )
                continue
            entries.append(
                UniverseEntry(
                    market="CA",
                    exchange=exchange,
                    exchange_symbol=symbol,
                    yahoo_symbol=yahoo,
                    name=name or None,
                    source=f"tmx:{exchange.lower()}",
                )
            )
    return entries, excluded


class CAUniverseAdapter(UniverseAdapter):
    market = "CA"

    def fetch(self) -> UniverseResult:
        result = UniverseResult(market="CA")
        entries: list[UniverseEntry] = []
        errors: list[str] = []
        for venue, exchange in VENUES:
            venue_entries = 0
            for letter in LETTERS:
                url = TMX_URL.format(venue=venue, letter=letter)
                try:
                    parsed, excluded = parse_tmx_payload(http_get(url), exchange)
                    entries.extend(parsed)
                    result.excluded.extend(excluded)
                    venue_entries += len(parsed)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{venue}/{letter}: {exc}")
            if venue_entries:
                result.sources.append(TMX_URL.format(venue=venue, letter="A-Z"))
            else:
                errors.append(f"{venue}: zero entries")

        if not entries:
            raise UniverseFetchError("CA universe sources all failed: " + "; ".join(errors[:10]))
        if errors:
            result.warnings.append(f"partial CA universe: {len(errors)} letter requests failed")
        result.entries = dedupe_entries(entries, result)
        return result
