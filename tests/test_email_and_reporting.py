"""Email construction, credential handling and the markdown reports."""

from __future__ import annotations

from datetime import date

import pytest

from fcf_factor.notify.email import (
    EmailConfig,
    EmailConfigurationError,
    build_email_html,
    build_message,
    send_quarterly_email,
)
from fcf_factor.reporting import build_market_report, write_index_report, write_market_report
from fcf_factor.storage import upsert_prices, write_signal_snapshot

SIGNAL_DATE = date(2026, 9, 4)

ENV = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "robot@example.com",
    "SMTP_PASSWORD": "not-a-real-password",
    "EMAIL_FROM": "robot@example.com",
    "EMAIL_TO": "analyst@example.com, second@example.com",
}


@pytest.fixture
def snapshot(data_root):
    write_signal_snapshot(
        "AU",
        SIGNAL_DATE,
        universe_rows=[{"yahoo_symbol": "AAA.AX"}, {"yahoo_symbol": "BBB.AX"}],
        raw_financial_rows=[],
        factor_rows=[],
        selected_rows=[
            {
                "ticker": "AAA.AX",
                "company": "Alpha Industries Limited",
                "sector": "Technology",
                "market_cap_usd": 450_000_000.0,
                "fcf_yield": 0.11,
                "growth_score": 0.85,
                "target_weight": 0.6,
            },
            {
                "ticker": "BBB.AX",
                "company": "Beta Mining Limited",
                "sector": "Basic Materials",
                "market_cap_usd": 220_000_000.0,
                "fcf_yield": 0.09,
                "growth_score": 0.40,
                "target_weight": 0.4,
            },
        ],
        excluded_rows=[{"yahoo_symbol": "CCC.AX", "reasons": "below_min_market_cap:1000"}],
        metadata={
            "market": "AU",
            "signal_date": SIGNAL_DATE.isoformat(),
            "signal_timestamp": "2026-09-04T09:00:00+00:00",
            "config_hash": "abc123def456",
            "data_provider": "yahoo",
            "coverage": {
                "universe_size": 2000,
                "metadata_resolved": 1900,
                "passed_security_filters": 1500,
                "above_market_cap": 800,
                "eligible": 600,
                "value_shortlist": 113,
                "selected": 2,
                "metadata_coverage": 0.95,
                "financial_coverage": 0.75,
                "warnings": ["universe refreshed from a secondary source"],
                "exclusion_counts": {"below_min_market_cap": 700},
                "flag_counts": {"extreme_fcf_yield": 3},
            },
            "universe": {"size": 2000, "is_stale": False, "sources": ["asx"]},
            "weighting": {"sector_weights": {"Technology": 0.6, "Basic Materials": 0.4}},
            "notes": [],
        },
    )
    return SIGNAL_DATE


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------
def test_missing_credentials_raise_rather_than_silently_skipping():
    with pytest.raises(EmailConfigurationError) as excinfo:
        EmailConfig.from_env({})
    message = str(excinfo.value)
    for name in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO"):
        assert name in message


def test_a_partially_configured_mailer_names_the_missing_variable():
    partial = {k: v for k, v in ENV.items() if k != "SMTP_PASSWORD"}
    with pytest.raises(EmailConfigurationError, match="SMTP_PASSWORD"):
        EmailConfig.from_env(partial)


def test_a_non_numeric_port_is_rejected():
    with pytest.raises(EmailConfigurationError, match="SMTP_PORT"):
        EmailConfig.from_env({**ENV, "SMTP_PORT": "not-a-port"})


def test_multiple_recipients_are_parsed():
    config = EmailConfig.from_env(ENV)
    assert config.recipients == ["analyst@example.com", "second@example.com"]


def test_sending_fails_loudly_when_credentials_are_absent(snapshot):
    with pytest.raises(EmailConfigurationError):
        send_quarterly_email("AU", snapshot, env={}, dry_run=True)


# --------------------------------------------------------------------------
# Message content
# --------------------------------------------------------------------------
def test_the_email_states_the_execution_rule(snapshot):
    html = build_email_html("AU", snapshot)
    assert (
        "Signal generated after market close. Forward-test execution occurs at the "
        "next available market open." in html
    )


