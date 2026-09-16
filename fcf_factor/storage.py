"""Persistence layer.

Everything here is plain CSV and JSON on disk, committed to git, because the
point of this project is an *auditable* forward test.  A future reader must be
able to open any quarterly directory in a text editor and see exactly what the
model knew and decided on that day.

The one hard rule enforced in code: a quarterly signal directory is written
once and never rewritten.  :func:`write_signal_snapshot` raises
:class:`SnapshotExistsError` rather than overwrite history.
"""

from __future__ import annotations

import csv
import json
import os
import tempfile
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from . import config as _config
from .logging_utils import get_logger

log = get_logger(__name__)

SIGNAL_FILES = (
    "universe.csv",
    "raw_financials.csv",
    "factor_scores.csv",
    "selected.csv",
    "excluded.csv",
    "metadata.json",
)


class SnapshotExistsError(RuntimeError):
    """Raised when a quarterly snapshot already exists and would be overwritten."""


# --------------------------------------------------------------------------
# Low-level atomic IO
# --------------------------------------------------------------------------
def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_csv(path: Path, rows: Sequence[dict], columns: Sequence[str] | None = None) -> None:
    """Write ``rows`` as CSV with a stable column order (header always present)."""
    if columns is None:
        seen: list[str] = []
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.append(key)
        columns = seen or ["_empty"]
    buffer = _csv_text(rows, columns)
    atomic_write_text(path, buffer)


def _csv_text(rows: Iterable[dict], columns: Sequence[str]) -> str:
    import io

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(columns), extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: _format_cell(row.get(c)) for c in columns})
    return out.getvalue()


def _format_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        # Keep files diff-friendly and free of 1e-17 noise.
        if value != value:  # NaN
            return ""
        return f"{value:.10g}"
    if isinstance(value, (list, tuple, set)):
        return "|".join(str(v) for v in sorted(value) if v is not None)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:  # pragma: no cover - corrupt state
        log.error("could not parse %s: %s", path, exc)
        return None


# --------------------------------------------------------------------------
# Universes
# --------------------------------------------------------------------------
def universe_dir(market: str) -> Path:
    return _config.UNIVERSE_DIR / market.upper()


def universe_path(market: str, as_of: date) -> Path:
    return universe_dir(market) / f"{as_of.isoformat()}.csv"


def save_universe(market: str, as_of: date, rows: Sequence[dict], metadata: dict) -> Path:
    path = universe_path(market, as_of)
    write_csv(path, rows, columns=["market", "exchange", "exchange_symbol", "yahoo_symbol", "name", "industry", "source"])
    write_json(universe_dir(market) / "latest.json", {**metadata, "path": path.name, "size": len(rows)})
    return path


def list_universe_files(market: str) -> list[Path]:
    directory = universe_dir(market)
    if not directory.exists():
        return []
    return sorted(p for p in directory.glob("*.csv") if p.stem[:4].isdigit())


def load_latest_universe(market: str) -> tuple[list[dict], Path | None]:
    files = list_universe_files(market)
    if not files:
        return [], None
    latest = files[-1]
    return read_csv(latest), latest


# --------------------------------------------------------------------------
# Quarterly signal snapshots (immutable)
# --------------------------------------------------------------------------
def signal_dir(market: str, signal_date: date) -> Path:
    return _config.SIGNALS_DIR / market.upper() / signal_date.isoformat()


def signal_exists(market: str, signal_date: date) -> bool:
    return (signal_dir(market, signal_date) / "metadata.json").exists()


def list_signal_dates(market: str) -> list[date]:
    directory = _config.SIGNALS_DIR / market.upper()
    if not directory.exists():
        return []
    dates = []
    for child in directory.iterdir():
        if not child.is_dir():
            continue
        try:
            parsed = date.fromisoformat(child.name)
        except ValueError:
            continue
        if (child / "metadata.json").exists():
            dates.append(parsed)
    return sorted(dates)


def latest_signal_date(market: str, before: date | None = None) -> date | None:
    dates = list_signal_dates(market)
    if before is not None:
        dates = [d for d in dates if d < before]
    return dates[-1] if dates else None


def load_signal(market: str, signal_date: date) -> dict:
    """Read one immutable quarterly snapshot back into memory."""
    directory = signal_dir(market, signal_date)
    return {
        "metadata": read_json(directory / "metadata.json") or {},
        "selected": read_csv(directory / "selected.csv"),
        "factor_scores": read_csv(directory / "factor_scores.csv"),
        "excluded": read_csv(directory / "excluded.csv"),
        "universe": read_csv(directory / "universe.csv"),
        "path": directory,
    }


def write_signal_snapshot(
    market: str,
    signal_date: date,
    *,
    universe_rows: Sequence[dict],
    raw_financial_rows: Sequence[dict],
    factor_rows: Sequence[dict],
    selected_rows: Sequence[dict],
    excluded_rows: Sequence[dict],
    metadata: dict,
    allow_overwrite: bool = False,
) -> Path:
    """Write a quarterly snapshot, refusing to clobber an existing one.

    ``allow_overwrite`` exists only for local experiments; the GitHub Actions
    workflows never set it.
    """
    directory = signal_dir(market, signal_date)
    if (directory / "metadata.json").exists() and not allow_overwrite:
        raise SnapshotExistsError(
            f"signal snapshot already exists at {directory}; refusing to overwrite a historical record"
        )
    directory.mkdir(parents=True, exist_ok=True)
    write_csv(directory / "universe.csv", universe_rows)
    write_csv(directory / "raw_financials.csv", raw_financial_rows)
    write_csv(directory / "factor_scores.csv", factor_rows)
    write_csv(directory / "selected.csv", selected_rows, columns=SELECTED_COLUMNS)
    write_csv(directory / "excluded.csv", excluded_rows, columns=["yahoo_symbol", "exchange_symbol", "name", "stage", "reasons"])
    write_json(directory / "metadata.json", metadata)
    return directory


