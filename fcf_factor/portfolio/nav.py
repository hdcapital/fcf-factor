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
    #: Purchases that could not trade at the rebalance open: their cash is set
    #: aside and spent at the first open where the stock actually trades.
    reserved: dict[str, dict] = {
        ticker: dict(info) for ticker, info in (state.get("reserved_cash") or {}).items()
    }
    cash = _to_float(state.get("cash"), 0.0) or 0.0
    nav = _to_float(state.get("nav"), config.STARTING_NAV) or config.STARTING_NAV
    performance_rows: list[dict] = []
    fills: list[dict] = state.setdefault("fills", [])

    # Read the pending snapshot once rather than on every session.
    pending_targets = set(_peek_targets(market, pending))

    for day in candidate_days:
        bars = index.get(day, {})
        relevant = set(positions) | set(reserved) | pending_targets
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
        executed_today = False
        if pending and day > pending[0].isoformat():
            signal_date = pending[0]
            execution, positions, reserved, cash, turnover, executed = _execute(
                market=market,
                signal_date=signal_date,
                day=day,
                bars=bars,
                positions=positions,
                reserved=reserved,
                cash=cash,
                config=config,
            )
            if executed:
                executed_today = True
                state.setdefault("executions", []).append(execution)
                update.executions.append(execution)
                state["active_signal_date"] = signal_date.isoformat()
                if not state.get("inception_date"):
                    state["inception_date"] = day
                pending = pending[1:]
                pending_targets = set(_peek_targets(market, pending))
                update.pending_signal_date = pending[0] if pending else None
                event_parts.append(f"rebalance:{signal_date.isoformat()}")
                for ticker in execution.get("awaiting_first_trade", []):
                    event_parts.append(f"awaiting:{ticker}")
                for ticker in execution.get("sale_awaiting_trade", []):
                    event_parts.append(f"sale_awaiting:{ticker}")

        # ---- deferred trades: fill at the first open where the stock trades
        if not executed_today:
            cash, filled = _process_deferred(
                day=day,
                bars=bars,
                positions=positions,
                reserved=reserved,
                cash=cash,
                signal_date=state.get("active_signal_date"),
            )
            for fill in filled:
                fills.append(fill)
                event_parts.append(f"{'filled' if fill['side'] == 'buy' else 'sold'}:{fill['ticker']}")

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
        reserved_total = sum(_to_float(r.get("cash"), 0.0) or 0.0 for r in reserved.values())
        nav = market_value + cash + reserved_total

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
                "reserved_cash": reserved_total,
                "event": ";".join(event_parts),
                "signal_date": state.get("active_signal_date") or "",
                "turnover": turnover,
            }
        )
        state["last_processed_date"] = day
        update.sessions_processed += 1

    state["positions"] = positions
    state["reserved_cash"] = reserved
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


def _open_price(bar: dict | None) -> float | None:
    """The price the stock traded at on this session's open, if it traded.

    A bar with no open but a close (some feeds omit the open for thin stocks)
    still proves the stock traded, so the close stands in.  No price at all
    means it did not trade and nothing can be bought or sold at it.
    """
    if not bar:
        return None
    for key in ("open", "close"):
        value = _to_float(bar.get(key))
        if value is not None and value > 0:
            return value
    return None


