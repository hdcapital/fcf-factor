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
    provider_ev_usd: float | None = None,
    *,
    debt_field_available: bool = True,
    cash_field_available: bool = True,
) -> EnterpriseValue:
    """``EV = MarketCap + Debt (incl. lease liabilities) + PreferredStock - Cash``.

    All inputs are in USD.  Minority interests are deliberately absent: they
    are handled by scaling free cash flow to the parent's share instead (see
    :mod:`fcf_factor.factor.nci`), so adding them here would count them twice.

    Preferred stock genuinely absent from a balance sheet is treated as zero.
    A *missing* debt or cash field is not: assuming zero net debt would quietly
    flatter every company whose balance sheet failed to download, so the
    calculation is abandoned and the provider's own EV is used instead.
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
        if preferred_usd is None:
            result.flags.append("preferred_stock_assumed_zero")
        result.calculated = float(market_cap_usd) + float(total_debt_usd) + preferred - float(cash_usd)
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


#: Relative tolerance when matching total debt against borrowings + leases.
DEBT_MATCH_TOLERANCE = 0.05


@dataclass
class DebtResolution:
    """Debt for enterprise value, with lease liabilities verified to be inside."""

    debt: float | None = None
    lease_liabilities: float | None = None
    treatment: str = "unknown"
    flags: list[str] = field(default_factory=list)


def _close(a: float, b: float) -> bool:
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) <= DEBT_MATCH_TOLERANCE * scale


def resolve_debt(
    total_debt: float | None,
    borrowings: float | None,
    lease_liabilities: float | None,
) -> DebtResolution:
    """Debt to put in enterprise value, treating lease liabilities as debt.

    Under IFRS 16 / ASC 842 a lease is a liability, and the principal repaid
    each year sits in *financing* cash flow, not operating.  So free cash flow
    (operating cash flow less capex) is already before lease repayments, and
    the matching denominator must carry the lease liability as debt.  Only then
    does a retailer that rents its stores compare fairly with one that owns
    them: one carries more debt, the other more capex.

    Providers are inconsistent about whether their "total debt" includes
    leases, so this function checks rather than assumes:

    * total debt ~= borrowings + leases  -> leases already included
    * total debt ~= borrowings           -> leases missing, so they are added
    * total debt smaller than the leases -> it cannot contain them, so added
    * otherwise                          -> assumed included, and flagged
    """
    result = DebtResolution(lease_liabilities=lease_liabilities)
    has_leases = lease_liabilities is not None and lease_liabilities > 0

    if total_debt is None:
        if borrowings is None and lease_liabilities is None:
            result.treatment = "debt_unavailable"
            return result
        result.debt = (borrowings or 0.0) + (lease_liabilities or 0.0)
        result.treatment = "rebuilt_from_borrowings_and_leases"
        result.flags.append("total_debt_rebuilt")
        if lease_liabilities is None:
            result.flags.append("lease_liabilities_not_reported")
        return result

    if lease_liabilities is None:
        result.debt = float(total_debt)
        result.treatment = "leases_not_reported"
        result.flags.append("lease_liabilities_not_reported")
        return result

    if not has_leases:
        result.debt = float(total_debt)
        result.treatment = "no_leases"
        return result

    if borrowings is not None:
        if _close(total_debt, borrowings + lease_liabilities):
            result.debt = float(total_debt)
            result.treatment = "leases_included_verified"
            return result
        if _close(total_debt, borrowings):
            result.debt = float(total_debt) + float(lease_liabilities)
            result.treatment = "leases_added"
            result.flags.append("lease_liabilities_added_to_debt")
            return result

    if total_debt < lease_liabilities * (1.0 - DEBT_MATCH_TOLERANCE):
        result.debt = float(total_debt) + float(lease_liabilities)
        result.treatment = "leases_added"
        result.flags.append("lease_liabilities_added_to_debt")
        return result

    result.debt = float(total_debt)
    result.treatment = "leases_included_assumed"
    result.flags.append("lease_inclusion_assumed")
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