#: Column order for ``selected.csv`` -- stable so downstream tooling and humans
#: can rely on it.
SELECTED_COLUMNS = [
    "ticker",
    "exchange_symbol",
    "company",
    "sector",
    "industry",
    "market",
    "currency",
    "market_cap_local",
    "market_cap_usd",
    "enterprise_value_usd",
    "enterprise_value_source",
    "ttm_fcf",
    "ttm_fcf_usd",
    "normalized_fcf_margin",
    "forward_growth",
    "forward_revenue",
    "forward_fcf",
    "expected_fcf",
    "expected_fcf_usd",
    "fcf_yield",
    "revenue_trend",
    "ebitda_trend",
    "fcf_per_share_trend",
    "z_revenue",
    "z_ebitda",
    "z_fcf_per_share",
    "growth_score",
    "fcf_yield_rank",
    "growth_rank",
    "target_weight",
    "raw_weight",
    "ttm_source",
    "data_quality_flags",
]


# --------------------------------------------------------------------------
# Daily prices
# --------------------------------------------------------------------------
PRICE_COLUMNS = [
    "date",
    "ticker",
    "market",
    "currency",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
    "dividend",
    "split",
    "fx_rate_to_usd",
    "retrieved_at",
]


def price_path(market: str, month: str) -> Path:
    """``month`` is ``YYYY-MM``."""
    return _config.PRICES_DIR / market.upper() / f"{month}.csv"


def list_price_files(market: str) -> list[Path]:
    directory = _config.PRICES_DIR / market.upper()
    if not directory.exists():
        return []
    return sorted(directory.glob("*.csv"))


def load_prices(market: str, months: Sequence[str] | None = None) -> list[dict]:
    rows: list[dict] = []
    for path in list_price_files(market):
        if months is not None and path.stem not in months:
            continue
        rows.extend(read_csv(path))
    return rows


def has_price(row: dict) -> bool:
    """True when a stored row carries a real closing price.

    Providers sometimes return a placeholder bar for a session that has not
    opened yet: the date is present but every price field is blank.  Such a row
    is not an observation, and treating it as one would let the portfolio trade
    on a session that never happened.
    """
    raw = row.get("close")
    if raw is None or raw == "":
        return False
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return False
    return value == value and value > 0


def upsert_prices(market: str, rows: Sequence[dict]) -> dict[str, int]:
    """Merge new price rows into the monthly files without creating duplicates.

    An existing ``(date, ticker)`` row that carries a real price is left
    untouched: once a session has been recorded it is never rewritten, which is
    what keeps the forward test honest when a provider retroactively re-adjusts
    a series.

    The one exception is a stored *placeholder* -- a row with no closing price,
    captured before the session opened.  That is not an observation, so it is
    replaced once the real session data arrives.  Without this, a single early
    fetch would leave a permanent hole in the series.
    """
    by_month: dict[str, list[dict]] = {}
    for row in rows:
        day = str(row.get("date", ""))[:10]
        if not day:
            continue
        by_month.setdefault(day[:7], []).append(row)

    stats = {"added": 0, "skipped_existing": 0, "replaced_placeholder": 0}
    for month, month_rows in by_month.items():
        path = price_path(market, month)
        merged = read_csv(path)
        index = {
            (str(r.get("date", ""))[:10], str(r.get("ticker", ""))): i
            for i, r in enumerate(merged)
        }
        for row in month_rows:
            key = (str(row.get("date", ""))[:10], str(row.get("ticker", "")))
            position = index.get(key)
            if position is not None:
                if has_price(merged[position]) or not has_price(row):
                    stats["skipped_existing"] += 1
                    continue
                merged[position] = row
                stats["replaced_placeholder"] += 1
                continue
            index[key] = len(merged)
            merged.append(row)
            stats["added"] += 1
        merged.sort(key=lambda r: (str(r.get("date", ""))[:10], str(r.get("ticker", ""))))
        write_csv(path, merged, columns=PRICE_COLUMNS)
    return stats


# --------------------------------------------------------------------------
# Portfolio state and performance
# --------------------------------------------------------------------------
def state_path(market: str) -> Path:
    return _config.STATE_DIR / f"{market.upper()}.json"


def load_state(market: str) -> dict | None:
    return read_json(state_path(market))


def save_state(market: str, state: dict) -> Path:
    path = state_path(market)
    write_json(path, state)
    return path


PERFORMANCE_COLUMNS = [
    "date",
    "market",
    "nav",
    "daily_return",
    "cumulative_return",
    "holdings",
    "cash",
    "event",
    "signal_date",
    "turnover",
]


def performance_path(market: str) -> Path:
    return _config.PERFORMANCE_DIR / f"{market.upper()}.csv"


def load_performance(market: str) -> list[dict]:
    return read_csv(performance_path(market))


def append_performance(market: str, rows: Sequence[dict]) -> Path:
    """Append NAV rows, keeping one row per date (existing rows win)."""
    path = performance_path(market)
    existing = load_performance(market)
    seen = {r.get("date", "")[:10] for r in existing}
    merged = list(existing)
    for row in rows:
        day = str(row.get("date", ""))[:10]
        if not day or day in seen:
            continue
        seen.add(day)
        merged.append(row)
    merged.sort(key=lambda r: str(r.get("date", ""))[:10])
    write_csv(path, merged, columns=PERFORMANCE_COLUMNS)
    return path


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
