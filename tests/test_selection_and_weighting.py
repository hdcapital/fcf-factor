"""The two-stage funnel, deterministic ties, weighting and the cap waterfall."""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest

from fcf_factor.config import CONFIG
from fcf_factor.factor.selection import rank_by_fcf_yield, rank_by_growth, two_stage_selection
from fcf_factor.factor.weighting import apply_caps, build_weights, cube_root, raw_weight


@dataclass
class Candidate:
    ticker: str
    fcf_yield: float | None
    growth_score: float | None


def _universe(n: int) -> list[Candidate]:
    # Yield falls as the index rises; growth rises as the index rises, so the
    # two screens genuinely disagree.
    return [Candidate(f"T{i:03d}", 0.50 - i * 0.001, -1.0 + i * 0.01) for i in range(n)]


def test_funnel_sizes_follow_the_configured_proportions():
    result = two_stage_selection(_universe(100))
    assert result.eligible_count == 100
    assert result.n_value == math.ceil(100 * CONFIG.VALUE_SCREEN_PERCENTILE)  # 19
    assert len(result.value_shortlist) == 19
    assert len(result.selected) == math.ceil(19 * CONFIG.GROWTH_KEEP_RATIO)  # 13
    # ~12.5% of the eligible universe, as designed.
    assert 0.11 <= len(result.selected) / 100 <= 0.14


def test_stage_one_ranks_on_yield_and_stage_two_on_growth():
    result = two_stage_selection(_universe(100))
    shortlist = {c.ticker for c in result.value_shortlist}
    # The 19 cheapest names are T000..T018.
    assert shortlist == {f"T{i:03d}" for i in range(19)}
    # Within those, the best growth scores are the highest indices.
    assert result.selected[0].ticker == "T018"


def test_screens_are_not_blended_into_a_single_score():
    """The very cheapest name is dropped when its growth is worst in the shortlist."""
    result = two_stage_selection(_universe(100))
    assert result.value_shortlist[0].ticker == "T000"
    assert "T000" not in {c.ticker for c in result.selected}


def test_ties_break_deterministically_and_end_on_the_ticker():
    tied = [
        Candidate("ZZZ", 0.10, 0.5),
        Candidate("AAA", 0.10, 0.5),
        Candidate("MMM", 0.10, 0.5),
    ]
    assert [c.ticker for c in rank_by_fcf_yield(tied)] == ["AAA", "MMM", "ZZZ"]
    assert [c.ticker for c in rank_by_growth(tied)] == ["AAA", "MMM", "ZZZ"]


def test_yield_ties_are_broken_by_growth_before_the_ticker():
    tied = [
        Candidate("AAA", 0.10, 0.1),
        Candidate("BBB", 0.10, 0.9),
    ]
    assert [c.ticker for c in rank_by_fcf_yield(tied)] == ["BBB", "AAA"]


def test_selection_is_reproducible_regardless_of_input_order():
    universe = _universe(60)
    forward = [c.ticker for c in two_stage_selection(universe).selected]
    backward = [c.ticker for c in two_stage_selection(list(reversed(universe))).selected]
    assert forward == backward


def test_missing_scores_sort_last_without_crashing():
    candidates = [Candidate("AAA", None, None), Candidate("BBB", 0.2, 0.3)]
    assert rank_by_fcf_yield(candidates)[0].ticker == "BBB"
    assert rank_by_growth(candidates)[0].ticker == "BBB"


def test_empty_universe_selects_nothing():
    result = two_stage_selection([])
    assert result.selected == []
    assert result.eligible_count == 0


def test_tiny_universe_still_selects_at_least_one_name():
    result = two_stage_selection(_universe(3))
    assert len(result.selected) >= 1


# --------------------------------------------------------------------------
# Weighting
# --------------------------------------------------------------------------
def test_cube_root_handles_negatives_without_producing_nan():
    assert cube_root(27.0) == pytest.approx(3.0)
    assert cube_root(-27.0) == pytest.approx(-3.0)


def test_raw_weight_uses_the_capped_yield_and_the_cube_root_of_expected_fcf():
    assert raw_weight(0.10, 8.0) == pytest.approx(0.10 * 2.0)
    # The yield cap binds above 15%.
    assert raw_weight(0.40, 8.0) == pytest.approx(CONFIG.FCF_YIELD_WEIGHT_CAP * 2.0)


def test_raw_weight_is_none_for_a_non_positive_expected_fcf():
    assert raw_weight(0.10, 0.0) is None
    assert raw_weight(0.10, -5.0) is None
    assert raw_weight(None, 8.0) is None


