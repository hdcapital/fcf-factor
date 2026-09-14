"""Cross-sectional z-scores, computed independently inside each market.

There is deliberately no global pool: an Australian miner is scored against
other Australian companies, never against Nasdaq software.  Mixing the five
markets would make the growth score a country bet.
"""

from __future__ import annotations

import statistics

from ..config import CONFIG, FactorConfig


def zscores(values: list[float | None], config: FactorConfig = CONFIG) -> list[float | None]:
    """Standardise a cross-section, clipping each score to +/- ``Z_SCORE_CLIP``.

    ``None`` inputs stay ``None`` (a missing trend is not a zero trend).  A
    zero-variance cross-section yields 0.0 for every valid observation.
    """
    valid = [float(v) for v in values if v is not None]
    if len(valid) < 2:
        return [None if v is None else 0.0 for v in values]
    mean = statistics.fmean(valid)
    stdev = statistics.pstdev(valid)
    limit = config.Z_SCORE_CLIP
    if stdev <= 0:
        return [None if v is None else 0.0 for v in values]
    out: list[float | None] = []
    for value in values:
        if value is None:
            out.append(None)
            continue
        z = (float(value) - mean) / stdev
        out.append(max(-limit, min(limit, z)))
    return out


def growth_score(components: list[float | None]) -> float | None:
    """Mean of the available z-scores; ``None`` when none are available."""
    valid = [c for c in components if c is not None]
    if not valid:
        return None
    return sum(valid) / len(valid)
