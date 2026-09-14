"""The forward-test portfolio engine: execution, drift and NAV.

Each market runs an independent portfolio that starts at NAV 100 on the day its
first signal is executed.  There is no history before that day, by design --
inventing returns for periods before the software existed would defeat the
entire purpose of the exercise.

The execution rule is the part that keeps the test honest:

* the signal is generated after a market close,
* the portfolio may only transact at the **next available market open**,
* the old portfolio is sold at that open and the new one bought at that same
  open, and every open price used is stored.

Between rebalances the portfolio drifts.  It is never rebalanced back to target
weights daily, because a real portfolio would not be.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..config import CONFIG, MARKET_CURRENCY, METHODOLOGY_VERSION, FactorConfig
from ..logging_utils import get_logger
from ..storage import (
    append_performance,
    list_signal_dates,
    load_signal,
    load_state,
    save_state,
    utc_now_iso,
)
from .prices import price_index

log = get_logger(__name__)

#: Below this share of holdings reporting a close, the session is marked as
#: having thin data (positions are carried forward at their last known price).
THIN_SESSION_COVERAGE = 0.5


def new_state(market: str, config: FactorConfig = CONFIG) -> dict:
    """A portfolio that has never traded: all cash, NAV at the starting level."""
    return {
        "market": market.upper(),
        "currency": MARKET_CURRENCY[market.upper()],
        "methodology_version": METHODOLOGY_VERSION,
        "starting_nav": config.STARTING_NAV,
        "nav": config.STARTING_NAV,
        "cash": config.STARTING_NAV,
        "inception_date": None,
        "last_processed_date": None,
        "active_signal_date": None,
        "positions": {},
        "executions": [],
        "updated_at": utc_now_iso(),
    }


def _to_float(value, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:  # NaN
        return default
    return result


@dataclass
class PortfolioUpdate:
    """What one ``update-portfolio`` run actually did."""

    market: str
    sessions_processed: int = 0
    executions: list[dict] = field(default_factory=list)
    performance_rows: list[dict] = field(default_factory=list)
    nav: float | None = None
    pending_signal_date: date | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "market": self.market,
            "sessions_processed": self.sessions_processed,
            "executions": self.executions,
            "nav": self.nav,
            "pending_signal_date": self.pending_signal_date.isoformat()
            if self.pending_signal_date
            else None,
            "notes": self.notes,
        }


def pending_signal_dates(market: str, state: dict) -> list[date]:
    """Signal dates that exist on disk but have not been executed yet."""
    executed = {e.get("signal_date") for e in state.get("executions", [])}
    return [d for d in list_signal_dates(market) if d.isoformat() not in executed]


def _targets_from_signal(market: str, signal_date: date) -> tuple[dict[str, float], dict, dict[str, dict]]:
    """Target weights, snapshot metadata and per-ticker detail for one signal."""
    snapshot = load_signal(market, signal_date)
    weights: dict[str, float] = {}
    detail: dict[str, dict] = {}
    for row in snapshot.get("selected", []):
        ticker = (row.get("ticker") or "").strip()
        weight = _to_float(row.get("target_weight"))
        if not ticker or weight is None or weight <= 0:
            continue
        weights[ticker] = weight
        detail[ticker] = row
    return weights, snapshot.get("metadata", {}), detail


def update_portfolio(
    market: str,
    *,
    today: date | None = None,
    config: FactorConfig = CONFIG,
    persist: bool = True,
) -> PortfolioUpdate:
    """Advance one market's portfolio through every unprocessed trading session."""
    market = market.upper()
    state = load_state(market) or new_state(market, config)
    update = PortfolioUpdate(market=market)

    index = price_index(market)
    if not index:
        update.notes.append("no stored prices for this market yet")
        update.nav = _to_float(state.get("nav"), config.STARTING_NAV)
        return update

    pending = pending_signal_dates(market, state)
    update.pending_signal_date = pending[0] if pending else None

    last_processed = state.get("last_processed_date")
    candidate_days = sorted(d for d in index if (last_processed is None or d > last_processed))
    if today is not None:
        candidate_days = [d for d in candidate_days if d <= today.isoformat()]

    # Never start a portfolio before its first signal exists.
    if not state.get("positions") and not state.get("executions"):
        if not pending:
            update.notes.append("no signal has been generated yet; portfolio not started")
            update.nav = _to_float(state.get("nav"), config.STARTING_NAV)
            return update
        first_signal = pending[0].isoformat()
        candidate_days = [d for d in candidate_days if d > first_signal]

    positions: dict[str, dict] = {
        ticker: dict(info) for ticker, info in (state.get("positions") or {}).items()
    }
    cash = _to_float(state.get("cash"), 0.0) or 0.0
    nav = _to_float(state.get("nav"), config.STARTING_NAV) or config.STARTING_NAV
    performance_rows: list[dict] = []

    # Read the pending snapshot once rather than on every session.
    pending_targets = set(_peek_targets(market, pending))

    for day in candidate_days:
        bars = index.get(day, {})
        relevant = set(positions) | pending_targets
        traded = [t for t in relevant if _to_float((bars.get(t) or {}).get("close")) is not None]
        if not traded:
            continue  # holiday, or no data for anything we care about

        event_parts: list[str] = []
        turnover = 0.0

        # ---- corporate actions ------------------------------------------
        for ticker, position in positions.items():
            bar = bars.get(ticker)
            if not bar:
                continue
            split = _to_float(bar.get("split"), 0.0) or 0.0
            if split > 0 and abs(split - 1.0) > 1e-9:
                position["shares"] = (_to_float(position.get("shares"), 0.0) or 0.0) * split
                event_parts.append(f"split:{ticker}x{split:g}")
            dividend = _to_float(bar.get("dividend"), 0.0) or 0.0
            if dividend > 0:
                cash += (_to_float(position.get("shares"), 0.0) or 0.0) * dividend
                event_parts.append(f"dividend:{ticker}")

        # ---- execution at the open --------------------------------------
        if pending and day > pending[0].isoformat():
            signal_date = pending[0]
            execution, positions, cash, turnover, executed = _execute(
                market=market,
                signal_date=signal_date,
                day=day,
                bars=bars,
                positions=positions,
                cash=cash,
                config=config,
            )
            if executed:
                state.setdefault("executions", []).append(execution)
                update.executions.append(execution)
                state["active_signal_date"] = signal_date.isoformat()
                if not state.get("inception_date"):
                    state["inception_date"] = day
                pending = pending[1:]
                pending_targets = set(_peek_targets(market, pending))
                update.pending_signal_date = pending[0] if pending else None
                event_parts.append(f"rebalance:{signal_date.isoformat()}")
            else:
                update.notes.append(
                    f"{day}: could not execute signal {signal_date} (no open prices); will retry"
                )

        # ---- mark to market at the close --------------------------------
        previous_nav = nav
        stale = 0
        market_value = 0.0
        for ticker, position in positions.items():
            close = _to_float((bars.get(ticker) or {}).get("close"))
            if close is None or close <= 0:
                close = _to_float(position.get("last_price"))
                stale += 1
            if close is None:
                continue
            position["last_price"] = close
            position["last_price_date"] = day
            market_value += (_to_float(position.get("shares"), 0.0) or 0.0) * close
        nav = market_value + cash

        if positions:
            for position in positions.values():
                value = (_to_float(position.get("shares"), 0.0) or 0.0) * (
                    _to_float(position.get("last_price"), 0.0) or 0.0
                )
                position["drifted_weight"] = (value / nav) if nav > 0 else 0.0
            coverage = 1.0 - (stale / len(positions))
            if coverage < THIN_SESSION_COVERAGE:
                event_parts.append(f"thin_data:{coverage:.0%}")

        daily_return = (nav / previous_nav - 1.0) if previous_nav else 0.0
        starting = _to_float(state.get("starting_nav"), config.STARTING_NAV) or config.STARTING_NAV
        performance_rows.append(
            {
                "date": day,
                "market": market,
                "nav": nav,
                "daily_return": daily_return,
                "cumulative_return": nav / starting - 1.0,
                "holdings": len(positions),
                "cash": cash,
                "event": ";".join(event_parts),
                "signal_date": state.get("active_signal_date") or "",
                "turnover": turnover,
            }
        )
        state["last_processed_date"] = day
        update.sessions_processed += 1

    state["positions"] = positions
    state["cash"] = cash
    state["nav"] = nav
    state["updated_at"] = utc_now_iso()
    update.nav = nav
    update.performance_rows = performance_rows

    if persist:
        if performance_rows:
            append_performance(market, performance_rows)
        save_state(market, state)
    return update


