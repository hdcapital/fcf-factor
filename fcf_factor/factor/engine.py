"""Per-company factor evaluation.

This is where a :class:`~fcf_factor.providers.base.CompanyFundamentals` bundle
becomes a :class:`CompanyFactors` row.  Everything is provider-neutral and
side-effect free, so the whole factor can be exercised in tests with handmade
fixtures.

Order of operations matters and is deliberate:

1. identity and security-type checks (cheapest, no arithmetic),
2. market cap in USD and the ``$50m`` floor,
3. statement history sufficiency,
4. TTM construction with its fallback ladder,
5. normalised margin -> forward growth -> forward FCF -> expected FCF,
6. enterprise value in USD, then FCF yield,
7. growth/quality trends.

A company that fails any step carries an explicit exclusion reason and never
reaches the cross-sectional stage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..config import CONFIG, FactorConfig
from ..currency import FxConverter, canonical_currency
from ..providers.base import CompanyFundamentals
from ..universe.base import UniverseEntry
from ..universe.filters import quote_type_exclusion_reason, sector_exclusion_reason
from .fcf import annual_fcf_margins, annual_fcf_series, build_ttm, fcf_margin
from .growth import (
    expected_fcf,
    forward_fcf,
    forward_growth,
    forward_revenue,
    normalized_fcf_margin,
)
from .quality import (
    assess_market_cap,
    currency_mismatch_flag,
    empty_payload_flag,
    extreme_yield_flag,
    provider_ev_sanity_flag,
    statement_freshness_flag,
)
from .trends import normalized_trend, per_share_series
from .valuation import calculate_enterprise_value, fcf_yield


@dataclass
class CompanyFactors:
    """One company's complete factor record for one observation date."""

    ticker: str
    exchange_symbol: str = ""
    company: str | None = None
    sector: str | None = None
    industry: str | None = None
    market: str = ""
    currency: str | None = None
    financial_currency: str | None = None

    price_major: float | None = None
    shares_outstanding: float | None = None
    market_cap_local: float | None = None
    market_cap_usd: float | None = None

    ttm_revenue: float | None = None
    ttm_ebitda: float | None = None
    ttm_fcf: float | None = None
    ttm_fcf_usd: float | None = None
    ttm_source: str | None = None
    normalized_fcf_margin: float | None = None
    normalized_margin_observations: int = 0

    forward_growth: float | None = None
    revenue_cagr: float | None = None
    latest_yoy_growth: float | None = None
    forward_revenue: float | None = None
    forward_fcf: float | None = None
    expected_fcf: float | None = None
    expected_fcf_usd: float | None = None

    total_debt_usd: float | None = None
    cash_usd: float | None = None
    enterprise_value_usd: float | None = None
    enterprise_value_calculated_usd: float | None = None
    enterprise_value_provider_usd: float | None = None
    enterprise_value_source: str | None = None

    fcf_yield: float | None = None

    revenue_trend: float | None = None
    ebitda_trend: float | None = None
    fcf_per_share_trend: float | None = None
    z_revenue: float | None = None
    z_ebitda: float | None = None
    z_fcf_per_share: float | None = None
    growth_score: float | None = None

    fcf_yield_rank: int | None = None
    growth_rank: int | None = None
    target_weight: float | None = None
    raw_weight: float | None = None

    annual_periods: int = 0
    latest_annual_period_end: date | None = None
    eligible: bool = False
    exclusion_reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def add_flag(self, flag: str | None) -> None:
        if flag and flag not in self.flags:
            self.flags.append(flag)

    def exclude(self, reason: str) -> CompanyFactors:
        if reason not in self.exclusion_reasons:
            self.exclusion_reasons.append(reason)
        self.eligible = False
        return self

    def to_row(self) -> dict:
        """Flat dict for ``factor_scores.csv`` / ``selected.csv``."""
        return {
            "ticker": self.ticker,
            "exchange_symbol": self.exchange_symbol,
            "company": self.company or "",
            "sector": self.sector or "",
            "industry": self.industry or "",
            "market": self.market,
            "currency": self.currency or "",
            "financial_currency": self.financial_currency or "",
            "price_major": self.price_major,
            "shares_outstanding": self.shares_outstanding,
            "market_cap_local": self.market_cap_local,
            "market_cap_usd": self.market_cap_usd,
            "ttm_revenue": self.ttm_revenue,
            "ttm_ebitda": self.ttm_ebitda,
            "ttm_fcf": self.ttm_fcf,
            "ttm_fcf_usd": self.ttm_fcf_usd,
            "ttm_source": self.ttm_source or "",
            "normalized_fcf_margin": self.normalized_fcf_margin,
            "normalized_margin_observations": self.normalized_margin_observations,
            "forward_growth": self.forward_growth,
            "revenue_cagr": self.revenue_cagr,
            "latest_yoy_growth": self.latest_yoy_growth,
            "forward_revenue": self.forward_revenue,
            "forward_fcf": self.forward_fcf,
            "expected_fcf": self.expected_fcf,
            "expected_fcf_usd": self.expected_fcf_usd,
            "total_debt_usd": self.total_debt_usd,
            "cash_usd": self.cash_usd,
            "enterprise_value_usd": self.enterprise_value_usd,
            "enterprise_value_calculated_usd": self.enterprise_value_calculated_usd,
            "enterprise_value_provider_usd": self.enterprise_value_provider_usd,
            "enterprise_value_source": self.enterprise_value_source or "",
            "fcf_yield": self.fcf_yield,
            "revenue_trend": self.revenue_trend,
            "ebitda_trend": self.ebitda_trend,
            "fcf_per_share_trend": self.fcf_per_share_trend,
            "z_revenue": self.z_revenue,
            "z_ebitda": self.z_ebitda,
            "z_fcf_per_share": self.z_fcf_per_share,
            "growth_score": self.growth_score,
            "fcf_yield_rank": self.fcf_yield_rank,
            "growth_rank": self.growth_rank,
            "raw_weight": self.raw_weight,
            "target_weight": self.target_weight,
            "annual_periods": self.annual_periods,
            "latest_annual_period_end": self.latest_annual_period_end,
            "eligible": self.eligible,
            "exclusion_reasons": "|".join(self.exclusion_reasons),
            "data_quality_flags": "|".join(self.flags),
        }


