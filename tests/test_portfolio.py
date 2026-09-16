"""Execution timing, drift, dividends, splits and NAV.

The single most important test in this file is
:func:`test_execution_happens_at_the_next_open_not_the_signal_close`: if that
ever passes by using the signal day's close, the whole forward test is
contaminated by look-ahead bias.
"""

from __future__ import annotations

from datetime import date

import pytest

from fcf_factor.config import CONFIG
from fcf_factor.portfolio.nav import active_tickers, new_state, update_portfolio
from fcf_factor.storage import (
    load_performance,
    load_state,
    save_state,
    upsert_prices,
    write_signal_snapshot,
)

MARKET = "AU"
SIGNAL_DATE = date(2026, 9, 4)


def write_signal(tickers_and_weights: dict[str, float], signal_date: date = SIGNAL_DATE):
    return write_signal_snapshot(
        MARKET,
        signal_date,
        universe_rows=[{"yahoo_symbol": t} for t in tickers_and_weights],
        raw_financial_rows=[],
        factor_rows=[],
        selected_rows=[
            {"ticker": t, "company": t, "sector": "Technology", "target_weight": w}
            for t, w in tickers_and_weights.items()
        ],
        excluded_rows=[],
        metadata={
            "market": MARKET,
            "signal_date": signal_date.isoformat(),
            "signal_timestamp": f"{signal_date.isoformat()}T09:00:00+00:00",
        },
    )


def price(day: str, ticker: str, open_: float, close: float, dividend: float = 0.0, split: float = 0.0):
    return {
        "date": day,
        "ticker": ticker,
        "market": MARKET,
        "currency": "AUD",
        "open": open_,
        "high": max(open_, close),
        "low": min(open_, close),
        "close": close,
        "adj_close": close,
        "volume": 1000,
        "dividend": dividend,
        "split": split,
        "fx_rate_to_usd": 0.65,
        "retrieved_at": "2026-09-07T23:35:00+00:00",
    }


def test_execution_happens_at_the_next_open_not_the_signal_close(data_root):
    """The signal-day close is observable but unusable; the next open is used."""
    write_signal({"AAA.AX": 1.0})
    upsert_prices(
        MARKET,
        [
            # Friday: the session that generated the signal.
            price("2026-09-04", "AAA.AX", 9.0, 10.0),
            # Monday: the first tradable session.
            price("2026-09-07", "AAA.AX", 20.0, 22.0),
        ],
    )
    update = update_portfolio(MARKET)
    assert len(update.executions) == 1
    execution = update.executions[0]
    assert execution["execution_date"] == "2026-09-07"
    assert execution["signal_date"] == "2026-09-04"
    assert execution["positions"][0]["execution_open_price"] == pytest.approx(20.0)
    # Bought at 20.00 and marked at 22.00: +10% on the day, never +120%.
    assert update.nav == pytest.approx(CONFIG.STARTING_NAV * 1.10)


def test_no_performance_exists_before_the_first_signal(data_root):
    upsert_prices(MARKET, [price("2026-08-03", "AAA.AX", 9.0, 10.0)])
    update = update_portfolio(MARKET)
    assert update.sessions_processed == 0
    assert load_performance(MARKET) == []
    assert "no signal has been generated yet" in " ".join(update.notes)


def test_sessions_before_the_signal_are_never_priced(data_root):
    write_signal({"AAA.AX": 1.0})
    upsert_prices(
        MARKET,
        [
            price("2026-08-03", "AAA.AX", 5.0, 5.0),
            price("2026-09-04", "AAA.AX", 9.0, 10.0),
            price("2026-09-07", "AAA.AX", 10.0, 11.0),
        ],
    )
    update_portfolio(MARKET)
    dates = [r["date"] for r in load_performance(MARKET)]
    assert dates == ["2026-09-07"]


def test_the_portfolio_drifts_and_is_not_rebalanced_daily(data_root):
    write_signal({"AAA.AX": 0.5, "BBB.AX": 0.5})
    upsert_prices(
        MARKET,
        [
            price("2026-09-07", "AAA.AX", 10.0, 10.0),
            price("2026-09-07", "BBB.AX", 10.0, 10.0),
            price("2026-09-08", "AAA.AX", 10.0, 20.0),
            price("2026-09-08", "BBB.AX", 10.0, 10.0),
        ],
    )
    update_portfolio(MARKET)
    state = load_state(MARKET)
    assert state["positions"]["AAA.AX"]["drifted_weight"] == pytest.approx(2 / 3)
    assert state["positions"]["BBB.AX"]["drifted_weight"] == pytest.approx(1 / 3)
    # Share counts are unchanged: no daily rebalancing took place.
    assert state["positions"]["AAA.AX"]["shares"] == pytest.approx(5.0)
    assert update_portfolio(MARKET).sessions_processed == 0