def _execute(
    *,
    market: str,
    signal_date: date,
    day: str,
    bars: dict[str, dict],
    positions: dict[str, dict],
    reserved: dict[str, dict],
    cash: float,
    config: FactorConfig,
) -> tuple[dict, dict[str, dict], dict[str, dict], float, float, bool]:
    """Move the book to the new targets at ``day``'s open, as far as it can trade.

    A stock with no trade on the rebalance day cannot be bought or sold at a
    price that never existed.  So:

    * a new purchase that cannot trade has its cash set aside, and is bought at
      the first later open where the stock trades;
    * a sale that cannot trade stays in the book, valued at its last price, and
      is sold at the first later open where it trades;
    * a retained holding that cannot trade simply keeps its shares.

    Transaction costs are charged at the rebalance on the full intended
    turnover, including trades that complete later.
    """
    weights, metadata, _detail = _targets_from_signal(market, signal_date)
    if not weights:
        return {}, positions, reserved, cash, 0.0, False
    total = sum(weights.values())
    weights = {t: w / total for t, w in weights.items()}

    # Purchases still waiting from the previous signal are cancelled: the new
    # signal decides what to own, and their cash returns to the pool.
    cancelled = sorted(reserved)
    cash += sum(_to_float(r.get("cash"), 0.0) or 0.0 for r in reserved.values())
    reserved = {}

    opens = {t: _open_price(bars.get(t)) for t in set(positions) | set(weights)}

    # Value the existing book at today's open (last price where it can't trade).
    values: dict[str, float] = {}
    for ticker, position in positions.items():
        price = opens.get(ticker) or _to_float(position.get("last_price"), 0.0) or 0.0
        values[ticker] = (_to_float(position.get("shares"), 0.0) or 0.0) * price
    nav_at_open = cash + sum(values.values())
    if nav_at_open <= 0:
        return {}, positions, reserved, cash, 0.0, False

    old_weights = {t: v / nav_at_open for t, v in values.items()}
    traded_fraction = sum(
        abs(weights.get(t, 0.0) - old_weights.get(t, 0.0)) for t in set(weights) | set(old_weights)
    )
    turnover = traded_fraction / 2.0
    cost = nav_at_open * traded_fraction * (config.TRANSACTION_COST_BPS / 10_000.0)
    nav_after_cost = nav_at_open - cost
    cash -= cost

    new_positions: dict[str, dict] = {}
    sold: list[str] = []
    sale_awaiting: list[str] = []
    held_untradable: list[str] = []

    # Exits.
    for ticker, position in positions.items():
        if ticker in weights:
            continue
        price = opens.get(ticker)
        if price is not None:
            cash += (_to_float(position.get("shares"), 0.0) or 0.0) * price
            sold.append(ticker)
        else:
            kept = dict(position)
            kept["pending_sale"] = True
            kept["pending_sale_since"] = day
            kept["target_weight"] = 0.0
            new_positions[ticker] = kept
            sale_awaiting.append(ticker)

    # Entries and resizes.
    awaiting: list[str] = []
    for ticker, weight in sorted(weights.items()):
        target_value = nav_after_cost * weight
        price = opens.get(ticker)
        held = positions.get(ticker)
        if held is not None and price is None:
            kept = dict(held)
            kept.pop("pending_sale", None)
            kept.pop("pending_sale_since", None)
            kept["target_weight"] = weight
            new_positions[ticker] = kept
            held_untradable.append(ticker)
            continue
        if price is None:
            reserved[ticker] = {
                "cash": target_value,
                "target_weight": weight,
                "signal_date": signal_date.isoformat(),
                "since": day,
            }
            cash -= target_value
            awaiting.append(ticker)
            continue
        old_shares = (_to_float(held.get("shares"), 0.0) or 0.0) if held else 0.0
        shares = target_value / price
        cash -= (shares - old_shares) * price
        new_positions[ticker] = {
            "shares": shares,
            "entry_price": price if held is None else held.get("entry_price", price),
            "entry_date": day if held is None else held.get("entry_date", day),
            "rebalance_price": price,
            "last_price": price,
            "last_price_date": day,
            "target_weight": weight,
            "drifted_weight": weight,
        }

    execution = {
        "signal_date": signal_date.isoformat(),
        "signal_timestamp": metadata.get("signal_timestamp"),
        "execution_date": day,
        "execution_methodology_version": METHODOLOGY_VERSION,
        "nav_before": nav_at_open,
        "nav_after": nav_after_cost,
        "transaction_cost": cost,
        "transaction_cost_bps": config.TRANSACTION_COST_BPS,
        "turnover": turnover,
        "sold": sorted(sold),
        "bought": sorted(t for t in new_positions if t not in positions),
        "retained": sorted(t for t in new_positions if t in positions and t in weights),
        "awaiting_first_trade": sorted(awaiting),
        "sale_awaiting_trade": sorted(sale_awaiting),
        "held_without_trade": sorted(held_untradable),
        "cancelled_unfilled_purchases": cancelled,
        "positions": [
            {
                "ticker": ticker,
                "execution_open_price": info.get("rebalance_price"),
                "shares": info["shares"],
                "target_weight": info.get("target_weight"),
            }
            for ticker, info in sorted(new_positions.items())
            if ticker in weights and ticker not in held_untradable
        ],
        "recorded_at": utc_now_iso(),
    }
    return execution, new_positions, reserved, cash, turnover, True


def _process_deferred(
    *,
    day: str,
    bars: dict[str, dict],
    positions: dict[str, dict],
    reserved: dict[str, dict],
    cash: float,
    signal_date: str | None,
) -> tuple[float, list[dict]]:
    """Complete any purchase or sale that was waiting for the stock to trade."""
    filled: list[dict] = []

    for ticker in sorted(reserved):
        price = _open_price(bars.get(ticker))
        if price is None:
            continue
        order = reserved.pop(ticker)
        amount = _to_float(order.get("cash"), 0.0) or 0.0
        shares = amount / price
        positions[ticker] = {
            "shares": shares,
            "entry_price": price,
            "entry_date": day,
            "rebalance_price": price,
            "last_price": price,
            "last_price_date": day,
            "target_weight": order.get("target_weight"),
            "drifted_weight": None,
        }
        filled.append(
            {
                "date": day,
                "ticker": ticker,
                "side": "buy",
                "price": price,
                "shares": shares,
                "amount": amount,
                "signal_date": order.get("signal_date") or signal_date,
                "waited_since": order.get("since"),
            }
        )

    for ticker in sorted(t for t, p in positions.items() if p.get("pending_sale")):
        price = _open_price(bars.get(ticker))
        if price is None:
            continue
        position = positions.pop(ticker)
        shares = _to_float(position.get("shares"), 0.0) or 0.0
        cash += shares * price
        filled.append(
            {
                "date": day,
                "ticker": ticker,
                "side": "sell",
                "price": price,
                "shares": shares,
                "amount": shares * price,
                "signal_date": signal_date,
                "waited_since": position.get("pending_sale_since"),
            }
        )
    return cash, filled


def active_tickers(market: str) -> list[str]:
    """Every ticker the daily price job must track for this market.

    That is the current book plus anything in a signal that has not executed
    yet -- otherwise the pending portfolio would have no opening price to buy at.
    """
    state = load_state(market) or {}
    tickers = set((state.get("positions") or {}).keys())
    tickers.update((state.get("reserved_cash") or {}).keys())
    for signal_date in pending_signal_dates(market, state):
        try:
            weights, _, _ = _targets_from_signal(market, signal_date)
            tickers.update(weights)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read signal %s for %s: %s", signal_date, market, exc)
    return sorted(tickers)
