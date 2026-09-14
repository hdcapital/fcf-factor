"""Currency and unit normalisation.

Two separate problems live here and they are easy to confuse:

1. **Minor units.**  Yahoo quotes London-listed shares in *pence* (``GBp`` /
   ``GBX``), not pounds, while it reports the same company's market cap and
   financial statements in *pounds*.  Mixing the two silently produces a 100x
   valuation error, which is exactly the sort of bug this system must never
   ship.  :func:`normalise_quote` converts a quoted price into major units and
   tells the caller whether it had to.

2. **Different currencies.**  A company may list in CAD but report its accounts
   in USD.  Every monetary quantity is therefore converted into USD *before*
   any arithmetic combines it with another quantity.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Currency codes Yahoo uses for minor units, mapped to (major code, divisor).
MINOR_UNIT_CURRENCIES: dict[str, tuple[str, float]] = {
    "GBP2": ("GBP", 100.0),  # seen occasionally on LSE feeds
    "GBX": ("GBP", 100.0),
    "GBPENCE": ("GBP", 100.0),
    "ZAC": ("ZAR", 100.0),
    "ILA": ("ILS", 100.0),
    "ILS2": ("ILS", 100.0),
}

#: ``GBp`` is case-sensitive in Yahoo's payloads; normalise defensively.
_CASE_SENSITIVE_MINOR = {"GBp": ("GBP", 100.0), "ZAc": ("ZAR", 100.0)}


@dataclass(frozen=True)
class QuoteNormalisation:
    """Result of converting a quoted price into major currency units."""

    currency: str
    price: float | None
    divisor: float
    was_minor_units: bool


def canonical_currency(code: str | None) -> str | None:
    """Return the *major* ISO currency code for a possibly-minor-unit code."""
    if code is None:
        return None
    raw = code.strip()
    if not raw:
        return None
    if raw in _CASE_SENSITIVE_MINOR:
        return _CASE_SENSITIVE_MINOR[raw][0]
    upper = raw.upper()
    if upper in MINOR_UNIT_CURRENCIES:
        return MINOR_UNIT_CURRENCIES[upper][0]
    return upper


def minor_unit_divisor(code: str | None) -> float:
    """Return 100.0 for pence-style quotes, else 1.0."""
    if code is None:
        return 1.0
    raw = code.strip()
    if raw in _CASE_SENSITIVE_MINOR:
        return _CASE_SENSITIVE_MINOR[raw][1]
    upper = raw.upper()
    if upper in MINOR_UNIT_CURRENCIES:
        return MINOR_UNIT_CURRENCIES[upper][1]
    return 1.0


def normalise_quote(currency: str | None, price: float | None) -> QuoteNormalisation:
    """Convert ``price`` from ``currency`` into that currency's major units.

    ``normalise_quote("GBp", 250.0)`` -> ``QuoteNormalisation("GBP", 2.5, 100.0, True)``
    """
    divisor = minor_unit_divisor(currency)
    canon = canonical_currency(currency)
    new_price = None if price is None else float(price) / divisor
    return QuoteNormalisation(
        currency=canon or "",
        price=new_price,
        divisor=divisor,
        was_minor_units=divisor != 1.0,
    )


class FxConverter:
    """Converts amounts into USD using a fixed, timestamped set of FX rates.

    Rates are stored as "units of USD per 1 unit of currency" so that
    ``amount_in_ccy * rate == amount_in_usd``.  The rate set is captured once
    per run and persisted in the quarterly snapshot, which means a snapshot can
    always be recomputed exactly.
    """

    def __init__(self, rates_to_usd: dict[str, float]):
        self._rates: dict[str, float] = {"USD": 1.0}
        for code, rate in rates_to_usd.items():
            canon = canonical_currency(code)
            if canon and rate and rate > 0:
                self._rates[canon] = float(rate)

    @property
    def rates(self) -> dict[str, float]:
        return dict(self._rates)

    def has(self, currency: str | None) -> bool:
        canon = canonical_currency(currency)
        return canon is not None and canon in self._rates

    def rate(self, currency: str | None) -> float | None:
        """USD per 1 unit of ``currency`` (handles pence-style codes)."""
        if currency is None:
            return None
        canon = canonical_currency(currency)
        if canon is None or canon not in self._rates:
            return None
        return self._rates[canon] / minor_unit_divisor(currency)

    def to_usd(self, amount: float | None, currency: str | None) -> float | None:
        """Convert ``amount`` expressed in ``currency`` into USD.

        Returns ``None`` when the amount is missing or no rate is available --
        never a guess, never zero.
        """
        if amount is None:
            return None
        r = self.rate(currency)
        if r is None:
            return None
        return float(amount) * r