def test_the_email_contains_the_funnel_and_the_picks(snapshot):
    html = build_email_html("AU", snapshot)
    for fragment in (
        "Raw universe", "2000", "Above US$50,000,000 market cap", "800",
        "Surviving financial-data filters", "600", "FCF-yield shortlist", "113",
        "AAA.AX", "Alpha Industries Limited", "Technology", "11.00%", "60.00%",
        "Changes versus the previous rebalance", "Forward test",
    ):
        assert fragment in html, fragment


def test_data_quality_warnings_are_surfaced(snapshot):
    html = build_email_html("AU", snapshot)
    assert "Data-quality warnings" in html
    assert "universe refreshed from a secondary source" in html
    assert "3 companies flagged: extreme_fcf_yield" in html


def test_the_subject_names_the_market_and_the_quarter(snapshot):
    message = build_message("AU", snapshot, EmailConfig.from_env(ENV))
    assert message["Subject"] == "FCF Factor – AU – 2026 Q3"
    assert message["To"] == "analyst@example.com, second@example.com"


def test_selected_csv_is_attached(snapshot):
    message = build_message("AU", snapshot, EmailConfig.from_env(ENV))
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "fcf_factor_AU_2026-09-04_selected.csv"
    assert b"AAA.AX" in attachments[0].get_payload(decode=True)


def test_the_message_carries_a_plain_text_alternative(snapshot):
    message = build_message("AU", snapshot, EmailConfig.from_env(ENV))
    body = message.get_body(preferencelist=("plain",))
    assert body is not None
    assert "AAA.AX" in body.get_content()


def test_a_dry_run_builds_the_message_without_sending(snapshot):
    result = send_quarterly_email("AU", snapshot, env=ENV, dry_run=True)
    assert result["sent"] is False
    assert result["dry_run"] is True
    assert result["subject"].endswith("2026 Q3")


def test_html_is_escaped_so_a_company_name_cannot_inject_markup(data_root):
    write_signal_snapshot(
        "NZ",
        SIGNAL_DATE,
        universe_rows=[],
        raw_financial_rows=[],
        factor_rows=[],
        selected_rows=[
            {"ticker": "X.NZ", "company": "<script>alert(1)</script>", "target_weight": 1.0}
        ],
        excluded_rows=[],
        metadata={"signal_timestamp": "t0", "coverage": {}, "universe": {}},
    )
    html = build_email_html("NZ", SIGNAL_DATE)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------
def test_the_market_report_covers_picks_coverage_and_performance(snapshot):
    report = build_market_report("AU", snapshot)
    for fragment in (
        "# FCF Factor - AU", "Latest picks", "AAA.AX", "Universe and coverage",
        "Changes versus the previous rebalance", "Distributions", "FCF yield",
        "Growth score", "Forward-test performance", "Sector weights",
        "Exclusion reasons", "Data-quality flags raised",
    ):
        assert fragment in report, fragment


def test_the_report_is_written_to_the_expected_path(snapshot):
    path = write_market_report("AU", snapshot)
    assert path.name == "AU.md"
    assert "AAA.AX" in path.read_text()


def test_a_market_with_no_signal_still_produces_a_report(data_root):
    report = build_market_report("NZ")
    assert "No quarterly signal has been generated" in report


def test_the_index_report_lists_every_market(data_root):
    path = write_index_report(["AU", "US", "UK", "NZ", "CA"])
    text = path.read_text()
    for market in ("AU", "US", "UK", "NZ", "CA"):
        assert f"[{market}]({market}.md)" in text


def test_the_report_shows_live_nav_once_the_portfolio_has_executed(snapshot):
    from fcf_factor.portfolio.nav import update_portfolio

    upsert_prices(
        "AU",
        [
            {
                "date": "2026-09-07",
                "ticker": t,
                "market": "AU",
                "currency": "AUD",
                "open": 10.0,
                "close": 11.0,
                "dividend": 0.0,
                "split": 0.0,
            }
            for t in ("AAA.AX", "BBB.AX")
        ],
    )
    update_portfolio("AU")
    report = build_market_report("AU", snapshot)
    assert "Since-inception return" in report
    assert "10.00%" in report
    assert "Current positions (drifted weights)" in report