def test_dividends_are_added_to_cash_and_lift_total_return(data_root):
    write_signal({"AAA.AX": 1.0})
    upsert_prices(
        MARKET,
        [
            price("2026-09-07", "AAA.AX", 10.0, 10.0),
            # Ex-dividend of 1.00 with the price unchanged.
            price("2026-09-08", "AAA.AX", 10.0, 10.0, dividend=1.0),
        ],
    )
    update_portfolio(MARKET)
    state = load_state(MARKET)
    assert state["cash"] == pytest.approx(10.0)  # 10 shares x $1.00
    assert state["nav"] == pytest.approx(110.0)
    rows = load_performance(MARKET)
    assert rows[-1]["event"].startswith("dividend")
    assert float(rows[-1]["daily_return"]) == pytest.approx(0.10)


def test_a_stock_split_adjusts_shares_and_leaves_nav_unchanged(data_root):
    write_signal({"AAA.AX": 1.0})
    upsert_prices(
        MARKET,
        [
            price("2026-09-07", "AAA.AX", 10.0, 10.0),
            # Two-for-one: the price halves and the share count doubles.
            price("2026-09-08", "AAA.AX", 5.0, 5.0, split=2.0),
        ],
    )
    update_portfolio(MARKET)
    state = load_state(MARKET)
    assert state["positions"]["AAA.AX"]["shares"] == pytest.approx(20.0)
    assert state["nav"] == pytest.approx(100.0)
    assert "split:AAA.AX" in load_performance(MARKET)[-1]["event"]


def test_a_second_signal_closes_the_old_book_at_the_next_open(data_root):
    write_signal({"AAA.AX": 1.0}, date(2026, 6, 5))
    write_signal({"BBB.AX": 1.0}, date(2026, 9, 4))
    upsert_prices(
        MARKET,
        [
            price("2026-06-08", "AAA.AX", 10.0, 10.0),
            price("2026-09-04", "AAA.AX", 12.0, 12.0),
            price("2026-09-07", "AAA.AX", 12.0, 12.0),
            price("2026-09-07", "BBB.AX", 24.0, 24.0),
        ],
    )
    update = update_portfolio(MARKET)
    assert [e["signal_date"] for e in update.executions] == ["2026-06-05", "2026-09-04"]
    second = update.executions[1]
    assert second["sold"] == ["AAA.AX"]
    assert second["bought"] == ["BBB.AX"]
    assert second["turnover"] == pytest.approx(1.0)
    state = load_state(MARKET)
    assert set(state["positions"]) == {"BBB.AX"}
    # 100 -> 120 on AAA, then fully into BBB.
    assert state["nav"] == pytest.approx(120.0)


def test_transaction_costs_are_applied_when_configured(data_root, monkeypatch):
    from dataclasses import replace as dc_replace

    costed = dc_replace(CONFIG, TRANSACTION_COST_BPS=50.0)
    write_signal({"AAA.AX": 1.0})
    upsert_prices(
        MARKET,
        [price("2026-09-04", "AAA.AX", 10.0, 10.0), price("2026-09-07", "AAA.AX", 10.0, 10.0)],
    )
    update = update_portfolio(MARKET, config=costed)
    # One-way turnover of 100% at 50bps costs 0.5% of NAV.
    assert update.executions[0]["transaction_cost"] == pytest.approx(0.5)
    assert update.nav == pytest.approx(99.5)


def test_a_missing_opening_price_redistributes_that_weight(data_root):
    write_signal({"AAA.AX": 0.5, "HALTED.AX": 0.5})
    upsert_prices(
        MARKET,
        [price("2026-09-04", "AAA.AX", 10.0, 10.0), price("2026-09-07", "AAA.AX", 10.0, 10.0)],
    )
    update = update_portfolio(MARKET)
    execution = update.executions[0]
    assert execution["missing_at_execution"] == ["HALTED.AX"]
    assert execution["positions"][0]["effective_target_weight"] == pytest.approx(1.0)
    assert update.nav == pytest.approx(100.0)


def test_a_missing_close_carries_the_last_price_forward(data_root):
    write_signal({"AAA.AX": 0.5, "BBB.AX": 0.5})
    upsert_prices(
        MARKET,
        [
            price("2026-09-07", "AAA.AX", 10.0, 10.0),
            price("2026-09-07", "BBB.AX", 10.0, 10.0),
            # BBB does not trade on the 8th.
            price("2026-09-08", "AAA.AX", 10.0, 12.0),
        ],
    )
    update_portfolio(MARKET)
    state = load_state(MARKET)
    assert state["positions"]["BBB.AX"]["last_price"] == pytest.approx(10.0)
    assert state["nav"] == pytest.approx(110.0)


