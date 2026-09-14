"""US universe: NYSE, Nasdaq and NYSE American common stock.

Source: the Nasdaq Trader symbol directory, the official consolidated symbol
files published every trading day:

* ``nasdaqlisted.txt`` -- Nasdaq-listed securities
* ``otherlisted.txt``  -- everything else on the consolidated tape, with an
  exchange code we use to keep only NYSE (``N``) and NYSE American (``A``).
  Arca (``P``), BATS (``Z``) and IEX (``V``) listings are predominantly ETFs
  and are dropped, and OTC never appears in these files at all.

The parsing functions are pure so they can be tested against a captured fixture
without touching the network.
"""

from __future__ import annotations

from .base import (
    UniverseAdapter,
    UniverseEntry,
    UniverseFetchError,
    UniverseResult,
    dedupe_entries,
    http_get,
)
from .filters import classify_security, us_symbol_exclusion
from .symbols import to_yahoo_symbol

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

#: Exchange codes in ``otherlisted.txt`` that this strategy accepts.
KEPT_EXCHANGE_CODES = {"N": "NYSE", "A": "NYSE AMERICAN"}


def _split_records(text: str) -> list[dict[str, str]]:
    """Parse a pipe-delimited Nasdaq Trader file into dicts, dropping the footer."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    header = [h.strip() for h in lines[0].split("|")]
    records = []
    for line in lines[1:]:
        if line.startswith("File Creation Time"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != len(header):
            continue
        records.append(dict(zip(header, parts, strict=True)))
    return records


def parse_nasdaq_listed(text: str) -> tuple[list[UniverseEntry], list[dict]]:
    entries: list[UniverseEntry] = []
    excluded: list[dict] = []
    for row in _split_records(text):
        symbol = row.get("Symbol", "")
        name = row.get("Security Name", "")
        reason = None
        if row.get("Test Issue", "N").upper() == "Y":
            reason = "test_issue"
        elif row.get("ETF", "N").upper() == "Y":
            reason = "etf"
        elif row.get("NextShares", "N").upper() == "Y":
            reason = "etf"
        else:
            reason = us_symbol_exclusion(symbol) or classify_security(name, symbol)
        if reason:
            excluded.append({"exchange_symbol": symbol, "name": name, "reason": reason, "exchange": "NASDAQ"})
            continue
        yahoo = to_yahoo_symbol(symbol, "US", "NASDAQ")
        if not yahoo:
            excluded.append(
                {"exchange_symbol": symbol, "name": name, "reason": "symbol_unmappable", "exchange": "NASDAQ"}
            )
            continue
        entries.append(
            UniverseEntry(
                market="US",
                exchange="NASDAQ",
                exchange_symbol=symbol,
                yahoo_symbol=yahoo,
                name=name,
                source="nasdaqtrader:nasdaqlisted",
            )
        )
    return entries, excluded


def parse_other_listed(text: str) -> tuple[list[UniverseEntry], list[dict]]:
    entries: list[UniverseEntry] = []
    excluded: list[dict] = []
    for row in _split_records(text):
        symbol = row.get("ACT Symbol", "")
        name = row.get("Security Name", "")
        code = (row.get("Exchange", "") or "").upper()
        exchange = KEPT_EXCHANGE_CODES.get(code)
        if exchange is None:
            excluded.append(
                {"exchange_symbol": symbol, "name": name, "reason": f"exchange_not_targeted:{code}", "exchange": code}
            )
            continue
        reason = None
        if row.get("Test Issue", "N").upper() == "Y":
            reason = "test_issue"
        elif row.get("ETF", "N").upper() == "Y":
            reason = "etf"
        else:
            reason = us_symbol_exclusion(symbol) or classify_security(name, symbol)
        if reason:
            excluded.append({"exchange_symbol": symbol, "name": name, "reason": reason, "exchange": exchange})
            continue
        yahoo = to_yahoo_symbol(symbol, "US", exchange)
        if not yahoo:
            excluded.append(
                {"exchange_symbol": symbol, "name": name, "reason": "symbol_unmappable", "exchange": exchange}
            )
            continue
        entries.append(
            UniverseEntry(
                market="US",
                exchange=exchange,
                exchange_symbol=symbol,
                yahoo_symbol=yahoo,
                name=name,
                source="nasdaqtrader:otherlisted",
            )
        )
    return entries, excluded


class USUniverseAdapter(UniverseAdapter):
    market = "US"

    def fetch(self) -> UniverseResult:
        result = UniverseResult(market="US")
        entries: list[UniverseEntry] = []
        errors: list[str] = []

        for url, parser, label in (
            (NASDAQ_LISTED_URL, parse_nasdaq_listed, "nasdaqlisted"),
            (OTHER_LISTED_URL, parse_other_listed, "otherlisted"),
        ):
            try:
                text = http_get(url).decode("utf-8", errors="replace")
                parsed, excluded = parser(text)
                entries.extend(parsed)
                result.excluded.extend(excluded)
                result.sources.append(url)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{label}: {exc}")

        if not entries:
            raise UniverseFetchError(f"US universe sources all failed: {'; '.join(errors)}")
        if errors:
            result.warnings.append("partial US universe: " + "; ".join(errors))

        result.entries = dedupe_entries(entries, result)
        return result

