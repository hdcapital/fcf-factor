"""Market -> adapter registry, plus the safety net around universe refreshes.

Two safeguards live here and they are the reason this module exists at all:

* **Stale fallback.** If the live refresh fails, the most recently stored
  universe is reused -- but ``is_stale`` is set, the reason is recorded, and the
  flag travels all the way into ``metadata.json``, the run report and the email.
  A stale universe is never presented as a fresh one.
* **Shrink detection.** A universe that suddenly collapses (a source silently
  returning a truncated file, say) is the classic way a screen quietly starts
  picking from 40 stocks instead of 2,000.  A drop beyond
  ``UNIVERSE_SHRINK_WARN`` versus the previous stored universe, or a raw size
  below ``MIN_RAW_UNIVERSE``, raises :class:`UniverseIntegrityError` unless the
  caller explicitly accepts it.
"""

from __future__ import annotations

from datetime import date

from ..config import CONFIG, MARKETS
from ..logging_utils import get_logger
from ..storage import load_latest_universe, save_universe
from .au import AUUniverseAdapter
from .base import UniverseAdapter, UniverseEntry, UniverseResult
from .ca import CAUniverseAdapter
from .nz import NZUniverseAdapter
from .uk import UKUniverseAdapter
from .us import USUniverseAdapter

log = get_logger(__name__)

ADAPTERS: dict[str, type[UniverseAdapter]] = {
    "AU": AUUniverseAdapter,
    "US": USUniverseAdapter,
    "UK": UKUniverseAdapter,
    "NZ": NZUniverseAdapter,
    "CA": CAUniverseAdapter,
}


class UniverseIntegrityError(RuntimeError):
    """Raised when a universe is too small or has shrunk implausibly."""


def get_adapter(market: str) -> UniverseAdapter:
    key = market.upper()
    if key not in ADAPTERS:
        raise ValueError(f"unknown market {market!r} (known: {', '.join(MARKETS)})")
    return ADAPTERS[key]()


def _entries_from_rows(market: str, rows: list[dict]) -> list[UniverseEntry]:
    entries = []
    for row in rows:
        symbol = (row.get("yahoo_symbol") or "").strip()
        if not symbol:
            continue
        entries.append(
            UniverseEntry(
                market=market,
                exchange=(row.get("exchange") or "").strip(),
                exchange_symbol=(row.get("exchange_symbol") or "").strip(),
                yahoo_symbol=symbol,
                name=(row.get("name") or "").strip() or None,
                industry=(row.get("industry") or "").strip() or None,
                source=(row.get("source") or "").strip() or None,
            )
        )
    return entries


def check_universe_integrity(result: UniverseResult, previous_size: int | None) -> list[str]:
    """Return integrity problems (empty list means the universe looks sane)."""
    problems: list[str] = []
    minimum = CONFIG.MIN_RAW_UNIVERSE.get(result.market.upper())
    if minimum is not None and result.size < minimum:
        problems.append(
            f"raw universe for {result.market} has {result.size} securities, "
            f"below the configured floor of {minimum}"
        )
    if previous_size:
        shrink = 1.0 - (result.size / previous_size)
        if shrink > CONFIG.UNIVERSE_SHRINK_WARN:
            problems.append(
                f"universe for {result.market} shrank {shrink:.1%} versus the previous "
                f"stored universe ({previous_size} -> {result.size})"
            )
    return problems


def build_universe(
    market: str,
    *,
    as_of: date | None = None,
    refresh: bool = True,
    persist: bool = True,
    allow_integrity_failure: bool = False,
) -> UniverseResult:
    """Refresh (or reload) the candidate universe for one market."""
    market = market.upper()
    as_of = as_of or date.today()
    previous_rows, previous_path = load_latest_universe(market)
    previous_size = len(previous_rows) if previous_rows else None

    result: UniverseResult
    if refresh:
        try:
            result = get_adapter(market).fetch()
        except Exception as exc:  # noqa: BLE001 - any source failure lands here
            log.error("universe refresh failed for %s: %s", market, exc)
            if not previous_rows:
                raise UniverseIntegrityError(
                    f"universe refresh failed for {market} and no stored universe exists: {exc}"
                ) from exc
            result = UniverseResult(
                market=market,
                entries=_entries_from_rows(market, previous_rows),
                is_stale=True,
                stale_reason=f"refresh failed ({exc}); reusing {previous_path.name if previous_path else 'stored universe'}",
                sources=[str(previous_path) if previous_path else "stored"],
            )
            result.warnings.append(result.stale_reason)
    else:
        if not previous_rows:
            raise UniverseIntegrityError(f"no stored universe for {market}; run with --refresh")
        result = UniverseResult(
            market=market,
            entries=_entries_from_rows(market, previous_rows),
            is_stale=True,
            stale_reason=f"refresh disabled; reusing {previous_path.name if previous_path else 'stored universe'}",
            sources=[str(previous_path) if previous_path else "stored"],
        )

    problems = check_universe_integrity(result, previous_size if not result.is_stale else None)
    if problems:
        message = "; ".join(problems)
        if allow_integrity_failure:
            log.warning("universe integrity warning for %s: %s", market, message)
            result.warnings.append(f"integrity: {message}")
        else:
            raise UniverseIntegrityError(message)

    if persist and not result.is_stale:
        save_universe(
            market,
            as_of,
            [entry.to_row() for entry in result.entries],
            {
                "market": market,
                "as_of": as_of.isoformat(),
                "fetched_at": result.fetched_at,
                "sources": result.sources,
                "warnings": result.warnings,
                "excluded_count": len(result.excluded),
            },
        )
    return result