def _peek_targets(market: str, pending: list[date]) -> dict[str, float]:
    if not pending:
        return {}
    try:
        weights, _, _ = _targets_from_signal(market, pending[0])
        return weights
    except Exception as exc:  # noqa: BLE001 - a malformed snapshot must not stop pricing
        log.warning("could not read pending signal %s for %s: %s", pending[0], market, exc)
        return {}


def _execute(
    *,
    market: str,
    signal_date: date,
    day: str,
    bars: dict[str, dict],
    positions: dict[str, dict],
    cash: float,
    config: FactorConfig,
) -> tuple[dict, dict[str, dict], float, float, bool]:
    """Close the old book and open the new one at ``day``'s opening prices."""
    weights, metadata, _detail = _targets_from_signal(market, signal_date)
    if not weights:
        return {}, positions, cash, 0.0, False

    # Value the existing book at today's open.
    nav_at_open = cash
    old_values: dict[str, float] = {}
    stale_priced: list[str] = []
    for ticker, position in positions.items():
        open_price = _to_float((bars.get(ticker) or {}).get("open"))
        if open_price is None or open_price <= 0:
            # No opening print today: fall back to the last observed price and
            # record it, so a sale at a carried price is visible in the audit.
            open_price = _to_float(position.get("last_price"))
            stale_priced.append(ticker)
        shares = _to_float(position.get("shares"), 0.0) or 0.0
        value = shares * (open_price or 0.0)
        old_values[ticker] = value
        nav_at_open += value
    if nav_at_open <= 0:
        return {}, positions, cash, 0.0, False

    available: dict[str, float] = {}
    missing: list[str] = []
    for ticker in weights:
        open_price = _to_float((bars.get(ticker) or {}).get("open"))
        if open_price is None or open_price <= 0:
            open_price = _to_float((bars.get(ticker) or {}).get("close"))
        if open_price is None or open_price <= 0:
            missing.append(ticker)
            continue
        available[ticker] = open_price
    if not available:
        return {}, positions, cash, 0.0, False

    total_weight = sum(weights[t] for t in available)
    effective = {t: weights[t] / total_weight for t in available}

    old_weights = {t: (v / nav_at_open) for t, v in old_values.items()}
    # ``traded_fraction`` is the two-way notional turned over (0 to 2);
    # ``turnover`` is the conventional one-way figure, so a complete swap of the
    # book reads as 100% and an initial purchase from cash reads as 50%.
    traded_fraction = sum(
        abs(effective.get(t, 0.0) - old_weights.get(t, 0.0)) for t in set(effective) | set(old_weights)
    )
    turnover = traded_fraction / 2.0
    cost = nav_at_open * traded_fraction * (config.TRANSACTION_COST_BPS / 10_000.0)
    nav_after_cost = nav_at_open - cost

    new_positions: dict[str, dict] = {}
    for ticker, weight in effective.items():
        open_price = available[ticker]
        shares = (nav_after_cost * weight) / open_price
        new_positions[ticker] = {
            "shares": shares,
            "entry_price": open_price,
            "entry_date": day,
            "last_price": open_price,
            "last_price_date": day,
            "target_weight": weights[ticker],
            "effective_target_weight": weight,
            "drifted_weight": weight,
        }

    execution = {
        "signal_date": signal_date.isoformat(),
        "signal_timestamp": metadata.get("signal_timestamp"),
        "execution_date": day,
        "nav_before": nav_at_open,
        "nav_after": nav_after_cost,
        "transaction_cost": cost,
        "transaction_cost_bps": config.TRANSACTION_COST_BPS,
        "turnover": turnover,
        "sold": sorted(set(positions) - set(new_positions)),
        "bought": sorted(set(new_positions) - set(positions)),
        "retained": sorted(set(new_positions) & set(positions)),
        "missing_at_execution": missing,
        "sold_at_carried_price": sorted(stale_priced),
        "positions": [
            {
                "ticker": ticker,
                "execution_open_price": info["entry_price"],
                "shares": info["shares"],
                "target_weight": info["target_weight"],
                "effective_target_weight": info["effective_target_weight"],
            }
            for ticker, info in sorted(new_positions.items())
        ],
        "recorded_at": utc_now_iso(),
    }
    return execution, new_positions, 0.0, turnover, True


def active_tickers(market: str) -> list[str]:
    """Every ticker the daily price job must track for this market.

    That is the current book plus anything in a signal that has not executed
    yet -- otherwise the pending portfolio would have no opening price to buy at.
    """
    state = load_state(market) or {}
    tickers = set((state.get("positions") or {}).keys())
    for signal_date in pending_signal_dates(market, state):
        try:
            weights, _, _ = _targets_from_signal(market, signal_date)
            tickers.update(weights)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read signal %s for %s: %s", signal_date, market, exc)
    return sorted(tickers)
