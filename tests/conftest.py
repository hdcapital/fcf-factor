"""Shared fixtures.

Two rules govern this suite:

* **No network.**  A fixture replaces ``requests.get`` with something that
  raises, so a test that accidentally reaches for the internet fails loudly
  instead of becoming flaky.
* **No writes into the repository.**  Every test that persists anything runs
  against a temporary data root.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fcf_factor import config  # noqa: E402
from fcf_factor.providers.base import (  # noqa: E402
    BalanceSheetSnapshot,
    CompanyFundamentals,
    CompanyMetadata,
    IncomeCashflowPeriod,
    PriceBar,
)
from fcf_factor.universe.base import UniverseEntry  # noqa: E402


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Make any accidental HTTP call fail immediately and obviously."""

    def blocked(*args, **kwargs):  # pragma: no cover - only runs on a bug
        raise AssertionError("tests must not perform network requests")

    monkeypatch.setattr("requests.get", blocked, raising=False)
    monkeypatch.setattr("requests.post", blocked, raising=False)


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """Redirect all persistence into a temporary directory."""
    original = (
        config.DATA_DIR,
        config.UNIVERSE_DIR,
        config.SIGNALS_DIR,
        config.PRICES_DIR,
        config.PERFORMANCE_DIR,
        config.STATE_DIR,
        config.REPORTS_DIR,
    )
    config.set_data_root(tmp_path / "data", tmp_path / "reports")
    yield tmp_path
    (
        config.DATA_DIR,
        config.UNIVERSE_DIR,
        config.SIGNALS_DIR,
        config.PRICES_DIR,
        config.PERFORMANCE_DIR,
        config.STATE_DIR,
        config.REPORTS_DIR,
    ) = original


@pytest.fixture
def fx():
    """A fixed FX table, so conversions in tests are exact and checkable."""
    from fcf_factor.currency import FxConverter

    return FxConverter({"USD": 1.0, "AUD": 0.65, "GBP": 1.25, "NZD": 0.60, "CAD": 0.75, "EUR": 1.10})


def make_annual(
    period_end: date,
    revenue: float,
    cfo: float,
    capex: float,
    ebitda: float | None = None,
    shares: float | None = 1_000_000.0,
    currency: str = "USD",
) -> IncomeCashflowPeriod:
    return IncomeCashflowPeriod(
        period_end=period_end,
        period_type="annual",
        currency=currency,
        revenue=revenue,
        ebitda=(None if revenue is None else revenue * 0.2) if ebitda is None else ebitda,
        ebit=None if revenue is None else revenue * 0.15,
        operating_cashflow=cfo,
        capex=capex,
        depreciation_amortization=None if revenue is None else revenue * 0.05,
        diluted_shares=shares,
        basic_shares=shares,
    )


def make_quarter(period_end: date, revenue: float, cfo: float, capex: float, ebitda: float | None = None):
    return IncomeCashflowPeriod(
        period_end=period_end,
        period_type="quarterly",
        currency="USD",
        revenue=revenue,
        ebitda=(None if revenue is None else revenue * 0.2) if ebitda is None else ebitda,
        ebit=None if revenue is None else revenue * 0.15,
        operating_cashflow=cfo,
        capex=capex,
        diluted_shares=1_000_000.0,
        basic_shares=1_000_000.0,
    )


@pytest.fixture
def healthy_company():
    """A well-behaved company with four clean annual years and four quarters."""

    def build(
        symbol: str = "TEST",
        *,
        sector: str = "Technology",
        base_revenue: float = 100_000_000.0,
        growth: float = 0.10,
        margin: float = 0.12,
        currency: str = "USD",
        quote_currency: str | None = None,
        shares: float = 50_000_000.0,
        price: float = 10.0,
        market_cap: float | None = None,
    ) -> CompanyFundamentals:
        quote_currency = quote_currency or currency
        annual = []
        for i in range(4):
            revenue = base_revenue * ((1 + growth) ** i)
            cfo = revenue * (margin + 0.05)
            annual.append(
                make_annual(
                    date(2022 + i, 12, 31),
                    revenue,
                    cfo,
                    -revenue * 0.05,
                    shares=shares,
                    currency=currency,
                )
            )
        latest_revenue = base_revenue * ((1 + growth) ** 3)
        quarters = []
        end = date(2025, 12, 31)
        for q in range(4):
            quarters.append(
                make_quarter(
                    end - timedelta(days=90 * (3 - q)),
                    latest_revenue / 4,
                    latest_revenue / 4 * (margin + 0.05),
                    -latest_revenue / 4 * 0.05,
                )
            )
        balance = [
            BalanceSheetSnapshot(
                period_end=date(2025, 12, 31),
                period_type="annual",
                currency=currency,
                total_debt=base_revenue * 0.2,
                cash_and_equivalents=base_revenue * 0.1,
                preferred_stock=0.0,
                minority_interest=0.0,
                shares_outstanding=shares,
            )
        ]
        metadata = CompanyMetadata(
            symbol=symbol,
            name=f"{symbol} Limited",
            sector=sector,
            industry="Software - Application",
            quote_type="EQUITY",
            exchange="TEST",
            quote_currency=quote_currency,
            financial_currency=currency,
            price=price,
            shares_outstanding=shares,
            market_cap=market_cap if market_cap is not None else price * shares,
            enterprise_value=price * shares * 1.1,
        )
        return CompanyFundamentals(
            symbol=symbol,
            metadata=metadata,
            annual=annual,
            quarterly=quarters,
            annual_balance=balance,
            quarterly_balance=[replace(balance[0], period_type="quarterly")],
        )

    return build


@pytest.fixture
def entry_factory():
    def build(symbol: str, market: str = "US") -> UniverseEntry:
        return UniverseEntry(
            market=market,
            exchange="TEST",
            exchange_symbol=symbol.split(".")[0],
            yahoo_symbol=symbol,
            name=f"{symbol} Limited",
            source="test",
        )

    return build


def bar(day: date, close: float, open_: float | None = None, dividend: float = 0.0, split: float = 0.0, currency: str = "AUD") -> PriceBar:
    return PriceBar(
        date=day,
        open=open_ if open_ is not None else close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        adj_close=close,
        volume=1000.0,
        dividend=dividend,
        split=split,
        currency=currency,
    )