def evaluate_company(
    entry: UniverseEntry,
    fundamentals: CompanyFundamentals,
    fx: FxConverter,
    as_of: date,
    config: FactorConfig = CONFIG,
) -> CompanyFactors:
    """Score one company, or record exactly why it cannot be scored."""
    factors = CompanyFactors(
        ticker=entry.yahoo_symbol,
        exchange_symbol=entry.exchange_symbol,
        company=entry.name,
        market=entry.market,
    )

    metadata = fundamentals.metadata
    if metadata is None:
        return factors.exclude("symbol_unresolved")

    factors.company = metadata.name or entry.name
    factors.sector = metadata.sector
    factors.industry = metadata.industry or entry.industry
    factors.financial_currency = canonical_currency(metadata.financial_currency)

    reason = quote_type_exclusion_reason(metadata.quote_type)
    if reason:
        return factors.exclude(reason)

    reason = sector_exclusion_reason(metadata.sector, metadata.industry)
    if reason:
        return factors.exclude(reason)

    # ---- market capitalisation ------------------------------------------
    cap = assess_market_cap(metadata, fx, config)
    for flag in cap.flags:
        factors.add_flag(flag)
    factors.currency = cap.currency
    factors.price_major = cap.price_major
    factors.shares_outstanding = metadata.shares_outstanding
    factors.market_cap_local = cap.market_cap_local
    factors.market_cap_usd = cap.market_cap_usd
    if cap.exclusion:
        return factors.exclude(cap.exclusion)
    if cap.market_cap_usd is None:
        return factors.exclude("market_cap_usd_unavailable")
    if cap.market_cap_usd < config.MIN_MARKET_CAP_USD:
        return factors.exclude(f"below_min_market_cap:{cap.market_cap_usd:.0f}")

    factors.add_flag(currency_mismatch_flag(metadata.quote_currency, metadata.financial_currency))
    factors.add_flag(
        empty_payload_flag(True, len(fundamentals.annual), len(fundamentals.quarterly))
    )

    # ---- annual history --------------------------------------------------
    annual = fundamentals.annual_sorted
    revenues = [p.revenue for p in annual]
    usable_years = sum(1 for r in revenues if r is not None)
    factors.annual_periods = usable_years
    if annual:
        factors.latest_annual_period_end = annual[-1].period_end
    factors.add_flag(statement_freshness_flag(factors.latest_annual_period_end, as_of, config))
    if usable_years < config.MIN_ANNUAL_PERIODS:
        return factors.exclude(f"insufficient_annual_history:{usable_years}")
    if usable_years < config.PREFERRED_ANNUAL_PERIODS:
        factors.add_flag(f"annual_history_below_preferred:{usable_years}")

    financial_currency = factors.financial_currency
    if financial_currency is None:
        financial_currency = factors.currency
        factors.add_flag("financial_currency_missing_assumed_quote_currency")
    if not fx.has(financial_currency):
        return factors.exclude(f"fx_rate_unavailable:{financial_currency}")

    # ---- TTM -------------------------------------------------------------
    ttm = build_ttm(fundamentals.quarterly_sorted, annual, metadata)
    for flag in ttm.flags:
        factors.add_flag(flag)
    factors.ttm_revenue = ttm.revenue
    factors.ttm_ebitda = ttm.ebitda
    factors.ttm_fcf = ttm.fcf
    factors.ttm_fcf_usd = fx.to_usd(ttm.fcf, financial_currency)
    factors.ttm_source = "|".join(f"{k}={v}" for k, v in ttm.sources().items() if v)

    if ttm.revenue is None:
        return factors.exclude("ttm_revenue_unavailable")
    if ttm.fcf is None:
        return factors.exclude("ttm_fcf_unavailable")

    # ---- profitability gate ---------------------------------------------
    latest_fy = annual[-1]
    if ttm.ebitda is not None:
        if ttm.ebitda <= 0:
            return factors.exclude("ttm_ebitda_not_positive")
    elif latest_fy.ebitda is not None:
        factors.add_flag("ebitda_gate_used_latest_fy")
        if latest_fy.ebitda <= 0:
            return factors.exclude("latest_fy_ebitda_not_positive")
    else:
        return factors.exclude("ebitda_unavailable")

    # ---- normalised margin ----------------------------------------------
    annual_margins = [m for _, m in annual_fcf_margins(annual)]
    margin, observations = normalized_fcf_margin(annual_margins, fcf_margin(ttm.fcf, ttm.revenue))
    factors.normalized_fcf_margin = margin
    factors.normalized_margin_observations = observations
    if margin is None:
        return factors.exclude("normalized_fcf_margin_unavailable")

    # ---- mechanical forward growth --------------------------------------
    growth = forward_growth(revenues, config)
    for flag in growth.flags:
        factors.add_flag(flag)
    factors.revenue_cagr = growth.cagr
    factors.latest_yoy_growth = growth.yoy
    factors.forward_growth = growth.growth
    if growth.growth is None:
        return factors.exclude("forward_growth_unavailable")

    factors.forward_revenue = forward_revenue(ttm.revenue, growth.growth)
    factors.forward_fcf = forward_fcf(factors.forward_revenue, margin)
    factors.expected_fcf = expected_fcf(ttm.fcf, factors.forward_fcf, config)
    if factors.expected_fcf is None:
        return factors.exclude("expected_fcf_unavailable")
    if factors.expected_fcf <= 0:
        return factors.exclude("expected_fcf_not_positive")
    factors.expected_fcf_usd = fx.to_usd(factors.expected_fcf, financial_currency)
    if factors.expected_fcf_usd is None:
        return factors.exclude(f"fx_rate_unavailable:{financial_currency}")

    # ---- enterprise value ------------------------------------------------
    balance = fundamentals.latest_balance
    debt_available = balance is not None and balance.total_debt is not None
    cash_available = balance is not None and balance.cash_and_equivalents is not None
    factors.total_debt_usd = fx.to_usd(balance.total_debt, financial_currency) if debt_available else None
    factors.cash_usd = (
        fx.to_usd(balance.cash_and_equivalents, financial_currency) if cash_available else None
    )
    # The provider reports enterprise value in *major* units of the listing
    # currency, exactly like market cap, even when it quotes prices in pence.
    # Converting from the raw quote currency here would divide a London
    # company's EV by 100.
    provider_ev_usd = fx.to_usd(metadata.enterprise_value, factors.currency)
    ev_sanity = provider_ev_sanity_flag(provider_ev_usd, factors.market_cap_usd)
    if ev_sanity:
        factors.add_flag(ev_sanity)
        provider_ev_usd = None
    ev = calculate_enterprise_value(
        market_cap_usd=factors.market_cap_usd,
        total_debt_usd=factors.total_debt_usd,
        cash_usd=factors.cash_usd,
        preferred_usd=fx.to_usd(balance.preferred_stock, financial_currency) if balance else None,
        minority_interest_usd=fx.to_usd(balance.minority_interest, financial_currency) if balance else None,
        provider_ev_usd=provider_ev_usd,
        debt_field_available=debt_available,
        cash_field_available=cash_available,
    )
    for flag in ev.flags:
        factors.add_flag(flag)
    factors.enterprise_value_calculated_usd = ev.calculated
    factors.enterprise_value_provider_usd = ev.provider
    factors.enterprise_value_source = ev.source
    factors.enterprise_value_usd = ev.value
    if ev.value is None:
        return factors.exclude("enterprise_value_unavailable")
    if ev.value <= 0:
        return factors.exclude("enterprise_value_not_positive")

    # ---- FCF yield -------------------------------------------------------
    factors.fcf_yield = fcf_yield(factors.expected_fcf_usd, factors.enterprise_value_usd)
    if factors.fcf_yield is None:
        return factors.exclude("fcf_yield_unavailable")
    factors.add_flag(extreme_yield_flag(factors.fcf_yield, config))

    # ---- growth / quality trends ----------------------------------------
    factors.revenue_trend = normalized_trend(revenues, config.MIN_ANNUAL_PERIODS)
    factors.ebitda_trend = normalized_trend([p.ebitda for p in annual], config.MIN_ANNUAL_PERIODS)

    fcf_values = [value for _, value in annual_fcf_series(annual)]
    diluted = [p.diluted_shares for p in annual]
    basic = [p.basic_shares for p in annual]
    if sum(1 for s in diluted if s) >= config.MIN_ANNUAL_PERIODS:
        share_series = diluted
    elif sum(1 for s in basic if s) >= config.MIN_ANNUAL_PERIODS:
        share_series = basic
        factors.add_flag("fcf_per_share_used_basic_shares")
    else:
        share_series = []
        factors.add_flag("fcf_per_share_trend_unavailable")
    if share_series:
        factors.fcf_per_share_trend = normalized_trend(
            per_share_series(fcf_values, share_series), config.MIN_ANNUAL_PERIODS
        )

    if factors.revenue_trend is None:
        return factors.exclude("revenue_trend_unavailable")
    if factors.ebitda_trend is None and factors.fcf_per_share_trend is None:
        return factors.exclude("no_secondary_trend_available")

    factors.eligible = True
    return factors
