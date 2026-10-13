"""Snapshot immutability, price de-duplication and the rebalance calendar."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from fcf_factor.schedule import (
    first_friday,
    is_first_friday,
    last_completed_session,
    next_business_day,
    quarter_label,
    resolve_signal_date,
    scheduled_rebalance_decision,
)
from fcf_factor.storage import (
    SnapshotExistsError,
    latest_signal_date,
    list_signal_dates,
    load_prices,
    load_signal,
    read_csv,
    signal_exists,
    upsert_prices,
    write_signal_snapshot,
)


# --------------------------------------------------------------------------
# Schedule
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("year", "month", "expected"),
    [(2026, 3, date(2026, 3, 6)), (2026, 6, date(2026, 6, 5)), (2026, 9, date(2026, 9, 4)), (2026, 12, date(2026, 12, 4))],
)
def test_first_friday_of_each_rebalance_month(year, month, expected):
    assert first_friday(year, month) == expected
    assert is_first_friday(expected)


def test_rebalance_is_due_on_the_saturday_after_the_first_friday():
    decision = scheduled_rebalance_decision(date(2026, 9, 5))
    assert decision.due is True
    assert decision.signal_date == date(2026, 9, 4)


def test_no_rebalance_on_later_weekends_in_the_month():
    for day in (date(2026, 9, 12), date(2026, 9, 13), date(2026, 9, 19), date(2026, 9, 27)):
        assert scheduled_rebalance_decision(day).due is False


def test_no_rebalance_outside_the_four_rebalance_months():
    assert scheduled_rebalance_decision(date(2026, 10, 3)).due is False
    assert "not a rebalance month" in scheduled_rebalance_decision(date(2026, 10, 3)).reason


def test_no_rebalance_on_a_weekday():
    assert scheduled_rebalance_decision(date(2026, 9, 4)).due is False
    assert scheduled_rebalance_decision(date(2026, 9, 7)).due is False


def test_a_late_sunday_run_still_rebalances_for_the_same_friday():
    """GitHub runs scheduled jobs late; Sunday must still find Friday's signal."""
    decision = scheduled_rebalance_decision(date(2026, 12, 6))
    assert decision.due is True
    assert decision.signal_date == date(2026, 12, 4)


def test_the_december_2026_rebalance_falls_on_saturday_5_december():
    decision = scheduled_rebalance_decision(date(2026, 12, 5))
    assert decision.due is True
    assert decision.signal_date == date(2026, 12, 4)


def test_the_check_command_runs_on_the_installed_package(capsys):
    """Regression: the workflow's decision step crashed before deciding."""
    import json

    from fcf_factor.cli import main

    assert main(["check-rebalance", "--date", "2026-12-06"]) == 0
    assert json.loads(capsys.readouterr().out)["due"] == "true"


def test_manual_runs_use_the_latest_completed_session():
    decision = resolve_signal_date("US", manual=True, now=datetime(2026, 9, 9, 23, 0, tzinfo=UTC))
    assert decision.due is True
    assert decision.signal_date == date(2026, 9, 9)


def test_a_session_that_has_not_closed_yet_is_not_used():
    # 12:00 UTC on a Wednesday is 08:00 in New York: the session is still open.
    assert last_completed_session("US", datetime(2026, 9, 9, 12, 0, tzinfo=UTC)) == date(2026, 9, 8)


def test_weekends_step_back_to_friday():
    assert last_completed_session("AU", datetime(2026, 9, 13, 12, 0, tzinfo=UTC)) == date(2026, 9, 11)


def test_next_business_day_skips_the_weekend():
    assert next_business_day(date(2026, 9, 4)) == date(2026, 9, 7)
    assert next_business_day(date(2026, 9, 7)) == date(2026, 9, 8)


def test_quarter_labels():
    assert quarter_label(date(2026, 3, 6)) == "2026 Q1"
    assert quarter_label(date(2026, 12, 4)) == "2026 Q4"


