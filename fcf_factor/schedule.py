"""Rebalance timing.

The factor rebalances quarterly, in March, June, September and December, with
the signal generated **after the first Friday of the month has closed**.  That
mirrors the VFLO schedule closely enough for a comparable forward test.

GitHub Actions runs the quarterly workflow every Saturday in those four months;
this module decides whether the Saturday in question actually follows the first
Friday, so the schedule lives in reviewable Python rather than in a cron
expression nobody can verify.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import MARKET_TIMEZONE, REBALANCE_MONTHS

#: Local closing time of each market, plus a settling buffer, used to decide
#: whether today's session counts as "completed" on a manual run.
MARKET_CLOSE_LOCAL: dict[str, time] = {
    "AU": time(16, 30),
    "US": time(16, 30),
    "UK": time(17, 0),
    "NZ": time(17, 15),
    "CA": time(16, 30),
}


def first_friday(year: int, month: int) -> date:
    """Date of the first Friday in the given month."""
    for day in range(1, 8):
        candidate = date(year, month, day)
        if candidate.weekday() == calendar.FRIDAY:
            return candidate
    raise AssertionError("unreachable: every 7-day window contains a Friday")


def is_first_friday(day: date) -> bool:
    return day.weekday() == calendar.FRIDAY and day.day <= 7


def is_rebalance_month(month: int) -> bool:
    return month in REBALANCE_MONTHS


def quarter_of(day: date) -> int:
    return (day.month - 1) // 3 + 1


def quarter_label(day: date) -> str:
    """``2026 Q3`` -- used in email subjects and report headings."""
    return f"{day.year} Q{quarter_of(day)}"


@dataclass(frozen=True)
class RebalanceDecision:
    """Whether a scheduled run should generate a signal, and for which date."""

    due: bool
    signal_date: date | None
    reason: str

    def to_dict(self) -> dict:
        return {
            "due": self.due,
            "signal_date": self.signal_date.isoformat() if self.signal_date else None,
            "reason": self.reason,
        }


def scheduled_rebalance_decision(run_date: date) -> RebalanceDecision:
    """Decide whether ``run_date`` (a scheduled Saturday) is a rebalance day.

    A rebalance happens when ``run_date`` is the Saturday immediately after the
    first Friday of March, June, September or December.  Everything else is a
    no-op, which is what stops the weekly cron from generating twelve signals a
    quarter.
    """
    if run_date.weekday() != calendar.SATURDAY:
        return RebalanceDecision(False, None, f"{run_date} is not a Saturday")
    previous_day = run_date - timedelta(days=1)
    if not is_rebalance_month(previous_day.month):
        return RebalanceDecision(
            False, None, f"{previous_day:%B} is not a rebalance month"
        )
    if not is_first_friday(previous_day):
        return RebalanceDecision(
            False, None, f"{previous_day} is not the first Friday of {previous_day:%B %Y}"
        )
    return RebalanceDecision(
        True, previous_day, f"{previous_day} was the first Friday of {previous_day:%B %Y}"
    )


def last_completed_session(market: str, now: datetime | None = None) -> date:
    """Most recent weekday whose session has certainly closed for ``market``.

    Used by manual (``workflow_dispatch``) runs, where there is no first-Friday
    anchor.  Weekends are stepped over; exchange holidays are not modelled here
    because the price collector simply finds no session for that date and moves
    on, which produces the same answer without a holiday calendar to maintain.
    """
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    tz = ZoneInfo(MARKET_TIMEZONE[market.upper()])
    local = now.astimezone(tz)
    day = local.date()
    if local.time() < MARKET_CLOSE_LOCAL[market.upper()]:
        day -= timedelta(days=1)
    while day.weekday() >= calendar.SATURDAY:
        day -= timedelta(days=1)
    return day


def next_business_day(day: date) -> date:
    """Next weekday strictly after ``day`` (weekends only, no holidays)."""
    candidate = day + timedelta(days=1)
    while candidate.weekday() >= calendar.SATURDAY:
        candidate += timedelta(days=1)
    return candidate


def resolve_signal_date(
    market: str,
    *,
    run_date: date | None = None,
    manual: bool = False,
    now: datetime | None = None,
) -> RebalanceDecision:
    """Signal date for one market, for either a scheduled or a manual run."""
    run_date = run_date or (now or datetime.now(UTC)).date()
    if manual:
        session = last_completed_session(market, now)
        return RebalanceDecision(
            True, session, f"manual run; latest completed {market} session is {session}"
        )
    return scheduled_rebalance_decision(run_date)
