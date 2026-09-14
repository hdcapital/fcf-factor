"""Free cash flow: the definition, the margin, and the TTM fallback ladder.

Free cash flow is computed here rather than taken from the provider's own
``freeCashflow`` field, because providers disagree about what that field means
and about the sign of capital expenditure:

.. code-block:: text

    FCF = CFO - |CapEx|

Taking the absolute value is deliberate.  Yahoo reports CapEx as a negative
number on the cash-flow statement, but some tickers -- and some other providers
entirely -- report it positive.  ``abs()`` gives the same answer for both, and
:func:`capex_sign_flag` records when the reported sign was unusual so the
data-quality report can pick it up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..providers.base import CompanyMetadata, IncomeCashflowPeriod

#: Four consecutive quarter-end dates span roughly nine months end-to-end.
#: Anything far outside this range is not a clean trailing-twelve-month window.
MIN_TTM_SPAN_DAYS = 240
MAX_TTM_SPAN_DAYS = 400


def free_cash_flow(operating_cashflow: float | None, capex: float | None) -> float | None:
    """``CFO - |CapEx|``.  Returns ``None`` if either input is missing."""
    if operating_cashflow is None or capex is None:
        return None
    return float(operating_cashflow) - abs(float(capex))


def fcf_margin(fcf: float | None, revenue: float | None) -> float | None:
    """FCF divided by revenue.  Undefined for non-positive revenue."""
    if fcf is None or revenue is None:
        return None
    if revenue <= 0:
        return None
    return float(fcf) / float(revenue)


def capex_sign_flag(capex: float | None) -> str | None:
    """Flag a positive reported CapEx, which usually means an inverted sign."""
    if capex is None:
        return None
    if capex > 0:
        return "capex_reported_positive"
    return None


@dataclass
class TTMFinancials:
    """Trailing-twelve-month figures plus the provenance of each one.

    ``*_source`` is one of ``quarterly_sum``, ``provider_ttm`` or
    ``latest_fiscal_year`` and is written into every snapshot, so a reader can
    always tell which rung of the fallback ladder produced a number.
    """

    revenue: float | None = None
    revenue_source: str | None = None
    ebitda: float | None = None
    ebitda_source: str | None = None
    operating_cashflow: float | None = None
    capex: float | None = None
    fcf: float | None = None
    fcf_source: str | None = None
    period_end: date | None = None
    flags: list[str] = field(default_factory=list)

    @property
    def margin(self) -> float | None:
        return fcf_margin(self.fcf, self.revenue)

    def sources(self) -> dict[str, str | None]:
        return {
            "revenue": self.revenue_source,
            "ebitda": self.ebitda_source,
            "fcf": self.fcf_source,
        }


def _latest_four_quarters(quarters: list[IncomeCashflowPeriod]) -> list[IncomeCashflowPeriod] | None:
    """The most recent four quarters, if they form a plausible TTM window."""
    if len(quarters) < 4:
        return None
    window = sorted(quarters, key=lambda p: p.period_end)[-4:]
    span = (window[-1].period_end - window[0].period_end).days
    if not (MIN_TTM_SPAN_DAYS <= span <= MAX_TTM_SPAN_DAYS):
        return None
    return window


def _sum_field(window: list[IncomeCashflowPeriod] | None, attr: str) -> float | None:
    """Sum an attribute across the window, requiring *all four* quarters."""
    if not window:
        return None
    values = [getattr(p, attr) for p in window]
    if any(v is None for v in values):
        return None
    return float(sum(values))


def build_ttm(
    quarterly: list[IncomeCashflowPeriod],
    annual: list[IncomeCashflowPeriod],
    metadata: CompanyMetadata | None = None,
) -> TTMFinancials:
    """Construct TTM figures using the documented fallback hierarchy.

    For every metric, in order:

    1. sum of the latest four quarterly statements,
    2. the provider's own trailing-twelve-month value,
    3. the most recent fiscal year.

    Each choice is recorded, never inferred silently.
    """
    result = TTMFinancials()
    window = _latest_four_quarters(sorted(quarterly, key=lambda p: p.period_end))
    latest_fy = sorted(annual, key=lambda p: p.period_end)[-1] if annual else None

    if window:
        result.period_end = window[-1].period_end
    elif latest_fy:
        result.period_end = latest_fy.period_end

    if quarterly and window is None:
        result.flags.append("ttm_quarter_window_unusable")

    # ---- revenue ---------------------------------------------------------
    revenue = _sum_field(window, "revenue")
    if revenue is not None:
        result.revenue, result.revenue_source = revenue, "quarterly_sum"
    elif metadata is not None and metadata.ttm_revenue is not None:
        result.revenue, result.revenue_source = float(metadata.ttm_revenue), "provider_ttm"
        result.flags.append("revenue_ttm_from_provider")
    elif latest_fy is not None and latest_fy.revenue is not None:
        result.revenue, result.revenue_source = float(latest_fy.revenue), "latest_fiscal_year"
        result.flags.append("revenue_ttm_from_latest_fy")

    # ---- EBITDA ----------------------------------------------------------
    ebitda = _sum_field(window, "ebitda")
    if ebitda is not None:
        result.ebitda, result.ebitda_source = ebitda, "quarterly_sum"
    elif metadata is not None and metadata.ttm_ebitda is not None:
        result.ebitda, result.ebitda_source = float(metadata.ttm_ebitda), "provider_ttm"
        result.flags.append("ebitda_ttm_from_provider")
    elif latest_fy is not None and latest_fy.ebitda is not None:
        result.ebitda, result.ebitda_source = float(latest_fy.ebitda), "latest_fiscal_year"
        result.flags.append("ebitda_ttm_from_latest_fy")

    # ---- free cash flow --------------------------------------------------
    cfo = _sum_field(window, "operating_cashflow")
    capex = _sum_field(window, "capex")
    if cfo is not None and capex is not None:
        result.operating_cashflow, result.capex = cfo, capex
        result.fcf, result.fcf_source = free_cash_flow(cfo, capex), "quarterly_sum"
    elif metadata is not None and metadata.ttm_operating_cashflow is not None and metadata.ttm_capex is not None:
        result.operating_cashflow = float(metadata.ttm_operating_cashflow)
        result.capex = float(metadata.ttm_capex)
        result.fcf = free_cash_flow(result.operating_cashflow, result.capex)
        result.fcf_source = "provider_ttm"
        result.flags.append("fcf_ttm_from_provider")
    elif metadata is not None and metadata.ttm_free_cashflow is not None:
        result.fcf, result.fcf_source = float(metadata.ttm_free_cashflow), "provider_ttm_fcf_field"
        result.flags.append("fcf_ttm_from_provider_fcf_field")
    elif latest_fy is not None:
        fy_fcf = free_cash_flow(latest_fy.operating_cashflow, latest_fy.capex)
        if fy_fcf is not None:
            result.operating_cashflow = latest_fy.operating_cashflow
            result.capex = latest_fy.capex
            result.fcf, result.fcf_source = fy_fcf, "latest_fiscal_year"
            result.flags.append("fcf_ttm_from_latest_fy")

    for period in (window or []):
        flag = capex_sign_flag(period.capex)
        if flag and flag not in result.flags:
            result.flags.append(flag)
    if latest_fy is not None:
        flag = capex_sign_flag(latest_fy.capex)
        if flag and flag not in result.flags:
            result.flags.append(flag)

    return result


def annual_fcf_series(annual: list[IncomeCashflowPeriod]) -> list[tuple[date, float | None]]:
    """FCF per annual period, oldest first (``None`` where inputs are missing)."""
    return [
        (p.period_end, free_cash_flow(p.operating_cashflow, p.capex))
        for p in sorted(annual, key=lambda p: p.period_end)
    ]


def annual_fcf_margins(annual: list[IncomeCashflowPeriod]) -> list[tuple[date, float | None]]:
    """FCF margin per annual period, oldest first."""
    out = []
    for p in sorted(annual, key=lambda p: p.period_end):
        out.append((p.period_end, fcf_margin(free_cash_flow(p.operating_cashflow, p.capex), p.revenue)))
    return out
