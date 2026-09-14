"""Provider-agnostic data model and interface.

The factor engine never imports ``yfinance``.  It only ever sees the plain
dataclasses defined here, so swapping Yahoo for Capital IQ, EODHD, SEC filings
or Companies House is a matter of writing a new :class:`DataProvider`
subclass -- no factor code changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class CompanyMetadata:
    """Point-in-time descriptive and market data for one security."""

    symbol: str
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    quote_type: str | None = None
    exchange: str | None = None
    #: Currency the share price is quoted in, exactly as the provider reports it
    #: (may be a minor-unit code such as ``GBp``).
    quote_currency: str | None = None
    #: Currency the financial statements are reported in.
    financial_currency: str | None = None
    #: Latest share price in ``quote_currency`` units.
    price: float | None = None
    #: Date of the price above, when the provider supplies it.
    price_date: date | None = None
    shares_outstanding: float | None = None
    #: Market capitalisation as reported by the provider, in major units of
    #: ``quote_currency``.  Trust but verify -- see :mod:`fcf_factor.factor.quality`.
    market_cap: float | None = None
    #: Provider-reported enterprise value (fallback only).
    enterprise_value: float | None = None
    #: Provider-supplied trailing-twelve-month figures, used as the *second*
    #: choice behind summing four quarters.
    ttm_revenue: float | None = None
    ttm_ebitda: float | None = None
    ttm_operating_cashflow: float | None = None
    ttm_free_cashflow: float | None = None
    ttm_capex: float | None = None


@dataclass(frozen=True)
class IncomeCashflowPeriod:
    """One fiscal period of income-statement and cash-flow data."""

    period_end: date
    period_type: str  # "annual" | "quarterly"
    currency: str | None = None
    revenue: float | None = None
    ebitda: float | None = None
    ebit: float | None = None
    operating_cashflow: float | None = None
    #: As reported.  Sign conventions differ between providers and even between
    #: statements; the factor engine always takes ``abs()``.
    capex: float | None = None
    #: Depreciation & amortisation, used only to reconstruct EBITDA when the
    #: provider does not report it directly.
    depreciation_amortization: float | None = None
    diluted_shares: float | None = None
    basic_shares: float | None = None
    #: True when ``ebitda`` was reconstructed as EBIT + D&A rather than reported.
    ebitda_is_derived: bool = False


@dataclass(frozen=True)
class BalanceSheetSnapshot:
    """One fiscal period of balance-sheet data."""

    period_end: date
    period_type: str  # "annual" | "quarterly"
    currency: str | None = None
    total_debt: float | None = None
    cash_and_equivalents: float | None = None
    preferred_stock: float | None = None
    minority_interest: float | None = None
    shares_outstanding: float | None = None


@dataclass(frozen=True)
class PriceBar:
    """One daily trading session for one security."""

    date: date
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    adj_close: float | None = None
    volume: float | None = None
    dividend: float = 0.0
    split: float = 0.0
    currency: str | None = None


@dataclass
class CompanyFundamentals:
    """Everything the factor engine needs about one company, provider-neutral."""

    symbol: str
    metadata: CompanyMetadata | None = None
    annual: list[IncomeCashflowPeriod] = field(default_factory=list)
    quarterly: list[IncomeCashflowPeriod] = field(default_factory=list)
    annual_balance: list[BalanceSheetSnapshot] = field(default_factory=list)
    quarterly_balance: list[BalanceSheetSnapshot] = field(default_factory=list)
    #: Non-fatal provider issues (empty payload, partial statements, ...).
    fetch_errors: list[str] = field(default_factory=list)

    @property
    def annual_sorted(self) -> list[IncomeCashflowPeriod]:
        """Annual periods oldest -> newest (the order every trend expects)."""
        return sorted(self.annual, key=lambda p: p.period_end)

    @property
    def quarterly_sorted(self) -> list[IncomeCashflowPeriod]:
        return sorted(self.quarterly, key=lambda p: p.period_end)

    @property
    def latest_balance(self) -> BalanceSheetSnapshot | None:
        """Most recent balance sheet of any frequency."""
        candidates = [*self.quarterly_balance, *self.annual_balance]
        if not candidates:
            return None
        return max(candidates, key=lambda b: b.period_end)


class DataProvider(ABC):
    """Interface every data source must implement."""

    #: Human-readable provider name recorded in every snapshot.
    name: str = "abstract"

    @abstractmethod
    def get_company_metadata(self, symbol: str) -> CompanyMetadata | None:
        """Descriptive data, share price, share count and market cap."""

    @abstractmethod
    def get_annual_financials(self, symbol: str) -> list[IncomeCashflowPeriod]:
        """Annual income-statement and cash-flow history."""

    @abstractmethod
    def get_quarterly_financials(self, symbol: str) -> list[IncomeCashflowPeriod]:
        """Quarterly income-statement and cash-flow history."""

    @abstractmethod
    def get_balance_sheet(self, symbol: str, period: str = "annual") -> list[BalanceSheetSnapshot]:
        """Balance-sheet history for ``period`` in ``{"annual", "quarterly"}``."""

    @abstractmethod
    def get_prices(self, symbol: str, start: date, end: date) -> list[PriceBar]:
        """Daily OHLCV plus dividends and splits, inclusive of ``start``/``end``."""

    @abstractmethod
    def get_fx_rate(self, base: str, quote: str = "USD") -> float | None:
        """Units of ``quote`` per one unit of ``base`` (e.g. AUD->USD ~ 0.66)."""

    # ---- convenience ------------------------------------------------------
    def get_fundamentals(self, symbol: str) -> CompanyFundamentals:
        """Fetch everything for one company, tolerating partial failures."""
        bundle = CompanyFundamentals(symbol=symbol)
        try:
            bundle.metadata = self.get_company_metadata(symbol)
        except Exception as exc:  # pragma: no cover - defensive
            bundle.fetch_errors.append(f"metadata: {exc}")
        for attr, fn, kwargs in (
            ("annual", self.get_annual_financials, {}),
            ("quarterly", self.get_quarterly_financials, {}),
            ("annual_balance", self.get_balance_sheet, {"period": "annual"}),
            ("quarterly_balance", self.get_balance_sheet, {"period": "quarterly"}),
        ):
            try:
                setattr(bundle, attr, fn(symbol, **kwargs) or [])
            except Exception as exc:  # pragma: no cover - defensive
                bundle.fetch_errors.append(f"{attr}: {exc}")
        return bundle
