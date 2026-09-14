"""Normalised margin, revenue CAGR, growth clamping and expected FCF."""

from __future__ import annotations

import pytest

from fcf_factor.config import CONFIG
from fcf_factor.factor.growth import (
    clamp_growth,
    expected_fcf,
    forward_fcf,
    forward_growth,
    forward_revenue,
    latest_yoy_growth,
    normalized_fcf_margin,
    revenue_cagr,
)


def test_normalized_margin_is_the_median_of_three_years_and_ttm():
    margin, count = normalized_fcf_margin([0.05, 0.10, 0.30], ttm_margin=0.12)
    # median(0.05, 0.10, 0.30, 0.12) = (0.10 + 0.12) / 2
    assert margin == pytest.approx(0.11)
    assert count == 4


def test_normalized_margin_ignores_a_one_off_spike():
    """A single blow-out year must not set the company's normalised margin."""
    steady, _ = normalized_fcf_margin([0.10, 0.11, 0.10], ttm_margin=0.10)
    spiked, _ = normalized_fcf_margin([0.10, 0.11, 0.95], ttm_margin=0.10)
    assert steady == pytest.approx(0.10)
    assert spiked == pytest.approx(0.105)


def test_normalized_margin_uses_only_the_latest_three_years():
    margin, count = normalized_fcf_margin([9.0, 0.10, 0.20, 0.30], ttm_margin=None)
    assert count == 3
    assert margin == pytest.approx(0.20)


def test_normalized_margin_handles_missing_observations():
    margin, count = normalized_fcf_margin([None, 0.10, None], ttm_margin=None)
    assert margin == pytest.approx(0.10)
    assert count == 1
    assert normalized_fcf_margin([None, None], None) == (None, 0)


def test_revenue_cagr_over_three_intervals():
    assert revenue_cagr([100.0, 110.0, 121.0, 133.1]) == pytest.approx(0.10)


def test_revenue_cagr_undefined_for_non_positive_endpoints():
    assert revenue_cagr([0.0, 110.0]) is None
    assert revenue_cagr([100.0, -5.0]) is None
    assert revenue_cagr([100.0]) is None


def test_latest_yoy_growth():
    assert latest_yoy_growth([100.0, 110.0, 121.0]) == pytest.approx(0.10)
    assert latest_yoy_growth([0.0, 110.0]) is None


def test_growth_blend_weights_cagr_and_yoy():
    revenues = [100.0, 110.0, 121.0, 133.1]
    result = forward_growth(revenues)
    # CAGR and YoY are both 10% here, so the blend is 10%.
    assert result.growth == pytest.approx(0.10)
    assert result.was_clamped is False


def test_growth_is_clamped_at_the_upper_bound():
    result = forward_growth([10.0, 40.0, 160.0, 640.0])
    assert result.growth == pytest.approx(CONFIG.FORWARD_GROWTH_MAX)
    assert result.was_clamped is True
    assert "growth_clamped_high" in result.flags


def test_growth_is_clamped_at_the_lower_bound():
    result = forward_growth([640.0, 160.0, 40.0, 10.0])
    assert result.growth == pytest.approx(CONFIG.FORWARD_GROWTH_MIN)
    assert "growth_clamped_low" in result.flags


def test_clamp_growth_is_inclusive_of_the_bounds():
    assert clamp_growth(0.0) == 0.0
    assert clamp_growth(5.0) == CONFIG.FORWARD_GROWTH_MAX
    assert clamp_growth(-5.0) == CONFIG.FORWARD_GROWTH_MIN


def test_growth_falls_back_to_a_single_component_and_flags_it():
    # Earliest revenue is zero, so the CAGR is undefined but YoY is not.
    result = forward_growth([0.0, 100.0, 120.0])
    assert result.cagr is None
    assert result.growth == pytest.approx(0.20)
    assert "growth_from_cagr_only" not in result.flags
    assert "growth_from_yoy_only" in result.flags


def test_growth_unavailable_when_history_is_too_short():
    result = forward_growth([100.0])
    assert result.growth is None
    assert "growth_unavailable" in result.flags


def test_forward_revenue_and_forward_fcf():
    assert forward_revenue(1_000.0, 0.10) == pytest.approx(1_100.0)
    assert forward_fcf(1_100.0, 0.12) == pytest.approx(132.0)
    assert forward_revenue(None, 0.10) is None
    assert forward_fcf(1_100.0, None) is None


def test_expected_fcf_is_a_fifty_fifty_blend():
    assert expected_fcf(100.0, 140.0) == pytest.approx(120.0)


def test_expected_fcf_requires_both_legs():
    assert expected_fcf(None, 140.0) is None
    assert expected_fcf(100.0, None) is None


def test_expected_fcf_can_be_negative_and_is_not_silently_floored():
    assert expected_fcf(-100.0, -40.0) == pytest.approx(-70.0)
