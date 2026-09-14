"""Enterprise value and FCF yield.

Every component is converted to USD *before* it is combined with another, which
is the only way to handle a Canadian company that lists in CAD and reports in
USD without producing nonsense.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import CONFIG, FactorConfig


@dataclass
class EnterpriseValue:
    """An EV figure together with its provenance and any caveats."""

    value: float | None = None
    source: str | None = None  # "calculated" | "provider"
    calculated: float | None = None
    provider: float | None = None
    flags: list[str] = field(default_factory=list)


def calculate_enterprise_value(
    market_cap_usd: float | None,
    total_debt_usd: float | None,
    cash_usd: float | None,
    preferred_usd: float | None = None,
    minority_interest_usd: float | None = None,
    provider_ev_usd: float | None = None,
    *,
    debt_field_available: bool = True,
    cash_field_available: bool = True,
) -> EnterpriseValue:
    """``EV = MarketCap + Debt + Preferred + Minority - Cash`` (all in USD).

    Preferred stock and minority interest genuinely absent from a balance sheet
    are treated as zero -- that is a real economic statement.  A *missing debt
    or cash field* is not: assuming zero net debt would quietly flatter every
    company whose balance sheet failed to download, so the calculation is
    abandoned and the provider's own EV is used instead.
    """
    result = EnterpriseValue(provider=provider_ev_usd)

    can_calculate = (
        market_cap_usd is not None
        and debt_field_available
        and cash_field_available
        and total_debt_usd is not None
        and cash_usd is not None
    )
    if can_calculate:
        preferred = 0.0 if preferred_usd is None else float(preferred_usd)
        minority = 0.0 if minority_interest_usd is None else float(minority_interest_usd)
        if preferred_usd is None:
            result.flags.append("preferred_stock_assumed_zero")
        if minority_interest_usd is None:
            result.flags.append("minority_interest_assumed_zero")
        result.calculated = (
            float(market_cap_usd) + float(total_debt_usd) + preferred + minority - float(cash_usd)
        )
        result.value, result.source = result.calculated, "calculated"
        return result

    if not debt_field_available or total_debt_usd is None:
        result.flags.append("total_debt_unavailable")
    if not cash_field_available or cash_usd is None:
        result.flags.append("cash_unavailable")

    if provider_ev_usd is not None:
        result.value, result.source = float(provider_ev_usd), "provider"
        result.flags.append("ev_from_provider_fallback")
    return result


def fcf_yield(expected_fcf_usd: float | None, enterprise_value_usd: float | None) -> float | None:
    """``ExpectedFCF / EV``.  Undefined for a non-positive enterprise value."""
    if expected_fcf_usd is None or enterprise_value_usd is None:
        return None
    if enterprise_value_usd <= 0:
        return None
    return float(expected_fcf_usd) / float(enterprise_value_usd)


def weight_fcf_yield(raw_yield: float | None, config: FactorConfig = CONFIG) -> float | None:
    """FCF yield capped for weighting purposes only -- never for ranking."""
    if raw_yield is None:
        return None
    return min(float(raw_yield), config.FCF_YIELD_WEIGHT_CAP)