# --------------------------------------------------------------------------
# Snapshot immutability
# --------------------------------------------------------------------------
def _write(market: str, signal_date: date, tickers: list[str], data_root):
    return write_signal_snapshot(
        market,
        signal_date,
        universe_rows=[{"market": market, "yahoo_symbol": t} for t in tickers],
        raw_financial_rows=[{"ticker": t, "revenue": 1.0} for t in tickers],
        factor_rows=[{"ticker": t, "eligible": True} for t in tickers],
        selected_rows=[{"ticker": t, "target_weight": 1.0 / len(tickers)} for t in tickers],
        excluded_rows=[{"yahoo_symbol": "GONE", "reasons": "symbol_unresolved"}],
        metadata={"market": market, "signal_date": signal_date.isoformat(), "signal_timestamp": "t0"},
    )


def test_a_snapshot_is_written_with_every_expected_file(data_root):
    path = _write("AU", date(2026, 9, 4), ["BHP.AX", "WTC.AX"], data_root)
    for name in ("universe.csv", "raw_financials.csv", "factor_scores.csv", "selected.csv", "excluded.csv", "metadata.json"):
        assert (path / name).exists(), name
    assert signal_exists("AU", date(2026, 9, 4))


def test_a_snapshot_is_never_silently_overwritten(data_root):
    _write("AU", date(2026, 9, 4), ["BHP.AX"], data_root)
    with pytest.raises(SnapshotExistsError):
        _write("AU", date(2026, 9, 4), ["DIFFERENT.AX"], data_root)
    # The original record is intact.
    selected = load_signal("AU", date(2026, 9, 4))["selected"]
    assert [r["ticker"] for r in selected] == ["BHP.AX"]


def test_signal_dates_are_listed_in_order(data_root):
    _write("AU", date(2026, 3, 6), ["A.AX"], data_root)
    _write("AU", date(2026, 6, 5), ["B.AX"], data_root)
    _write("AU", date(2026, 9, 4), ["C.AX"], data_root)
    assert list_signal_dates("AU") == [date(2026, 3, 6), date(2026, 6, 5), date(2026, 9, 4)]
    assert latest_signal_date("AU") == date(2026, 9, 4)
    assert latest_signal_date("AU", before=date(2026, 9, 4)) == date(2026, 6, 5)


def test_markets_keep_entirely_separate_snapshots(data_root):
    _write("AU", date(2026, 9, 4), ["BHP.AX"], data_root)
    _write("NZ", date(2026, 9, 4), ["AIR.NZ"], data_root)
    assert [r["ticker"] for r in load_signal("AU", date(2026, 9, 4))["selected"]] == ["BHP.AX"]
    assert [r["ticker"] for r in load_signal("NZ", date(2026, 9, 4))["selected"]] == ["AIR.NZ"]


# --------------------------------------------------------------------------
# Daily price storage
# --------------------------------------------------------------------------
def _price_row(day: str, ticker: str, close: float) -> dict:
    return {
        "date": day,
        "ticker": ticker,
        "market": "AU",
        "currency": "AUD",
        "open": close * 0.99,
        "high": close,
        "low": close * 0.98,
        "close": close,
        "adj_close": close,
        "volume": 1000,
        "dividend": 0.0,
        "split": 0.0,
        "fx_rate_to_usd": 0.65,
        "retrieved_at": "2026-09-07T23:35:00+00:00",
    }


def test_duplicate_date_ticker_rows_are_never_created(data_root):
    rows = [_price_row("2026-09-07", "BHP.AX", 40.0), _price_row("2026-09-08", "BHP.AX", 41.0)]
    assert upsert_prices("AU", rows) == {"added": 2, "skipped_existing": 0, "replaced_placeholder": 0, "replaced_provisional": 0}
    # Re-running the same job (the 7-10 day lookback) must add nothing.
    assert upsert_prices("AU", rows) == {"added": 0, "skipped_existing": 2, "replaced_placeholder": 0, "replaced_provisional": 0}
    assert len(load_prices("AU")) == 2


