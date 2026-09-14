"""Data-quality safeguards.

Free data is not clean data.  Everything in this module exists because one of
the following silently destroys a screen if it goes unchecked: pence-versus-
pounds quoting, a stale share count, a provider returning an empty payload that
looks like a legitimate zero, or a company whose accounts are two years old.

Two kinds of output are produced:

* **flags** -- recorded against the company and carried into ``selected.csv``
  and the report, but not disqualifying;
* **exclusions** -- a machine-readable reason string; the company leaves the
  universe and appears in ``excluded.csv``.

Nothing is ever dropped without a reason, and no missing value is ever replaced
with a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..config import CONFIG, FactorConfig
from ..currency import FxConverter, normalise_quote
from ..providers.base import CompanyMetadata

#: A market cap above this (USD) is not credible for any listed company and
#: indicates a units error rather than a very large company.
IMPLAUSIBLE_MARKET_CAP_USD = 1.0e13
#: Fewer shares than this means the share count is in the wrong units.
MIN_PLAUSIBLE_SHARES = 1_000.0
#: A provider-reported enterprise value outside this band versus market cap is
#: a units problem, not a leveraged company.
PROVIDER_EV_MIN_RATIO = 0.02
PROVIDER_EV_MAX_RATIO = 50.0


@dataclass
class MarketCapAssessment:
    """Reconciled market capitalisation plus everything that went into it."""

    market_cap_local: float | None = None
    market_cap_usd: float | None = None
    price_major: float | None = None
    currency: str | None = None
    implied_market_cap: float | None = None
    reported_market_cap: float | None = None
    ratio: float | None = None
    was_minor_units: bool = False
    flags: list[str] = field(default_factory=list)
    exclusion: str | None = None


def assess_market_cap(
    metadata: CompanyMetadata,
    fx: FxConverter,
    config: FactorConfig = CONFIG,
) -> MarketCapAssessment:
    """Reconcile the reported market cap against ``price x shares``.

    This is the 100x-error detector.  A London share quoted at 250 (pence) with
    100m shares has a real market cap of GBP 250m, not GBP 25bn; if the two
    sources of truth disagree by roughly a factor of 100, the discrepancy is
    resolved explicitly and flagged rather than averaged away.
    """
    result = MarketCapAssessment()

    quote = normalise_quote(metadata.quote_currency, metadata.price)
    result.currency = quote.currency or None
    result.price_major = quote.price
    result.was_minor_units = quote.was_minor_units
    if quote.was_minor_units:
        result.flags.append("price_quoted_in_minor_units")

    if not result.currency:
        result.exclusion = "quote_currency_missing"
        return result

    shares = metadata.shares_outstanding
    if shares is not None and shares < MIN_PLAUSIBLE_SHARES:
        result.flags.append("share_count_implausible")
        shares = None

    if quote.price is not None and shares:
        result.implied_market_cap = float(quote.price) * float(shares)
    result.reported_market_cap = metadata.market_cap

    reported, implied = result.reported_market_cap, result.implied_market_cap

    if reported is not None and implied is not None and implied > 0:
        ratio = float(reported) / float(implied)
        result.ratio = ratio
        if config.MARKET_CAP_TOLERANCE_LOW <= ratio <= config.MARKET_CAP_TOLERANCE_HIGH:
            result.market_cap_local = float(reported)
        elif 50.0 <= ratio <= 200.0:
            # The reported cap is in minor units; the price was not.
            result.market_cap_local = float(reported) / 100.0
            result.flags.append("market_cap_minor_units_adjusted")
        elif 0.005 <= ratio <= 0.02:
            # The reported cap is 100x too small versus price x shares.
            result.market_cap_local = implied
            result.flags.append("market_cap_unit_mismatch_100x")
        elif ratio > config.MARKET_CAP_HARD_TOLERANCE or ratio < 1.0 / config.MARKET_CAP_HARD_TOLERANCE:
            result.exclusion = f"market_cap_inconsistent:ratio={ratio:.3g}"
            return result
        else:
            # Share counts go stale between filings; prefer the reported cap.
            result.market_cap_local = float(reported)
            result.flags.append(f"market_cap_discrepancy:ratio={ratio:.3g}")
    elif reported is not None:
        result.market_cap_local = float(reported)
        result.flags.append("market_cap_unverified_no_share_count")
    elif implied is not None:
        result.market_cap_local = implied
        result.flags.append("market_cap_from_price_times_shares")
    else:
        result.exclusion = "market_cap_unavailable"
        return result

    if result.market_cap_local is None or result.market_cap_local <= 0:
        result.exclusion = "market_cap_non_positive"
        return result

    usd = fx.to_usd(result.market_cap_local, result.currency)
    if usd is None:
        result.exclusion = f"fx_rate_unavailable:{result.currency}"
        return result
    if usd > IMPLAUSIBLE_MARKET_CAP_USD:
        result.exclusion = f"market_cap_implausible:{usd:.3g}"
        return result
    result.market_cap_usd = usd
    return result


def statement_freshness_flag(
    latest_period_end: date | None,
    as_of: date,
    config: FactorConfig = CONFIG,
) -> str | None:
    """Flag accounts that are older than the configured staleness window."""
    if latest_period_end is None:
        return "statement_date_unknown"
    age = (as_of - latest_period_end).days
    if age > config.MAX_STATEMENT_AGE_DAYS:
        return f"stale_statements:{age}d"
    return None


def currency_mismatch_flag(quote_currency: str | None, financial_currency: str | None) -> str | None:
    """Flag a company reporting in a currency other than its listing currency."""
    from ..currency import canonical_currency

    quote = canonical_currency(quote_currency)
    financial = canonical_currency(financial_currency)
    if not quote or not financial:
        return None
    if quote != financial:
        return f"reporting_currency_differs:{financial}_vs_{quote}"
    return None


def extreme_yield_flag(fcf_yield_value: float | None, config: FactorConfig = CONFIG) -> str | None:
    """Flag implausibly high FCF yields for review -- without re-ranking them."""
    if fcf_yield_value is None:
        return None
    if fcf_yield_value > config.EXTREME_FCF_YIELD:
        return f"extreme_fcf_yield:{fcf_yield_value:.3g}"
    return None


def provider_ev_sanity_flag(
    provider_ev_usd: float | None, market_cap_usd: float | None
) -> str | None:
    """Flag a provider enterprise value that cannot be in the same units as market cap.

    This matters most for London listings, where the provider quotes prices in
    pence but reports market cap and enterprise value in pounds.  A fallback EV
    that is 100x too small would hand the company an enormous FCF yield and put
    it straight at the top of the value screen, so an implausible figure is
    discarded rather than used.
    """
    if provider_ev_usd is None or not market_cap_usd or market_cap_usd <= 0:
        return None
    ratio = provider_ev_usd / market_cap_usd
    if ratio < PROVIDER_EV_MIN_RATIO or ratio > PROVIDER_EV_MAX_RATIO:
        return f"provider_ev_implausible:ratio={ratio:.3g}"
    return None


def empty_payload_flag(has_metadata: bool, annual_count: int, quarterly_count: int) -> str | None:
    """Flag a provider response that arrived but carried no statements at all."""
    if has_metadata and annual_count == 0 and quarterly_count == 0:
        return "provider_returned_no_statements"
    return None


@dataclass
class CoverageSummary:
    """Per-run counts used by the report, the email and the integrity checks."""

    universe_size: int = 0
    metadata_resolved: int = 0
    passed_security_filters: int = 0
    above_market_cap: int = 0
    passed_financial_filters: int = 0
    eligible: int = 0
    value_shortlist: int = 0
    selected: int = 0
    exclusion_counts: dict[str, int] = field(default_factory=dict)
    flag_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def metadata_coverage(self) -> float:
        if not self.universe_size:
            return 0.0
        return self.metadata_resolved / self.universe_size

    @property
    def financial_coverage(self) -> float:
        """Share of the market-cap-qualified names that had usable financials."""
        if not self.above_market_cap:
            return 0.0
        return self.eligible / self.above_market_cap

    def to_dict(self) -> dict:
        return {
            "universe_size": self.universe_size,
            "metadata_resolved": self.metadata_resolved,
            "passed_security_filters": self.passed_security_filters,
            "above_market_cap": self.above_market_cap,
            "passed_financial_filters": self.passed_financial_filters,
            "eligible": self.eligible,
            "value_shortlist": self.value_shortlist,
            "selected": self.selected,
            "metadata_coverage": round(self.metadata_coverage, 6),
            "financial_coverage": round(self.financial_coverage, 6),
            "exclusion_counts": dict(sorted(self.exclusion_counts.items())),
            "flag_counts": dict(sorted(self.flag_counts.items())),
            "warnings": list(self.warnings),
        }
