"""The cross-sectional stage: z-scores, ranks, selection and weights.

Everything here happens *within a single market*.  The five markets never share
a cross-section, a rank or a portfolio.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..config import CONFIG, FactorConfig
from .engine import CompanyFactors
from .selection import SelectionResult, rank_by_fcf_yield, rank_by_growth, two_stage_selection
from .weighting import WeightingResult, build_weights
from .zscores import growth_score, zscores


@dataclass
class MarketFactorResult:
    market: str
    eligible: list[CompanyFactors] = field(default_factory=list)
    shortlist: list[CompanyFactors] = field(default_factory=list)
    selected: list[CompanyFactors] = field(default_factory=list)
    selection: SelectionResult | None = None
    weighting: WeightingResult | None = None
    notes: list[str] = field(default_factory=list)


def score_cross_section(eligible: Sequence[CompanyFactors], config: FactorConfig = CONFIG) -> None:
    """Attach z-scores and the growth score, in place, across the whole market.

    This runs over every eligible company *before* the FCF-yield screen, so the
    growth score always measures a company against its full market rather than
    against the value shortlist it happened to land in.
    """
    items = list(eligible)
    if not items:
        return
    z_revenue = zscores([c.revenue_trend for c in items], config)
    z_ebitda = zscores([c.ebitda_trend for c in items], config)
    z_fcf_ps = zscores([c.fcf_per_share_trend for c in items], config)
    for company, zr, ze, zf in zip(items, z_revenue, z_ebitda, z_fcf_ps, strict=True):
        company.z_revenue = zr
        company.z_ebitda = ze
        company.z_fcf_per_share = zf
        company.growth_score = growth_score([zr, ze, zf])


def run_market_factor(
    market: str,
    eligible: Sequence[CompanyFactors],
    config: FactorConfig = CONFIG,
) -> MarketFactorResult:
    """Score, rank, select and weight one market's eligible companies."""
    result = MarketFactorResult(market=market, eligible=list(eligible))
    score_cross_section(result.eligible, config)

    # Ranks are recorded for every eligible company, not just the survivors,
    # so the snapshot shows where each name sat in the cross-section.
    for position, company in enumerate(rank_by_fcf_yield(result.eligible), start=1):
        company.fcf_yield_rank = position
    for position, company in enumerate(rank_by_growth(result.eligible), start=1):
        company.growth_rank = position

    selection = two_stage_selection(result.eligible, config)
    result.selection = selection
    result.shortlist = list(selection.value_shortlist)
    result.selected = list(selection.selected)

    weighting = build_weights(
        [c.ticker for c in result.selected],
        {c.ticker: c.fcf_yield for c in result.selected},
        {c.ticker: c.expected_fcf_usd for c in result.selected},
        {c.ticker: (c.sector or "Unknown") for c in result.selected},
        config,
    )
    result.weighting = weighting
    result.notes.extend(weighting.notes)
    for company in result.selected:
        company.raw_weight = weighting.raw_weights.get(company.ticker)
        company.target_weight = weighting.weights.get(company.ticker)

    # A selected company that could not be weighted would silently disappear
    # from the portfolio, so drop it explicitly instead.
    unweighted = [c for c in result.selected if not c.target_weight]
    if unweighted:
        result.notes.append(
            "dropped from portfolio (no positive raw weight): "
            + ", ".join(sorted(c.ticker for c in unweighted))
        )
        result.selected = [c for c in result.selected if c.target_weight]
    result.selected.sort(key=lambda c: (-(c.target_weight or 0.0), c.ticker))
    return result
