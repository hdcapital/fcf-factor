"""Portfolio weighting and the iterative cap waterfall.

.. code-block:: text

    RawWeight = min(FCFYield, 15%) * cbrt(ExpectedFCF_USD)

The cube root tilts the portfolio towards companies producing more absolute
cash without letting the largest name dominate, and the capped yield stops a
single suspicious 40% yield from buying a maximum position.  Raw weights are
normalised to 100% and then pushed through per-stock and per-sector caps.

Caps are enforced by water-filling rather than by clipping and renormalising:
clipping and renormalising re-inflates the very names that were just capped,
which is a classic way to end up with a "3% cap" portfolio holding 3.4%.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ..config import CONFIG, FactorConfig
from ..logging_utils import get_logger

log = get_logger(__name__)

TOLERANCE = 1e-12
UNKNOWN_SECTOR = "Unknown"


def cube_root(value: float) -> float:
    """Real cube root, defined for negatives (unused, but no silent NaN)."""
    if value < 0:
        return -((-value) ** (1.0 / 3.0))
    return value ** (1.0 / 3.0)


def raw_weight(fcf_yield: float | None, expected_fcf_usd: float | None, config: FactorConfig = CONFIG) -> float | None:
    """``min(FCFYield, cap) * cbrt(ExpectedFCF_USD)``."""
    if fcf_yield is None or expected_fcf_usd is None:
        return None
    if expected_fcf_usd <= 0:
        return None
    capped_yield = min(float(fcf_yield), config.FCF_YIELD_WEIGHT_CAP)
    if capped_yield <= 0:
        return None
    return capped_yield * cube_root(float(expected_fcf_usd))


@dataclass
class WeightingResult:
    weights: dict[str, float] = field(default_factory=dict)
    raw_weights: dict[str, float] = field(default_factory=dict)
    effective_stock_cap: float = 1.0
    effective_sector_cap: float = 1.0
    sector_weights: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _waterfill(weights: dict[str, float], caps: Mapping[str, float], total: float) -> dict[str, float]:
    """Scale ``weights`` to sum to ``total`` without exceeding any cap."""
    names = list(weights)
    if not names:
        return {}
    free = set(names)
    fixed: dict[str, float] = {}
    for _ in range(len(names) + 2):
        remaining = total - sum(fixed.values())
        if not free:
            break
        free_sum = sum(max(0.0, weights[n]) for n in free)
        if free_sum <= 0:
            share = remaining / len(free)
            for n in list(free):
                fixed[n] = min(share, caps[n])
                free.discard(n)
            break
        scale = remaining / free_sum
        breached = [n for n in free if max(0.0, weights[n]) * scale > caps[n] + TOLERANCE]
        if not breached:
            for n in free:
                fixed[n] = max(0.0, weights[n]) * scale
            free.clear()
            break
        for n in breached:
            fixed[n] = caps[n]
            free.discard(n)
    for n in free:  # pragma: no cover - defensive
        fixed[n] = min(max(0.0, weights[n]), caps[n])
    return {n: fixed.get(n, 0.0) for n in names}


def apply_caps(
    raw: Mapping[str, float],
    sectors: Mapping[str, str] | None = None,
    config: FactorConfig = CONFIG,
    max_iterations: int = 200,
) -> WeightingResult:
    """Normalise raw weights to 100% subject to stock and sector caps.

    Caps are relaxed automatically -- and loudly -- when they are arithmetically
    impossible.  A nine-stock portfolio cannot respect a 3% per-name cap, so the
    cap becomes ``1 / n`` and a note is recorded in the snapshot.
    """
    result = WeightingResult(raw_weights=dict(raw))
    names = [n for n, w in raw.items() if w is not None and w > 0]
    if not names:
        return result

    n = len(names)
    sector_map = {name: (sectors or {}).get(name) or UNKNOWN_SECTOR for name in names}

    stock_cap = config.MAX_STOCK_WEIGHT
    if stock_cap * n < 1.0 - TOLERANCE:
        relaxed = 1.0 / n
        result.notes.append(
            f"stock cap relaxed from {config.MAX_STOCK_WEIGHT:.4f} to {relaxed:.4f}: "
            f"{n} holdings cannot fill 100% at the configured cap"
        )
        log.warning(
            "relaxing MAX_STOCK_WEIGHT from %.4f to %.4f for a %d-stock portfolio",
            config.MAX_STOCK_WEIGHT,
            relaxed,
            n,
        )
        stock_cap = relaxed
    result.effective_stock_cap = stock_cap

    unique_sectors = sorted(set(sector_map.values()))
    sector_cap = config.MAX_SECTOR_WEIGHT
    capacity = sum(
        min(sector_cap, stock_cap * sum(1 for x in names if sector_map[x] == s)) for s in unique_sectors
    )
    if capacity < 1.0 - 1e-9:
        relaxed = 1.0
        if len(unique_sectors) > 1:
            relaxed = max(sector_cap, 1.0 / len(unique_sectors))
            # Grow until the caps can actually hold 100%.
            while relaxed < 1.0:
                capacity = sum(
                    min(relaxed, stock_cap * sum(1 for x in names if sector_map[x] == s))
                    for s in unique_sectors
                )
                if capacity >= 1.0 - 1e-9:
                    break
                relaxed = min(1.0, relaxed * 1.1 + 1e-6)
        result.notes.append(
            f"sector cap relaxed from {config.MAX_SECTOR_WEIGHT:.4f} to {relaxed:.4f}: "
            f"{len(unique_sectors)} sector(s) cannot fill 100% at the configured cap"
        )
        log.warning("relaxing MAX_SECTOR_WEIGHT to %.4f (%d sectors)", relaxed, len(unique_sectors))
        sector_cap = relaxed
    result.effective_sector_cap = sector_cap

    weights = {name: float(raw[name]) for name in names}
    caps = dict.fromkeys(names, stock_cap)
    weights = _waterfill(weights, caps, 1.0)

    for _ in range(max_iterations):
        sector_totals: dict[str, float] = {}
        for name, weight in weights.items():
            sector_totals[sector_map[name]] = sector_totals.get(sector_map[name], 0.0) + weight
        breached = {s: t for s, t in sector_totals.items() if t > sector_cap + 1e-10}
        if not breached:
            break
        freed = 0.0
        for sector, total in breached.items():
            scale = sector_cap / total
            for name in names:
                if sector_map[name] == sector:
                    freed += weights[name] * (1.0 - scale)
                    weights[name] *= scale
        receivers = [nm for nm in names if sector_map[nm] not in breached]
        if not receivers or freed <= 0:
            break
        receiver_caps = {}
        for sector in {sector_map[nm] for nm in receivers}:
            members = [nm for nm in receivers if sector_map[nm] == sector]
            room = max(0.0, sector_cap - sector_totals.get(sector, 0.0))
            member_total = sum(weights[nm] for nm in members)
            for nm in members:
                share = (weights[nm] / member_total) if member_total > 0 else 1.0 / len(members)
                receiver_caps[nm] = min(stock_cap, weights[nm] + room * share)
        sub = {nm: weights[nm] for nm in receivers}
        redistributed = _waterfill(sub, receiver_caps, sum(sub.values()) + freed)
        weights.update(redistributed)

    total = sum(weights.values())
    if total <= 0:  # pragma: no cover - defensive
        return result
    # Remove floating-point residue so the weights sum to 1 exactly.
    weights = {name: weight / total for name, weight in weights.items()}
    residual = 1.0 - sum(weights.values())
    if abs(residual) > 0:
        anchor = max(weights, key=lambda k: weights[k])
        weights[anchor] += residual

    result.weights = weights
    totals: dict[str, float] = {}
    for name, weight in weights.items():
        totals[sector_map[name]] = totals.get(sector_map[name], 0.0) + weight
    result.sector_weights = totals

    worst_stock = max(weights.values()) if weights else 0.0
    if worst_stock > stock_cap + 1e-6:
        result.notes.append(f"stock cap not fully satisfied: max weight {worst_stock:.4%}")
    worst_sector = max(totals.values()) if totals else 0.0
    if worst_sector > sector_cap + 1e-6:
        result.notes.append(f"sector cap not fully satisfied: max sector weight {worst_sector:.4%}")
    return result


def build_weights(
    tickers: Sequence[str],
    fcf_yields: Mapping[str, float | None],
    expected_fcf_usd: Mapping[str, float | None],
    sectors: Mapping[str, str] | None = None,
    config: FactorConfig = CONFIG,
) -> WeightingResult:
    """Convenience wrapper: raw weights, then caps, in one call."""
    raw = {}
    for ticker in tickers:
        value = raw_weight(fcf_yields.get(ticker), expected_fcf_usd.get(ticker), config)
        if value is not None and value > 0:
            raw[ticker] = value
    return apply_caps(raw, sectors, config)
