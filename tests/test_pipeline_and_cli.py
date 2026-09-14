"""Whole-pipeline behaviour and the command-line surface, all offline."""

from __future__ import annotations

import json
from datetime import date

import pytest

from fcf_factor.cli import main
from fcf_factor.config import CONFIG, METHODOLOGY_VERSION
from fcf_factor.pipeline import SyntheticDataRefused, persist_screen, run_screen
from fcf_factor.providers.synthetic import SyntheticProvider, synthetic_universe_symbols
from fcf_factor.storage import load_signal, read_csv, signal_dir
from fcf_factor.universe.base import UniverseEntry, UniverseResult

SIGNAL_DATE = date(2026, 9, 4)


def offline_universe(market: str, count: int = 80) -> UniverseResult:
    entries = [
        UniverseEntry(
            market=market,
            exchange="SYNTHETIC",
            exchange_symbol=symbol.split(".")[0],
            yahoo_symbol=symbol,
            source="synthetic",
        )
        for symbol in synthetic_universe_symbols(market, count)
    ]
    return UniverseResult(market=market, entries=entries, sources=["synthetic"])


def run(market: str = "NZ", count: int = 80):
    return run_screen(
        market,
        SyntheticProvider(today=SIGNAL_DATE),
        SIGNAL_DATE,
        universe=offline_universe(market, count),
        max_workers=2,
    )


def test_the_pipeline_runs_end_to_end_and_selects_a_portfolio(data_root):
    result = run()
    assert result.coverage.universe_size == 80
    assert result.coverage.eligible > 0
    assert 0 < len(result.selected) <= len(result.shortlist)
    assert sum(c.target_weight for c in result.selected) == pytest.approx(1.0, abs=1e-9)


def test_the_funnel_proportions_hold_on_a_realistic_universe(data_root):
    result = run(count=200)
    eligible = result.coverage.eligible
    assert result.coverage.value_shortlist == pytest.approx(
        round(eligible * CONFIG.VALUE_SCREEN_PERCENTILE), abs=1
    )
    assert len(result.selected) <= result.coverage.value_shortlist


def test_every_excluded_company_carries_a_reason(data_root):
    result = run()
    excluded = result.excluded_rows()
    assert excluded
    assert all(row["reasons"] for row in excluded)
    assert all(row["stage"] for row in excluded)


def test_metadata_records_the_methodology_and_config_hash(data_root):
    metadata = run().metadata()
    assert metadata["methodology_version"] == METHODOLOGY_VERSION
    assert metadata["config_hash"] == CONFIG.config_hash()
    assert len(metadata["config_hash"]) == 64
    assert metadata["config"]["MIN_MARKET_CAP_USD"] == CONFIG.MIN_MARKET_CAP_USD
    assert "next available market open" in metadata["execution_rule"]
    assert metadata["fx_rates_to_usd"]["USD"] == 1.0


def test_the_config_hash_changes_when_a_parameter_changes():
    from dataclasses import replace

    assert CONFIG.config_hash() != replace(CONFIG, MAX_STOCK_WEIGHT=0.05).config_hash()


def test_synthetic_results_are_never_written_into_the_research_record(data_root):
    result = run()
    with pytest.raises(SyntheticDataRefused):
        persist_screen(result)
    assert not signal_dir("NZ", SIGNAL_DATE).exists()


def test_a_real_snapshot_writes_every_file_with_the_documented_columns(data_root):
    result = run()
    result.provider = "yahoo"  # pretend the data came from the real provider
    path = persist_screen(result)
    snapshot = load_signal("NZ", SIGNAL_DATE)
    assert snapshot["metadata"]["market"] == "NZ"
    assert len(snapshot["selected"]) == len(result.selected)
    selected_columns = set(read_csv(signal_dir("NZ", SIGNAL_DATE) / "selected.csv")[0])
    for column in (
        "ticker", "company", "sector", "market", "market_cap_local", "market_cap_usd",
        "enterprise_value_usd", "ttm_fcf", "normalized_fcf_margin", "forward_growth",
        "forward_fcf", "expected_fcf", "fcf_yield", "revenue_trend", "ebitda_trend",
        "fcf_per_share_trend", "z_revenue", "z_ebitda", "z_fcf_per_share",
        "growth_score", "fcf_yield_rank", "growth_rank", "target_weight",
        "data_quality_flags",
    ):
        assert column in selected_columns, column
    assert str(path).endswith("2026-09-04")


def test_raw_financials_are_stored_for_the_names_that_were_actually_screened(data_root):
    result = run()
    result.provider = "yahoo"
    persist_screen(result)
    rows = read_csv(signal_dir("NZ", SIGNAL_DATE) / "raw_financials.csv")
    assert rows
    assert {"annual", "quarterly"} <= {r["period_type"] for r in rows}
    assert all(r["ticker"].endswith(".NZ") for r in rows)


def test_a_stale_universe_is_flagged_all_the_way_into_the_metadata(data_root):
    universe = offline_universe("NZ")
    universe.is_stale = True
    universe.stale_reason = "refresh failed (simulated)"
    result = run_screen(
        "NZ", SyntheticProvider(today=SIGNAL_DATE), SIGNAL_DATE, universe=universe, max_workers=2
    )
    assert any("STALE UNIVERSE" in note for note in result.notes)
    assert result.metadata()["universe"]["is_stale"] is True


def test_markets_are_screened_independently(data_root):
    nz = run("NZ")
    au = run("AU")
    assert {c.ticker for c in nz.selected}.isdisjoint({c.ticker for c in au.selected})
    assert all(c.ticker.endswith(".NZ") for c in nz.selected)
    assert all(c.ticker.endswith(".AX") for c in au.selected)


def test_uk_market_handles_pence_quotes_through_the_whole_pipeline(data_root):
    result = run("UK")
    assert result.coverage.eligible > 0
    for company in result.selected:
        assert company.currency == "GBP"
        assert "price_quoted_in_minor_units" in company.flags
        # A 100x error would put every UK company far above the others.
        assert company.market_cap_usd < 1e12


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def test_cli_smoke_test_runs_a_dry_screen(data_root, capsys):
    code = main(
        ["screen", "--market", "NZ", "--limit", "20", "--dry-run", "--provider", "synthetic"]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "screen"
    assert payload["markets"]["NZ"]["dry_run"] is True
    assert payload["markets"]["NZ"]["snapshot"] is None
    assert not signal_dir("NZ", date.fromisoformat(payload["markets"]["NZ"]["signal_date"])).exists()


def test_cli_check_rebalance_reports_the_schedule(capsys):
    assert main(["check-rebalance", "--date", "2026-09-05"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["due"] == "true"
    assert payload["signal_date"] == "2026-09-04"

    assert main(["check-rebalance", "--date", "2026-09-12"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["due"] == "false"


def test_cli_status_reports_configuration_and_every_market(data_root, capsys):
    assert main(["status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["config_hash"] == CONFIG.config_hash()
    assert set(payload["markets"]) == {"AU", "US", "UK", "NZ", "CA"}


def test_cli_quarterly_run_does_nothing_when_no_rebalance_is_due(data_root, capsys):
    code = main(["quarterly-run", "--run-date", "2026-09-12", "--provider", "synthetic", "--no-email"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert all(entry["skipped"] for entry in payload["markets"].values())


def test_cli_rejects_an_unknown_market(data_root):
    with pytest.raises(SystemExit):
        main(["screen", "--market", "ZZ", "--dry-run", "--provider", "synthetic"])


def test_cli_report_writes_every_markdown_file(data_root, capsys):
    assert main(["report"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["written"]) == 6  # five markets plus the index
