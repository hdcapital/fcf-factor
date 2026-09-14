"""Free cash flow, margins and the TTM fallback ladder."""

from __future__ import annotations

from datetime import date

import pytest

from fcf_factor.factor.fcf import (
    annual_fcf_margins,
    build_ttm,
    capex_sign_flag,
    fcf_margin,
    free_cash_flow,
)
from fcf_factor.providers.base import CompanyMetadata
from tests.conftest import make_annual, make_quarter


def test_fcf_with_negative_capex_yahoo_convention():
    # Yahoo reports CapEx as a negative cash outflow.
    assert free_cash_flow(1_000.0, -300.0) == 700.0


def test_fcf_with_positive_capex_other_convention():
    # Some sources report the same outflow as a positive number.
    assert free_cash_flow(1_000.0, 300.0) == 700.0


def test_fcf_is_none_when_an_input_is_missing():
    assert free_cash_flow(None, -300.0) is None
    assert free_cash_flow(1_000.0, None) is None


def test_positive_capex_is_flagged_for_review():
    assert capex_sign_flag(300.0) == "capex_reported_positive"
    assert capex_sign_flag(-300.0) is None
    assert capex_sign_flag(None) is None


def test_fcf_margin_requires_positive_revenue():
    assert fcf_margin(200.0, 1_000.0) == pytest.approx(0.2)
    assert fcf_margin(200.0, 0.0) is None
    assert fcf_margin(200.0, -50.0) is None


def test_annual_fcf_margins_are_oldest_first():
    annual = [
        make_annual(date(2024, 12, 31), 200.0, 40.0, -10.0),
        make_annual(date(2023, 12, 31), 100.0, 30.0, -10.0),
    ]
    margins = annual_fcf_margins(annual)
    assert [d.year for d, _ in margins] == [2023, 2024]
    assert margins[0][1] == pytest.approx(0.20)
    assert margins[1][1] == pytest.approx(0.15)


def _four_quarters():
    return [
        make_quarter(date(2025, 3, 31), 25.0, 6.0, -1.0),
        make_quarter(date(2025, 6, 30), 25.0, 6.0, -1.0),
        make_quarter(date(2025, 9, 30), 25.0, 6.0, -1.0),
        make_quarter(date(2025, 12, 31), 25.0, 6.0, -1.0),
    ]


def test_ttm_prefers_the_sum_of_four_quarters():
    ttm = build_ttm(_four_quarters(), [make_annual(date(2024, 12, 31), 90.0, 20.0, -4.0)])
    assert ttm.revenue == pytest.approx(100.0)
    assert ttm.revenue_source == "quarterly_sum"
    assert ttm.fcf == pytest.approx(20.0)
    assert ttm.fcf_source == "quarterly_sum"


def test_ttm_falls_back_to_provider_values_and_records_it():
    metadata = CompanyMetadata(
        symbol="X",
        ttm_revenue=111.0,
        ttm_ebitda=22.0,
        ttm_operating_cashflow=30.0,
        ttm_capex=-6.0,
    )
    ttm = build_ttm([], [make_annual(date(2024, 12, 31), 90.0, 20.0, -4.0)], metadata)
    assert ttm.revenue == pytest.approx(111.0)
    assert ttm.revenue_source == "provider_ttm"
    assert ttm.fcf == pytest.approx(24.0)
    assert ttm.fcf_source == "provider_ttm"
    assert "revenue_ttm_from_provider" in ttm.flags


def test_ttm_falls_back_to_the_latest_fiscal_year_last():
    annual = [make_annual(date(2024, 12, 31), 90.0, 20.0, -4.0)]
    ttm = build_ttm([], annual)
    assert ttm.revenue == pytest.approx(90.0)
    assert ttm.revenue_source == "latest_fiscal_year"
    assert ttm.fcf == pytest.approx(16.0)
    assert ttm.fcf_source == "latest_fiscal_year"
    assert "fcf_ttm_from_latest_fy" in ttm.flags


def test_ttm_rejects_a_quarter_window_that_does_not_span_a_year():
    quarters = [
        make_quarter(date(2025, 10, 1), 25.0, 6.0, -1.0),
        make_quarter(date(2025, 10, 15), 25.0, 6.0, -1.0),
        make_quarter(date(2025, 11, 1), 25.0, 6.0, -1.0),
        make_quarter(date(2025, 12, 31), 25.0, 6.0, -1.0),
    ]
    ttm = build_ttm(quarters, [make_annual(date(2024, 12, 31), 90.0, 20.0, -4.0)])
    assert ttm.revenue_source == "latest_fiscal_year"
    assert "ttm_quarter_window_unusable" in ttm.flags


def test_ttm_requires_all_four_quarters_to_carry_the_field():
    quarters = _four_quarters()
    quarters[1] = make_quarter(date(2025, 6, 30), None, 6.0, -1.0)
    ttm = build_ttm(quarters, [make_annual(date(2024, 12, 31), 90.0, 20.0, -4.0)])
    assert ttm.revenue_source == "latest_fiscal_year"
    # Cash flow was complete across all four quarters, so FCF still uses them.
    assert ttm.fcf_source == "quarterly_sum"
