"""OLS trend metrics, per-share series and cross-sectional z-scores."""

from __future__ import annotations

import pytest

from fcf_factor.config import CONFIG
from fcf_factor.factor.trends import normalized_trend, ols_slope, per_share_series
from fcf_factor.factor.zscores import growth_score, zscores


def test_ols_slope_on_a_perfect_line():
    assert ols_slope([1.0, 2.0, 3.0, 4.0]) == pytest.approx(1.0)
    assert ols_slope([10.0, 8.0, 6.0]) == pytest.approx(-2.0)


def test_ols_slope_of_a_flat_series_is_zero():
    assert ols_slope([5.0, 5.0, 5.0]) == pytest.approx(0.0)


def test_ols_slope_needs_at_least_two_points():
    assert ols_slope([1.0]) is None
    assert ols_slope([]) is None


def test_normalized_trend_is_scale_free():
    """A company ten times larger with the same shape gets the same trend."""
    small = normalized_trend([100.0, 110.0, 120.0, 130.0])
    large = normalized_trend([1000.0, 1100.0, 1200.0, 1300.0])
    assert small == pytest.approx(large)


def test_normalized_trend_divides_slope_by_mean_absolute_level():
    # slope 10, mean |value| = 115
    assert normalized_trend([100.0, 110.0, 120.0, 130.0]) == pytest.approx(10.0 / 115.0)


def test_normalized_trend_requires_enough_points():
    assert normalized_trend([100.0, 110.0], min_points=3) is None
    assert normalized_trend([100.0, None, 120.0], min_points=3) is None


def test_normalized_trend_returns_none_for_an_all_zero_series():
    assert normalized_trend([0.0, 0.0, 0.0]) is None


def test_per_share_series_skips_missing_or_zero_share_counts():
    assert per_share_series([100.0, 200.0, 300.0], [10.0, None, 0.0]) == [10.0, None, None]


def test_zscores_are_clipped_to_the_configured_limit():
    values = [1.0] * 30 + [1_000_000.0]
    scores = zscores(values)
    assert max(scores) == pytest.approx(CONFIG.Z_SCORE_CLIP)
    assert min(scores) >= -CONFIG.Z_SCORE_CLIP


def test_zscores_preserve_missing_values_as_missing():
    scores = zscores([1.0, None, 3.0])
    assert scores[1] is None
    assert scores[0] < scores[2]


def test_zscores_of_a_constant_cross_section_are_zero():
    assert zscores([2.0, 2.0, 2.0]) == [0.0, 0.0, 0.0]


def test_zscores_are_centred_on_the_cross_sectional_mean():
    scores = zscores([-1.0, 0.0, 1.0])
    assert scores[1] == pytest.approx(0.0)
    assert scores[0] == pytest.approx(-scores[2])


def test_growth_score_averages_only_the_available_components():
    assert growth_score([1.0, None, 2.0]) == pytest.approx(1.5)
    assert growth_score([None, None]) is None