def test_weights_sum_to_one_hundred_percent():
    raw = {f"T{i}": float(i + 1) for i in range(50)}
    sectors = {f"T{i}": f"S{i % 6}" for i in range(50)}
    result = apply_caps(raw, sectors)
    assert sum(result.weights.values()) == pytest.approx(1.0, abs=1e-12)


def test_stock_cap_is_respected():
    raw = {f"T{i}": (1000.0 if i == 0 else 1.0) for i in range(60)}
    sectors = {f"T{i}": f"S{i % 8}" for i in range(60)}
    result = apply_caps(raw, sectors)
    assert max(result.weights.values()) <= CONFIG.MAX_STOCK_WEIGHT + 1e-9
    assert result.weights["T0"] == pytest.approx(CONFIG.MAX_STOCK_WEIGHT)


def test_sector_cap_is_respected_and_excess_is_redistributed():
    """One sector attracts almost all the raw weight and must be cut to 35%."""
    raw = {f"T{i}": (100.0 if i < 20 else 1.0) for i in range(60)}
    sectors = {f"T{i}": ("Heavy" if i < 20 else f"Light{i % 4}") for i in range(60)}
    result = apply_caps(raw, sectors)
    assert result.effective_sector_cap == pytest.approx(CONFIG.MAX_SECTOR_WEIGHT)
    assert result.sector_weights["Heavy"] <= CONFIG.MAX_SECTOR_WEIGHT + 1e-6
    assert result.sector_weights["Heavy"] == pytest.approx(CONFIG.MAX_SECTOR_WEIGHT, abs=1e-6)
    assert max(result.weights.values()) <= CONFIG.MAX_STOCK_WEIGHT + 1e-9
    assert sum(result.weights.values()) == pytest.approx(1.0, abs=1e-12)


def test_sector_cap_relaxes_when_the_stock_cap_makes_it_infeasible():
    """Caps that cannot jointly hold 100% are relaxed, loudly, not ignored."""
    raw = {f"T{i}": 1.0 for i in range(60)}
    # Five small sectors can only hold 12% each at a 3% stock cap.
    sectors = {f"T{i}": ("Heavy" if i < 40 else f"Light{i % 5}") for i in range(60)}
    result = apply_caps(raw, sectors)
    assert result.effective_sector_cap > CONFIG.MAX_SECTOR_WEIGHT
    assert any("sector cap relaxed" in note for note in result.notes)
    assert sum(result.weights.values()) == pytest.approx(1.0, abs=1e-12)


def test_stock_cap_relaxes_when_it_is_arithmetically_impossible():
    raw = {"A": 1.0, "B": 2.0, "C": 3.0}
    result = apply_caps(raw, {"A": "X", "B": "Y", "C": "Z"})
    assert result.effective_stock_cap == pytest.approx(1 / 3)
    assert sum(result.weights.values()) == pytest.approx(1.0)
    assert any("stock cap relaxed" in note for note in result.notes)


def test_sector_cap_relaxes_when_every_holding_is_in_one_sector():
    raw = {f"T{i}": 1.0 for i in range(40)}
    sectors = {f"T{i}": "Only" for i in range(40)}
    result = apply_caps(raw, sectors)
    assert result.effective_sector_cap == pytest.approx(1.0)
    assert sum(result.weights.values()) == pytest.approx(1.0)
    assert any("sector cap relaxed" in note for note in result.notes)


def test_weights_are_never_negative():
    raw = {f"T{i}": float(i + 1) for i in range(40)}
    result = apply_caps(raw, {f"T{i}": f"S{i % 4}" for i in range(40)})
    assert min(result.weights.values()) >= 0.0


def test_build_weights_drops_candidates_with_no_positive_raw_weight():
    result = build_weights(
        ["A", "B", "C"],
        {"A": 0.1, "B": 0.1, "C": None},
        {"A": 8.0, "B": 27.0, "C": 5.0},
        {"A": "X", "B": "Y", "C": "Z"},
    )
    assert set(result.weights) == {"A", "B"}
    assert sum(result.weights.values()) == pytest.approx(1.0)


def test_larger_expected_fcf_earns_a_larger_weight_at_equal_yield():
    """Before any cap binds, the cube-root term tilts towards more cash."""
    tickers = [f"T{i}" for i in range(60)]
    yields = dict.fromkeys(tickers, 0.10)
    expected = dict.fromkeys(tickers, 1000000.0)
    expected["T0"] = 1_000_000_000.0
    sectors = {t: f"S{i % 6}" for i, t in enumerate(tickers)}
    result = build_weights(tickers, yields, expected, sectors)
    assert result.raw_weights["T0"] > result.raw_weights["T1"]
    assert result.weights["T0"] > result.weights["T1"]
