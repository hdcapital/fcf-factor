"""The FCF factor: pure computation, no I/O, no provider imports."""

from __future__ import annotations

from .cross_section import MarketFactorResult, run_market_factor, score_cross_section
from .engine import CompanyFactors, evaluate_company
from .fcf import TTMFinancials, build_ttm, fcf_margin, free_cash_flow
from .growth import (
    ForwardGrowth,
    clamp_growth,
    expected_fcf,
    forward_fcf,
    forward_growth,
    forward_revenue,
    latest_yoy_growth,
    normalized_fcf_margin,
    revenue_cagr,
)
from .quality import CoverageSummary, assess_market_cap
from .selection import two_stage_selection
from .trends import normalized_trend, ols_slope
from .valuation import calculate_enterprise_value, fcf_yield, weight_fcf_yield
from .weighting import apply_caps, build_weights, raw_weight
from .zscores import growth_score, zscores

__all__ = [
    "CompanyFactors",
    "CoverageSummary",
    "ForwardGrowth",
    "MarketFactorResult",
    "TTMFinancials",
    "apply_caps",
    "assess_market_cap",
    "build_ttm",
    "build_weights",
    "calculate_enterprise_value",
    "clamp_growth",
    "evaluate_company",
    "expected_fcf",
    "fcf_margin",
    "fcf_yield",
    "forward_fcf",
    "forward_growth",
    "forward_revenue",
    "free_cash_flow",
    "growth_score",
    "latest_yoy_growth",
    "normalized_fcf_margin",
    "normalized_trend",
    "ols_slope",
    "raw_weight",
    "revenue_cagr",
    "run_market_factor",
    "score_cross_section",
    "two_stage_selection",
    "weight_fcf_yield",
    "zscores",
]
