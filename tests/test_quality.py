"""Market-cap reconciliation and the other data-quality safeguards."""

from __future__ import annotations

from datetime import date

import pytest

from fcf_factor.factor.quality import (
    IMPLAUSIBLE_MARKET_CAP_USD,
    assess_market_cap,
    currency_mismatch_flag,
    empty_payload_flag,
    extreme_yield_flag,
    statement_freshness_flag,
)
from fcf_factor.providers.base import CompanyMetadata


def meta(**kwargs) -> CompanyMetadata:
    base = {
        "symbol": "TEST.L",
        "quote_currency": "GBp",
        "price": 250.0,
        "shares_outstanding": 100_000_000.0,
        "market_cap": 250_000_000.0,
    }
    base.update(kwargs)
    return CompanyMetadata(**base)


def test_london_pence_quote_reconciles_against_a_pound_market_cap(fx):
    """250p x 100m shares = GBP 250m, which is what Yahoo reports."""
    result = assess_market_cap(meta(), fx)
    assert result.exclusion is None
    assert result.currency == "GBP"
    assert result.price_major == pytest.approx(2.50)
    assert result.market_cap_local == pytest.approx(250_000_000.0)
    assert result.market_cap_usd == pytest.approx(312_500_000.0)
    assert "price_quoted_in_minor_units" in result.flags


def test_a_hundred_times_market_cap_error_is_detected_and_corrected(fx):
    """A cap reported in pence is 100x the pounds figure; use the pounds one."""
    result = assess_market_cap(meta(market_cap=25_000_000_000.0), fx)
    assert result.exclusion is None
    assert result.ratio == pytest.approx(100.0)
    assert result.market_cap_local == pytest.approx(250_000_000.0)
    assert "market_cap_minor_units_adjusted" in result.flags


def test_a_market_cap_one_hundredth_of_price_times_shares_is_corrected(fx):
    result = assess_market_cap(meta(market_cap=2_500_000.0), fx)
    assert result.market_cap_local == pytest.approx(250_000_000.0)
    assert "market_cap_unit_mismatch_100x" in result.flags


def test_an_unexplainable_discrepancy_excludes_the_company(fx):
    result = assess_market_cap(meta(market_cap=5_000_000_000.0), fx)
    assert result.exclusion is not None
    assert result.exclusion.startswith("market_cap_inconsistent")


def test_a_modest_discrepancy_is_flagged_but_tolerated(fx):
    # Stale share counts routinely produce a 2-5x gap; prefer the reported cap.
    result = assess_market_cap(meta(market_cap=750_000_000.0), fx)
    assert result.exclusion is None
    assert result.market_cap_local == pytest.approx(750_000_000.0)
    assert any(f.startswith("market_cap_discrepancy") for f in result.flags)


def test_market_cap_without_a_share_count_is_used_but_flagged(fx):
    result = assess_market_cap(meta(shares_outstanding=None), fx)
    assert result.market_cap_local == pytest.approx(250_000_000.0)
    assert "market_cap_unverified_no_share_count" in result.flags


def test_an_implausible_share_count_is_ignored_not_trusted(fx):
    result = assess_market_cap(meta(shares_outstanding=12.0), fx)
    assert "share_count_implausible" in result.flags
    assert result.implied_market_cap is None


def test_price_times_shares_is_used_when_no_market_cap_is_reported(fx):
    result = assess_market_cap(meta(market_cap=None), fx)
    assert result.market_cap_local == pytest.approx(250_000_000.0)
    assert "market_cap_from_price_times_shares" in result.flags


def test_no_market_cap_at_all_is_an_exclusion(fx):
    result = assess_market_cap(meta(market_cap=None, shares_outstanding=None), fx)
    assert result.exclusion == "market_cap_unavailable"


def test_missing_quote_currency_is_an_exclusion(fx):
    result = assess_market_cap(meta(quote_currency=None), fx)
    assert result.exclusion == "quote_currency_missing"


def test_unconvertible_currency_is_an_exclusion(fx):
    result = assess_market_cap(meta(quote_currency="XYZ", price=2.5, market_cap=250_000_000.0), fx)
    assert result.exclusion == "fx_rate_unavailable:XYZ"


def test_an_impossible_market_cap_is_rejected(fx):
    huge = IMPLAUSIBLE_MARKET_CAP_USD * 10
    result = assess_market_cap(
        meta(quote_currency="USD", price=1.0, shares_outstanding=huge, market_cap=huge), fx
    )
    assert result.exclusion.startswith("market_cap_implausible")


def test_negative_market_cap_is_rejected(fx):
    result = assess_market_cap(
        meta(quote_currency="USD", price=-1.0, shares_outstanding=None, market_cap=-5.0), fx
    )
    assert result.exclusion == "market_cap_non_positive"


def test_stale_statements_are_flagged():
    assert statement_freshness_flag(date(2023, 1, 1), date(2026, 9, 4)).startswith("stale_statements")
    assert statement_freshness_flag(date(2025, 12, 31), date(2026, 9, 4)) is None
    assert statement_freshness_flag(None, date(2026, 9, 4)) == "statement_date_unknown"


def test_reporting_currency_mismatch_is_flagged():
    assert currency_mismatch_flag("CAD", "USD") == "reporting_currency_differs:USD_vs_CAD"
    assert currency_mismatch_flag("GBp", "GBP") is None
    assert currency_mismatch_flag("AUD", None) is None


def test_extreme_yields_are_flagged_but_not_altered():
    flag = extreme_yield_flag(0.85)
    assert flag is not None and flag.startswith("extreme_fcf_yield")
    assert extreme_yield_flag(0.12) is None


def test_an_empty_statement_payload_is_flagged():
    assert empty_payload_flag(True, 0, 0) == "provider_returned_no_statements"
    assert empty_payload_flag(True, 4, 4) is None


def test_a_provider_enterprise_value_in_the_wrong_units_is_rejected():
    from fcf_factor.factor.quality import provider_ev_sanity_flag

    # A London EV mistakenly divided by 100 would be ~1% of market cap.
    flag = provider_ev_sanity_flag(3_000_000.0, 312_500_000.0)
    assert flag is not None and flag.startswith("provider_ev_implausible")
    # And one multiplied by 100 would be ~100x market cap.
    assert provider_ev_sanity_flag(31_250_000_000.0, 312_500_000.0) is not None


def test_a_plausible_provider_enterprise_value_passes():
    from fcf_factor.factor.quality import provider_ev_sanity_flag

    assert provider_ev_sanity_flag(400_000_000.0, 312_500_000.0) is None
    assert provider_ev_sanity_flag(None, 312_500_000.0) is None
    assert provider_ev_sanity_flag(400_000_000.0, None) is None
