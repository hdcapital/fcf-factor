"""The two-stage selection funnel.

VFLO screens on valuation first and quality second, and keeps the two separate
rather than blending them into one score.  This implementation follows that:

.. code-block:: text

    N        = fully eligible companies in the market
    n_value  = ceil(N * 0.1875)        # cheapest on FCF yield
    n_final  = ceil(n_value * 2/3)     # best growth/quality within those
             ~= N * 0.125

Both stages sort on an explicit, fully deterministic key so that re-running the
same snapshot always produces exactly the same portfolio.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..config import CONFIG, FactorConfig


class Rankable(Protocol):
    ticker: str
    fcf_yield: float | None
    growth_score: float | None


@dataclass
class SelectionResult:
    eligible_count: int
    value_shortlist: list[Any]
    selected: list[Any]
    n_value: int
    n_final: int


def _value_key(item: Any) -> tuple:
    """Stage-1 ordering: FCF yield desc, growth score desc, ticker asc."""
    return (
        -(item.fcf_yield if item.fcf_yield is not None else -math.inf),
        -(item.growth_score if item.growth_score is not None else -math.inf),
        str(item.ticker),
    )


def _growth_key(item: Any) -> tuple:
    """Stage-2 ordering: growth score desc, FCF yield desc, ticker asc."""
    return (
        -(item.growth_score if item.growth_score is not None else -math.inf),
        -(item.fcf_yield if item.fcf_yield is not None else -math.inf),
        str(item.ticker),
    )


def rank_by_fcf_yield(items: Sequence[Any]) -> list[Any]:
    return sorted(items, key=_value_key)


def rank_by_growth(items: Sequence[Any]) -> list[Any]:
    return sorted(items, key=_growth_key)


def two_stage_selection(items: Sequence[Any], config: FactorConfig = CONFIG) -> SelectionResult:
    """Run both screens over the eligible companies of a single market."""
    eligible = list(items)
    n = len(eligible)
    if n == 0:
        return SelectionResult(0, [], [], 0, 0)

    n_value = max(1, math.ceil(n * config.VALUE_SCREEN_PERCENTILE))
    n_value = min(n_value, n)
    shortlist = rank_by_fcf_yield(eligible)[:n_value]

    n_final = max(1, math.ceil(n_value * config.GROWTH_KEEP_RATIO))
    n_final = min(n_final, len(shortlist))
    selected = rank_by_growth(shortlist)[:n_final]

    return SelectionResult(
        eligible_count=n,
        value_shortlist=shortlist,
        selected=selected,
        n_value=n_value,
        n_final=n_final,
    )