def test_an_existing_row_is_not_rewritten_by_a_later_correction(data_root):
    upsert_prices("AU", [_price_row("2026-09-07", "BHP.AX", 40.0)])
    upsert_prices("AU", [_price_row("2026-09-07", "BHP.AX", 99.0)])
    stored = load_prices("AU")
    assert len(stored) == 1
    assert float(stored[0]["close"]) == pytest.approx(40.0)


def test_backfilling_a_gap_adds_only_the_missing_session(data_root):
    upsert_prices("AU", [_price_row("2026-09-07", "BHP.AX", 40.0), _price_row("2026-09-09", "BHP.AX", 42.0)])
    stats = upsert_prices(
        "AU",
        [
            _price_row("2026-09-07", "BHP.AX", 40.0),
            _price_row("2026-09-08", "BHP.AX", 41.0),
            _price_row("2026-09-09", "BHP.AX", 42.0),
        ],
    )
    assert stats == {"added": 1, "skipped_existing": 2, "replaced_placeholder": 0, "replaced_provisional": 0}
    assert [r["date"] for r in load_prices("AU")] == ["2026-09-07", "2026-09-08", "2026-09-09"]


def test_prices_are_split_across_monthly_files_and_stay_sorted(data_root):
    upsert_prices(
        "AU",
        [
            _price_row("2026-10-01", "WTC.AX", 100.0),
            _price_row("2026-09-30", "BHP.AX", 40.0),
            _price_row("2026-09-30", "AAA.AX", 5.0),
        ],
    )
    from fcf_factor.storage import price_path

    assert price_path("AU", "2026-09").exists()
    assert price_path("AU", "2026-10").exists()
    september = read_csv(price_path("AU", "2026-09"))
    assert [r["ticker"] for r in september] == ["AAA.AX", "BHP.AX"]


# --------------------------------------------------------------------------
# Placeholder bars
# --------------------------------------------------------------------------
def _blank_row(day: str, ticker: str) -> dict:
    """A bar for a session that has not opened yet: dated, but priceless."""
    row = _price_row(day, ticker, 0.0)
    for field in ("open", "high", "low", "close", "adj_close"):
        row[field] = ""
    row["volume"] = 0
    return row


def test_a_placeholder_row_is_replaced_once_the_real_session_arrives(data_root):
    """An empty bar is not an observation, so it must not become permanent."""
    upsert_prices("AU", [_blank_row("2026-09-16", "SRV.AX")])
    stats = upsert_prices("AU", [_price_row("2026-09-16", "SRV.AX", 4.20)])
    assert stats["replaced_placeholder"] == 1
    assert stats["added"] == 0
    stored = load_prices("AU")
    assert len(stored) == 1
    assert float(stored[0]["close"]) == pytest.approx(4.20)


def test_a_real_row_is_still_never_overwritten_by_a_placeholder(data_root):
    upsert_prices("AU", [_price_row("2026-09-16", "SRV.AX", 4.20)])
    stats = upsert_prices("AU", [_blank_row("2026-09-16", "SRV.AX")])
    assert stats["skipped_existing"] == 1
    assert float(load_prices("AU")[0]["close"]) == pytest.approx(4.20)


def test_a_real_row_is_still_never_overwritten_by_a_later_correction(data_root):
    upsert_prices("AU", [_price_row("2026-09-16", "SRV.AX", 4.20)])
    upsert_prices("AU", [_price_row("2026-09-16", "SRV.AX", 99.0)])
    assert float(load_prices("AU")[0]["close"]) == pytest.approx(4.20)


def test_has_price_rejects_blank_and_non_numeric_closes():
    from fcf_factor.storage import has_price

    assert has_price({"close": 4.2}) is True
    assert has_price({"close": "4.2"}) is True
    assert has_price({"close": ""}) is False
    assert has_price({"close": None}) is False
    assert has_price({"close": "0"}) is False
    assert has_price({}) is False


