"""Normalised margin, mechanical forward growth, and expected free cash flow.

VFLO uses sell-side consensus forecasts.  Small caps in AU, NZ, UK and Canada
frequently have none, so this system substitutes a purely mechanical estimate
built from reported revenue history.  It is not a better forecast than an
analyst's -- it is a *reproducible* one, which is what a prospective experiment
needs.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from ..config import CONFIG, FactorConfig


def normalized_fcf_margin(
    annual_margins: list[float | None],
    ttm_margin: float | None = None,
    lookback_years: int = 3,
) -> tuple[float | None, int]:
    """Median of the latest fiscal-year margins and the TTM margin.

    ``annual_margins`` is oldest-first.  Returns ``(median, n_observations)``;
    the count is stored in the snapshot so thin histories are visible.

    Using a median rather than a mean is the entire point: one year of unusual
    working-capital movement should not set a company's valuation.
    """
    recent = [m for m in annual_margins[-lookback_years:] if m is not None]
    observations = list(recent)
    if ttm_margin is not None:
        observations.append(ttm_margin)
    if not observations:
        return None, 0
    return statistics.median(observations), len(observations)


def revenue_cagr(revenues: list[float | None]) -> float | None:
    """Compound annual growth rate across the available annual revenues.

    ``(latest / earliest) ** (1 / years) - 1`` where ``years`` is the number of
    intervals, not observations.  Undefined when the earliest or latest revenue
    is non-positive -- a sign change cannot produce a meaningful growth rate.
    """
    values = [v for v in revenues if v is not None]
    if len(values) < 2:
        return None
    earliest, latest = float(values[0]), float(values[-1])
    years = len(values) - 1
    if earliest <= 0 or latest <= 0 or years <= 0:
        return None
    return (latest / earliest) ** (1.0 / years) - 1.0


def latest_yoy_growth(revenues: list[float | None]) -> float | None:
    """Most recent year-on-year revenue growth."""
    values = [v for v in revenues if v is not None]
    if len(values) < 2:
        return None
    prior, latest = float(values[-2]), float(values[-1])
    if prior <= 0:
        return None
    return latest / prior - 1.0


def clamp_growth(g: float, config: FactorConfig = CONFIG) -> float:
    """Clamp the growth estimate into the configured band."""
    return max(config.FORWARD_GROWTH_MIN, min(config.FORWARD_GROWTH_MAX, g))


@dataclass
class ForwardGrowth:
    """The mechanical forward revenue growth estimate and how it was built."""

    growth: float | None = None
    raw_growth: float | None = None
    cagr: float | None = None
    yoy: float | None = None
    was_clamped: bool = False
    flags: list[str] = field(default_factory=list)


def forward_growth(revenues: list[float | None], config: FactorConfig = CONFIG) -> ForwardGrowth:
    """Blend the revenue CAGR and the latest year-on-year growth, then clamp.

    ``g = 0.60 * CAGR + 0.40 * YoY``, clamped to ``[-15%, +30%]``.

    When only one of the two components can be computed, that component is used
    on its own and the fallback is flagged rather than the company being
    silently dropped.
    """
    result = ForwardGrowth()
    result.cagr = revenue_cagr(revenues)
    result.yoy = latest_yoy_growth(revenues)

    if result.cagr is not None and result.yoy is not None:
        raw = config.FORWARD_GROWTH_CAGR_WEIGHT * result.cagr + config.FORWARD_GROWTH_YOY_WEIGHT * result.yoy
    elif result.cagr is not None:
        raw = result.cagr
        result.flags.append("growth_from_cagr_only")
    elif result.yoy is not None:
        raw = result.yoy
        result.flags.append("growth_from_yoy_only")
    else:
        result.flags.append("growth_unavailable")
        return result

    result.raw_growth = raw
    result.growth = clamp_growth(raw, config)
    result.was_clamped = result.growth != raw
    if result.was_clamped:
        result.flags.append(
            "growth_clamped_low" if result.growth == config.FORWARD_GROWTH_MIN else "growth_clamped_high"
        )
    return result


def forward_revenue(ttm_revenue: float | None, growth: float | None) -> float | None:
    if ttm_revenue is None or growth is None:
        return None
    return float(ttm_revenue) * (1.0 + float(growth))


def forward_fcf(forward_revenue_value: float | None, normalized_margin: float | None) -> float | None:
    if forward_revenue_value is None or normalized_margin is None:
        return None
    return float(forward_revenue_value) * float(normalized_margin)


def expected_fcf(
    current_ttm_fcf: float | None,
    forward_fcf_value: float | None,
    config: FactorConfig = CONFIG,
) -> float | None:
    """``0.50 * current TTM FCF + 0.50 * forward FCF``.

    Both legs are required: a company without a forward estimate has no factor
    score, and is excluded with an explicit reason rather than being scored on
    half the formula.
    """
    if current_ttm_fcf is None or forward_fcf_value is None:
        return None
    return config.CURRENT_FCF_WEIGHT * float(current_ttm_fcf) + config.FORWARD_FCF_WEIGHT * float(
        forward_fcf_value
    )
