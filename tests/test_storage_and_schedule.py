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


def test_no_rebalance_on_later_saturdays_in_the_month():
    for day in (date(2026, 9, 12), date(2026, 9, 19), date(2026, 9, 26)):
        assert scheduled_rebalance_decision(day).due is False


def test_no_rebalance_outside_the_four_rebalance_months():
    assert scheduled_rebalance_decision(date(2026, 10, 3)).due is False
    assert "not a rebalance month" in scheduled_rebalance_decision(date(2026, 10, 3)).reason


def test_no_rebalance_on_a_non_saturday():
    assert scheduled_rebalance_decision(date(2026, 9, 4)).due is False


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
    assert upsert_prices("AU", rows) == {"added": 2, "skipped_existing": 0}
    # Re-running the same job (the 7-10 day lookback) must add nothing.
    assert upsert_prices("AU", rows) == {"added": 0, "skipped_existing": 2}
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
    assert stats == {"added": 1, "skipped_existing": 2}
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