# --------------------------------------------------------------------------
# Mid-session bars (GitHub runs the daily job hours late)
# --------------------------------------------------------------------------
def test_a_bar_fetched_mid_session_is_provisional():
    from fcf_factor.storage import is_provisional

    # 03:00 UTC on 13 Oct is 14:00 in Sydney: the ASX is still trading.
    midday = _price_row("2026-10-13", "BHP.AX", 40.0) | {"retrieved_at": "2026-10-13T03:00:00+00:00"}
    after = _price_row("2026-10-13", "BHP.AX", 40.0) | {"retrieved_at": "2026-10-13T23:35:00+00:00"}
    assert is_provisional("AU", midday) is True
    assert is_provisional("AU", after) is False


def test_a_provisional_bar_is_replaced_by_the_real_close(data_root):
    midday = _price_row("2026-10-13", "BHP.AX", 40.0) | {"retrieved_at": "2026-10-13T03:00:00+00:00"}
    final = _price_row("2026-10-13", "BHP.AX", 41.5) | {"retrieved_at": "2026-10-14T03:00:00+00:00"}
    upsert_prices("AU", [midday])
    stats = upsert_prices("AU", [final])
    assert stats["replaced_provisional"] == 1
    assert float(load_prices("AU")[0]["close"]) == pytest.approx(41.5)


def test_a_final_close_is_never_replaced_even_by_another_final_fetch(data_root):
    first = _price_row("2026-10-13", "BHP.AX", 41.5) | {"retrieved_at": "2026-10-14T03:00:00+00:00"}
    later = _price_row("2026-10-13", "BHP.AX", 99.0) | {"retrieved_at": "2026-10-20T03:00:00+00:00"}
    upsert_prices("AU", [first])
    assert upsert_prices("AU", [later])["skipped_existing"] == 1
    assert float(load_prices("AU")[0]["close"]) == pytest.approx(41.5)


def test_a_provisional_bar_is_not_replaced_by_another_provisional_bar(data_root):
    a = _price_row("2026-10-13", "BHP.AX", 40.0) | {"retrieved_at": "2026-10-13T01:00:00+00:00"}
    b = _price_row("2026-10-13", "BHP.AX", 40.7) | {"retrieved_at": "2026-10-13T03:00:00+00:00"}
    upsert_prices("AU", [a])
    assert upsert_prices("AU", [b])["skipped_existing"] == 1


def test_collection_skips_the_session_that_is_still_trading(data_root):
    """At 03:00 UTC on Tuesday 13 Oct only Monday's ASX session is complete."""
    from datetime import UTC, datetime

    from fcf_factor.portfolio.prices import collect_prices
    from tests.conftest import bar

    class Fake:
        name = "fake"

        def get_prices(self, symbol, start, end):
            return [bar(date(2026, 10, 12), 40.0), bar(date(2026, 10, 13), 40.5)]

        def get_fx_rate(self, base, quote="USD"):
            return 0.65

    result = collect_prices(
        "AU", Fake(), ["BHP.AX"], end=date(2026, 10, 13),
        now=datetime(2026, 10, 13, 3, 0, tzinfo=UTC),
    )
    assert result.skipped_open_session == 1
    assert [r["date"] for r in load_prices("AU")] == ["2026-10-12"]


def test_us_bars_are_kept_once_new_york_has_closed(data_root):
    from datetime import UTC, datetime

    from fcf_factor.portfolio.prices import collect_prices
    from tests.conftest import bar

    class Fake:
        name = "fake"

        def get_prices(self, symbol, start, end):
            return [bar(date(2026, 10, 12), 100.0, currency="USD")]

        def get_fx_rate(self, base, quote="USD"):
            return 1.0

    # 03:00 UTC Tuesday is 23:00 Monday in New York: Monday is closed.
    collect_prices("US", Fake(), ["AAPL"], end=date(2026, 10, 13),
                   now=datetime(2026, 10, 13, 3, 0, tzinfo=UTC))
    assert [r["date"] for r in load_prices("US")] == ["2026-10-12"]
