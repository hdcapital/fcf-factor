"""Assembling the FX rate set for one run.

Rates are captured once, at the factor observation date, and then persisted in
the quarterly snapshot.  Re-running a historical snapshot therefore reproduces
the original USD conversions exactly rather than re-converting at today's rate.
"""

from __future__ import annotations

from collections.abc import Iterable

from .config import MARKET_CURRENCY
from .currency import FxConverter, canonical_currency
from .logging_utils import get_logger
from .providers.base import DataProvider

log = get_logger(__name__)

#: Reporting currencies common enough to fetch up front, so a single company
#: reporting in EUR does not trigger a mid-run network call.
COMMON_REPORTING_CURRENCIES = ("USD", "AUD", "GBP", "NZD", "CAD", "EUR", "HKD", "SGD", "JPY", "CHF", "SEK", "NOK", "DKK", "ZAR", "ILS", "CNY")


class MissingFxRateError(RuntimeError):
    """Raised when a market's own listing currency cannot be converted."""


def collect_currencies(markets: Iterable[str], extra: Iterable[str] | None = None) -> list[str]:
    codes = {"USD"}
    for market in markets:
        codes.add(MARKET_CURRENCY[market.upper()])
    codes.update(COMMON_REPORTING_CURRENCIES)
    for code in extra or []:
        canon = canonical_currency(code)
        if canon:
            codes.add(canon)
    return sorted(codes)


def build_fx_converter(
    provider: DataProvider,
    currencies: Iterable[str],
    *,
    required: Iterable[str] = (),
) -> FxConverter:
    """Fetch USD rates for ``currencies``; fail loudly if a required one is missing."""
    rates: dict[str, float] = {"USD": 1.0}
    for code in currencies:
        canon = canonical_currency(code)
        if not canon or canon in rates:
            continue
        rate = provider.get_fx_rate(canon, "USD")
        if rate is None or rate <= 0:
            log.warning("no FX rate available for %s -> USD", canon)
            continue
        rates[canon] = rate

    missing = [
        canonical_currency(code)
        for code in required
        if canonical_currency(code) not in rates
    ]
    if missing:
        raise MissingFxRateError(
            "cannot convert required currencies to USD: " + ", ".join(sorted(str(m) for m in missing))
        )
    return FxConverter(rates)
