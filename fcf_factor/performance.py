"""Reading the forward-test record back out: NAV, returns and turnover.

Nothing here computes anything the portfolio engine did not already record.  It
reads ``data/performance/<MARKET>.csv`` and ``data/state/<MARKET>.json`` so the
report and the email always describe exactly what was persisted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .config import CONFIG
from .storage import list_signal_dates, load_performance, load_signal, load_state


def _f(value, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return None if out != out else out


@dataclass
class PerformanceSummary:
    """Point-in-time view of one market's live portfolio."""

    market: str
    started: bool = False
    inception_date: str | None = None
    latest_date: str | None = None
    nav: float | None = None
    starting_nav: float = CONFIG.STARTING_NAV
    since_inception_return: float | None = None
    latest_quarter_return: float | None = None
    latest_daily_return: float | None = None
    holdings: int = 0
    latest_turnover: float | None = None
    last_execution_date: str | None = None
    active_signal_date: str | None = None
    pending_signal_date: str | None = None
    sessions_recorded: int = 0
    positions: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "market": self.market,
            "started": self.started,
            "inception_date": self.inception_date,
            "latest_date": self.latest_date,
            "nav": self.nav,
            "since_inception_return": self.since_inception_return,
            "latest_quarter_return": self.latest_quarter_return,
            "latest_daily_return": self.latest_daily_return,
            "holdings": self.holdings,
            "latest_turnover": self.latest_turnover,
            "last_execution_date": self.last_execution_date,
            "active_signal_date": self.active_signal_date,
            "pending_signal_date": self.pending_signal_date,
            "sessions_recorded": self.sessions_recorded,
        }


def performance_summary(market: str) -> PerformanceSummary:
    """Summarise a market's live portfolio from the persisted record."""
    market = market.upper()
    summary = PerformanceSummary(market=market)
    state = load_state(market) or {}
    rows = load_performance(market)

    summary.starting_nav = _f(state.get("starting_nav"), CONFIG.STARTING_NAV) or CONFIG.STARTING_NAV
    summary.inception_date = state.get("inception_date")
    summary.active_signal_date = state.get("active_signal_date")
    summary.sessions_recorded = len(rows)

    executions = state.get("executions") or []
    if executions:
        last = executions[-1]
        summary.last_execution_date = last.get("execution_date")
        summary.latest_turnover = _f(last.get("turnover"))

    executed = {e.get("signal_date") for e in executions}
    outstanding = [d.isoformat() for d in list_signal_dates(market) if d.isoformat() not in executed]
    summary.pending_signal_date = outstanding[0] if outstanding else None

    positions = state.get("positions") or {}
    summary.holdings = len(positions)
    summary.positions = [
        {
            "ticker": ticker,
            "shares": _f(info.get("shares")),
            "last_price": _f(info.get("last_price")),
            "target_weight": _f(info.get("target_weight")),
            "drifted_weight": _f(info.get("drifted_weight")),
            "entry_price": _f(info.get("entry_price")),
            "entry_date": info.get("entry_date"),
        }
        for ticker, info in sorted(positions.items())
    ]
    summary.positions.sort(key=lambda p: -(p.get("drifted_weight") or 0.0))

    if not rows:
        summary.nav = _f(state.get("nav"), summary.starting_nav)
        return summary

    summary.started = True
    latest = rows[-1]
    summary.latest_date = str(latest.get("date", ""))[:10]
    summary.nav = _f(latest.get("nav"))
    summary.latest_daily_return = _f(latest.get("daily_return"))
    if summary.nav is not None and summary.starting_nav:
        summary.since_inception_return = summary.nav / summary.starting_nav - 1.0

    if summary.last_execution_date and summary.nav is not None:
        base = None
        for row in rows:
            day = str(row.get("date", ""))[:10]
            if day == summary.last_execution_date:
                base = _f(row.get("nav"))
                break
        if base:
            summary.latest_quarter_return = summary.nav / base - 1.0
    return summary


@dataclass
class SignalComparison:
    """Additions, removals and retained names versus the previous rebalance."""

    current_date: date | None = None
    previous_date: date | None = None
    additions: list[str] = field(default_factory=list)
    removals: list[str] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "current_signal_date": self.current_date.isoformat() if self.current_date else None,
            "previous_signal_date": self.previous_date.isoformat() if self.previous_date else None,
            "additions": self.additions,
            "removals": self.removals,
            "retained": self.retained,
        }


def compare_signals(
    market: str,
    current: date,
    previous: date | None = None,
    current_tickers: list[str] | None = None,
) -> SignalComparison:
    """Diff a signal against the previous stored one for the same market."""
    market = market.upper()
    comparison = SignalComparison(current_date=current)

    if current_tickers is None:
        current_tickers = [
            (row.get("ticker") or "").strip()
            for row in load_signal(market, current).get("selected", [])
        ]
    current_set = {t for t in current_tickers if t}

    if previous is None:
        earlier = [d for d in list_signal_dates(market) if d < current]
        previous = earlier[-1] if earlier else None
    comparison.previous_date = previous

    if previous is None:
        comparison.additions = sorted(current_set)
        return comparison

    previous_set = {
        (row.get("ticker") or "").strip()
        for row in load_signal(market, previous).get("selected", [])
    }
    previous_set.discard("")
    comparison.additions = sorted(current_set - previous_set)
    comparison.removals = sorted(previous_set - current_set)
    comparison.retained = sorted(current_set & previous_set)
    return comparison


def distribution(values: list[float | None], buckets: int = 5) -> list[tuple[str, int]]:
    """Coarse histogram used in the markdown reports."""
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return []
    low, high = clean[0], clean[-1]
    if high <= low:
        return [(f"{low:.4g}", len(clean))]
    width = (high - low) / buckets
    counts = [0] * buckets
    for value in clean:
        idx = min(buckets - 1, int((value - low) / width))
        counts[idx] += 1
    return [
        (f"{low + i * width:.3g} to {low + (i + 1) * width:.3g}", counts[i]) for i in range(buckets)
    ]


def percentiles(values: list[float | None]) -> dict[str, float]:
    """Min / quartiles / max of the valid values, for the report tables."""
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return {}

    def at(fraction: float) -> float:
        if len(clean) == 1:
            return clean[0]
        position = fraction * (len(clean) - 1)
        lower = int(position)
        upper = min(lower + 1, len(clean) - 1)
        weight = position - lower
        return clean[lower] * (1 - weight) + clean[upper] * weight

    return {
        "min": clean[0],
        "p25": at(0.25),
        "median": at(0.5),
        "p75": at(0.75),
        "max": clean[-1],
    }
