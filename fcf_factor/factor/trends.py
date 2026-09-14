"""Ordinary-least-squares trend metrics used by the growth/quality score.

Each trend is a *scale-free* slope:

.. code-block:: text

    Trend = OLS slope of the annual series / mean(|annual series|)

Dividing by the mean absolute level is what makes a NZ$40m company comparable
with a US$40bn one.  The slope is computed against the observation index
(0, 1, 2, ...) rather than calendar time, so an irregular reporting history
still produces a usable per-period slope.
"""

from __future__ import annotations


def ols_slope(values: list[float]) -> float | None:
    """Least-squares slope of ``values`` against their index.

    Returns ``None`` for fewer than two points or a degenerate x-variance.
    """
    n = len(values)
    if n < 2:
        return None
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values, strict=True))
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return None
    return numerator / denominator


def normalized_trend(values: list[float | None], min_points: int = 3) -> float | None:
    """Scale-free OLS trend of an annual series given oldest-first.

    ``None`` when there are too few valid points or the series averages to
    zero, in which case the caller omits the component instead of inventing it.
    """
    clean = [float(v) for v in values if v is not None]
    if len(clean) < min_points:
        return None
    slope = ols_slope(clean)
    if slope is None:
        return None
    scale = sum(abs(v) for v in clean) / len(clean)
    if scale <= 0:
        return None
    return slope / scale


def per_share_series(
    values: list[float | None], shares: list[float | None]
) -> list[float | None]:
    """Element-wise ``value / shares``, ``None`` wherever either side is missing."""
    out: list[float | None] = []
    # The two series can differ in length when a provider omits share counts
    # for some periods; pairing stops at the shorter one.
    for value, share_count in zip(values, shares, strict=False):
        if value is None or share_count is None or share_count <= 0:
            out.append(None)
        else:
            out.append(float(value) / float(share_count))
    return out
