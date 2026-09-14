"""A deterministic, offline provider used for smoke tests and examples.

It exists so that ``python -m fcf_factor screen --market NZ --limit 20 --dry-run
--provider synthetic`` runs end-to-end on a laptop with no network access, and
so the pipeline itself can be exercised in CI without touching Yahoo.

The data it returns is **invented**.  To make sure it can never contaminate the
research record, :mod:`fcf_factor.pipeline` refuses to persist a quarterly
snapshot produced by this provider.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

from ..config import MARKET_CURRENCY
from .base import (
    BalanceSheetSnapshot,
    CompanyMetadata,
    DataProvider,
    IncomeCashflowPeriod,
    PriceBar,
)

SECTORS = (
    "Technology",
    "Industrials",
    "Healthcare",
    "Consumer Cyclical",
    "Basic Materials",
    "Energy",
    "Communication Services",
    "Consumer Defensive",
    "Utilities",
)

_SUFFIX_CURRENCY = {
    ".AX": "AUD",
    ".NZ": "NZD",
    ".L": "GBp",
    ".TO": "CAD",
    ".V": "CAD",
}

#: Indicative, clearly-fake FX rates.  Never used for real snapshots.
SYNTHETIC_FX = {"AUD": 0.66, "USD": 1.0, "GBP": 1.27, "NZD": 0.61, "CAD": 0.73, "EUR": 1.08}


def _unit(symbol: str, salt: str) -> float:
    """Deterministic pseudo-random number in [0, 1) from the symbol."""
    digest = hashlib.sha256(f"{symbol}|{salt}".encode()).hexdigest()
    return int(digest[:12], 16) / float(16**12)


class SyntheticProvider(DataProvider):
    """Reproducible fake fundamentals -- useful for plumbing, useless for alpha."""

    name = "synthetic"

    def __init__(self, today: date | None = None, annual_periods: int = 4):
        self.today = today or date.today()
        self.annual_periods = annual_periods

    # ---- helpers ---------------------------------------------------------
    def _currency(self, symbol: str) -> str:
        for suffix, ccy in _SUFFIX_CURRENCY.items():
            if symbol.endswith(suffix):
                return ccy
        return "USD"

    def _base_revenue(self, symbol: str) -> float:
        return 5.0e7 + _unit(symbol, "rev") * 9.5e8

    def _growth(self, symbol: str) -> float:
        return -0.05 + _unit(symbol, "g") * 0.35

    def _margin(self, symbol: str) -> float:
        return -0.02 + _unit(symbol, "m") * 0.22

    def _shares(self, symbol: str) -> float:
        return 2.0e7 + _unit(symbol, "sh") * 3.0e8

    def _price(self, symbol: str) -> float:
        raw = 1.0 + _unit(symbol, "p") * 40.0
        return raw * 100.0 if self._currency(symbol) == "GBp" else raw

    # ---- interface -------------------------------------------------------
    def get_company_metadata(self, symbol: str) -> CompanyMetadata | None:
        if _unit(symbol, "exists") < 0.05:
            return None  # simulate a delisted / unmapped ticker
        ccy = self._currency(symbol)
        price = self._price(symbol)
        shares = self._shares(symbol)
        major_price = price / 100.0 if ccy == "GBp" else price
        sector = SECTORS[int(_unit(symbol, "sector") * len(SECTORS))]
        fin_ccy = "GBP" if ccy == "GBp" else ccy
        annual = self.get_annual_financials(symbol)
        latest = annual[-1] if annual else None
        return CompanyMetadata(
            symbol=symbol,
            name=f"{symbol.split('.')[0].title()} Holdings",
            sector=sector,
            industry=f"{sector} Equipment",
            quote_type="EQUITY",
            exchange="SYNTHETIC",
            quote_currency=ccy,
            financial_currency=fin_ccy,
            price=price,
            price_date=self.today,
            shares_outstanding=shares,
            market_cap=major_price * shares,
            enterprise_value=major_price * shares * 1.1,
            ttm_revenue=latest.revenue if latest else None,
            ttm_ebitda=latest.ebitda if latest else None,
            ttm_operating_cashflow=latest.operating_cashflow if latest else None,
            ttm_free_cashflow=None,
            ttm_capex=latest.capex if latest else None,
        )

    def _periods(self, symbol: str, period_type: str) -> list[IncomeCashflowPeriod]:
        ccy = self._currency(symbol)
        fin_ccy = "GBP" if ccy == "GBp" else ccy
        base = self._base_revenue(symbol)
        growth = self._growth(symbol)
        margin = self._margin(symbol)
        shares = self._shares(symbol)
        out: list[IncomeCashflowPeriod] = []
        count = self.annual_periods if period_type == "annual" else 4
        for i in range(count):
            if period_type == "annual":
                offset = count - 1 - i
                end = date(self.today.year - offset - 1, 12, 31)
                revenue = base * ((1.0 + growth) ** (count - 1 - offset))
                scale = 1.0
            else:
                end = self.today - timedelta(days=90 * (count - 1 - i))
                revenue = base * ((1.0 + growth) ** (count - 1)) / 4.0
                scale = 0.25
            cfo = revenue * (margin + 0.05)
            capex = -revenue * 0.05
            out.append(
                IncomeCashflowPeriod(
                    period_end=end,
                    period_type=period_type,
                    currency=fin_ccy,
                    revenue=revenue,
                    ebitda=revenue * (margin + 0.09),
                    ebit=revenue * (margin + 0.05),
                    operating_cashflow=cfo,
                    capex=capex,
                    depreciation_amortization=revenue * 0.04,
                    diluted_shares=shares * scale if period_type == "quarterly" else shares,
                    basic_shares=shares * scale if period_type == "quarterly" else shares,
                )
            )
        return out

    def get_annual_financials(self, symbol: str) -> list[IncomeCashflowPeriod]:
        return self._periods(symbol, "annual")

    def get_quarterly_financials(self, symbol: str) -> list[IncomeCashflowPeriod]:
        return self._periods(symbol, "quarterly")

    def get_balance_sheet(self, symbol: str, period: str = "annual") -> list[BalanceSheetSnapshot]:
        ccy = self._currency(symbol)
        fin_ccy = "GBP" if ccy == "GBp" else ccy
        revenue = self._base_revenue(symbol)
        out = []
        for p in self._periods(symbol, period):
            out.append(
                BalanceSheetSnapshot(
                    period_end=p.period_end,
                    period_type=period,
                    currency=fin_ccy,
                    total_debt=revenue * 0.3,
                    cash_and_equivalents=revenue * 0.15,
                    preferred_stock=0.0,
                    minority_interest=0.0,
                    shares_outstanding=self._shares(symbol),
                )
            )
        return out

    def get_prices(self, symbol: str, start: date, end: date) -> list[PriceBar]:
        ccy = self._currency(symbol)
        price = self._price(symbol)
        bars: list[PriceBar] = []
        day = start
        i = 0
        while day <= end:
            if day.weekday() < 5:
                drift = 1.0 + 0.001 * ((i % 7) - 3)
                close = price * drift
                bars.append(
                    PriceBar(
                        date=day,
                        open=close * 0.995,
                        high=close * 1.01,
                        low=close * 0.99,
                        close=close,
                        adj_close=close,
                        volume=100000.0,
                        dividend=0.0,
                        split=0.0,
                        currency=ccy,
                    )
                )
                i += 1
            day += timedelta(days=1)
        return bars

    def get_fx_rate(self, base: str, quote: str = "USD") -> float | None:
        from ..currency import canonical_currency, minor_unit_divisor

        b, q = canonical_currency(base), canonical_currency(quote)
        if b is None or q is None or b not in SYNTHETIC_FX or q not in SYNTHETIC_FX:
            return None
        divisor = minor_unit_divisor(base) / minor_unit_divisor(quote)
        return (SYNTHETIC_FX[b] / SYNTHETIC_FX[q]) / divisor


def synthetic_universe_symbols(market: str, count: int = 60) -> list[str]:
    """Deterministic fake tickers for a market, used by the offline smoke test."""
    suffix = {"AU": ".AX", "US": "", "UK": ".L", "NZ": ".NZ", "CA": ".TO"}[market]
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    symbols = []
    for i in range(count):
        root = letters[i % 26] + letters[(i // 26) % 26] + letters[(i * 7 + 3) % 26]
        symbols.append(f"{root}{suffix}")
    assert MARKET_CURRENCY[market]  # market must be known
    return symbols
