"""Exchange symbol -> Yahoo symbol mapping.

``exchange_symbol`` (what the exchange calls the security) and ``yahoo_symbol``
(what the data provider calls it) are kept as *separate* fields everywhere in
this system, because they disagree often enough to matter:

===========  ===============  ==================
Market       Exchange symbol  Yahoo symbol
===========  ===============  ==================
ASX          ``CBA``          ``CBA.AX``
NZX          ``AIR``          ``AIR.NZ``
LSE          ``BT.A``         ``BT-A.L``
TSX          ``CCL.B``        ``CCL-B.TO``
TSXV         ``ABC``          ``ABC.V``
US           ``BRK.A``        ``BRK-A``
===========  ===============  ==================

Mapping a symbol is never treated as proof that it exists.  The screening stage
asks the provider for metadata and records ``symbol_unresolved`` when nothing
comes back, so a bad suffix shows up as an explicit exclusion rather than a
silent hole.
"""

from __future__ import annotations

import re

#: Yahoo tickers are upper-case alphanumerics with optional ``-`` class markers
#: and an optional exchange suffix.
YAHOO_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{0,14}(\.[A-Z]{1,3})?$")

#: Exchange code -> Yahoo suffix, for markets with more than one venue.
EXCHANGE_SUFFIX = {
    "ASX": ".AX",
    "NZX": ".NZ",
    "LSE": ".L",
    "TSX": ".TO",
    "TSXV": ".V",
    "NYSE": "",
    "NASDAQ": "",
    "NYSE AMERICAN": "",
}

MARKET_DEFAULT_EXCHANGE = {"AU": "ASX", "NZ": "NZX", "UK": "LSE", "CA": "TSX", "US": "NASDAQ"}


def clean_exchange_symbol(symbol: str) -> str:
    """Upper-case, trim and collapse internal whitespace in a raw ticker."""
    return re.sub(r"\s+", " ", (symbol or "").strip()).upper()


def to_yahoo_symbol(exchange_symbol: str, market: str, exchange: str | None = None) -> str | None:
    """Translate an exchange ticker into the Yahoo convention.

    Returns ``None`` when the input cannot produce a plausible Yahoo symbol, so
    callers can record an explicit ``symbol_unmappable`` exclusion instead of
    guessing.
    """
    root = clean_exchange_symbol(exchange_symbol)
    if not root:
        return None

    venue = (exchange or MARKET_DEFAULT_EXCHANGE.get(market.upper(), "")).upper()
    suffix = EXCHANGE_SUFFIX.get(venue)
    if suffix is None:
        return None

    # Class markers: exchanges write ``BT.A`` / ``BT A``; Yahoo writes ``BT-A``.
    root = root.replace(" ", "-").replace(".", "-").replace("/", "-")
    root = re.sub(r"-+", "-", root).strip("-")
    if not root:
        return None

    candidate = f"{root}{suffix}"
    if not YAHOO_SYMBOL_RE.match(candidate):
        return None
    return candidate


def is_plausible_yahoo_symbol(symbol: str) -> bool:
    return bool(symbol) and bool(YAHOO_SYMBOL_RE.match(symbol))