def test_a_holiday_with_no_sessions_produces_no_rows(data_root):
    write_signal({"AAA.AX": 1.0})
    upsert_prices(MARKET, [price("2026-09-07", "AAA.AX", 10.0, 10.0)])
    update_portfolio(MARKET)
    before = len(load_performance(MARKET))
    # Running again with no new bars must be a no-op.
    update = update_portfolio(MARKET)
    assert update.sessions_processed == 0
    assert len(load_performance(MARKET)) == before


def test_running_twice_is_idempotent(data_root):
    write_signal({"AAA.AX": 1.0})
    upsert_prices(
        MARKET,
        [price("2026-09-07", "AAA.AX", 10.0, 10.0), price("2026-09-08", "AAA.AX", 10.0, 11.0)],
    )
    first = update_portfolio(MARKET)
    second = update_portfolio(MARKET)
    assert second.sessions_processed == 0
    assert second.nav == pytest.approx(first.nav)
    assert len(load_performance(MARKET)) == 2
    assert len(load_state(MARKET)["executions"]) == 1


def test_cumulative_return_is_measured_from_the_starting_nav(data_root):
    write_signal({"AAA.AX": 1.0})
    upsert_prices(
        MARKET,
        [price("2026-09-07", "AAA.AX", 10.0, 10.0), price("2026-09-08", "AAA.AX", 10.0, 12.0)],
    )
    update_portfolio(MARKET)
    rows = load_performance(MARKET)
    assert float(rows[-1]["cumulative_return"]) == pytest.approx(0.20)
    assert float(rows[-1]["nav"]) == pytest.approx(120.0)


def test_active_tickers_covers_held_and_pending_names(data_root):
    write_signal({"AAA.AX": 1.0}, date(2026, 6, 5))
    upsert_prices(
        MARKET,
        [price("2026-06-08", "AAA.AX", 10.0, 10.0)],
    )
    update_portfolio(MARKET)
    write_signal({"BBB.AX": 1.0}, date(2026, 9, 4))
    assert active_tickers(MARKET) == ["AAA.AX", "BBB.AX"]


def test_a_fresh_state_starts_flat_at_the_configured_nav():
    state = new_state("NZ")
    assert state["nav"] == CONFIG.STARTING_NAV
    assert state["cash"] == CONFIG.STARTING_NAV
    assert state["positions"] == {}
    assert state["inception_date"] is None


def test_each_market_keeps_its_own_independent_portfolio(data_root):
    for market in ("AU", "NZ"):
        write_signal_snapshot(
            market,
            SIGNAL_DATE,
            universe_rows=[],
            raw_financial_rows=[],
            factor_rows=[],
            selected_rows=[{"ticker": f"X.{market}", "target_weight": 1.0}],
            excluded_rows=[],
            metadata={"signal_timestamp": "t0"},
        )
        upsert_prices(
            market,
            [
                {
                    "date": "2026-09-07",
                    "ticker": f"X.{market}",
                    "market": market,
                    "currency": "AUD" if market == "AU" else "NZD",
                    "open": 10.0,
                    "close": 11.0 if market == "AU" else 9.0,
                    "dividend": 0.0,
                    "split": 0.0,
                }
            ],
        )
        update_portfolio(market)
    assert load_state("AU")["nav"] == pytest.approx(110.0)
    assert load_state("NZ")["nav"] == pytest.approx(90.0)


def test_state_survives_a_round_trip(data_root):
    state = new_state("CA")
    state["nav"] = 123.456
    save_state("CA", state)
    assert load_state("CA")["nav"] == pytest.approx(123.456)


def test_an_empty_bar_is_not_treated_as_a_tradable_session(data_root):
    """A session that has not opened yet must not trigger execution."""
    write_signal({"AAA.AX": 1.0})
    blank = price("2026-09-16", "AAA.AX", 0.0, 0.0)
    for field in ("open", "high", "low", "close"):
        blank[field] = ""
    upsert_prices(MARKET, [blank])
    update = update_portfolio(MARKET)
    assert update.executions == []
    assert update.sessions_processed == 0
    assert load_state(MARKET) is None or not load_state(MARKET).get("positions")


def test_execution_happens_once_the_real_session_data_replaces_the_placeholder(data_root):
    write_signal({"AAA.AX": 1.0})
    blank = price("2026-09-16", "AAA.AX", 0.0, 0.0)
    for field in ("open", "high", "low", "close"):
        blank[field] = ""
    upsert_prices(MARKET, [blank])
    assert update_portfolio(MARKET).executions == []
    # The completed session arrives and heals the placeholder.
    upsert_prices(MARKET, [price("2026-09-16", "AAA.AX", 20.0, 22.0)])
    update = update_portfolio(MARKET)
    assert len(update.executions) == 1
    assert update.executions[0]["positions"][0]["execution_open_price"] == pytest.approx(20.0)
    assert update.nav == pytest.approx(110.0)
