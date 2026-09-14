"""End-to-end company evaluation: eligibility, exclusions and cross-section."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from fcf_factor.factor.cross_section import run_market_factor
from fcf_factor.factor.engine import evaluate_company
from fcf_factor.providers.base import CompanyFundamentals

AS_OF = date(2026, 3, 6)


def test_a_healthy_company_is_eligible_and_fully_scored(healthy_company, entry_factory, fx):
    company = evaluate_company(entry_factory("GOOD"), healthy_company("GOOD"), fx, AS_OF)
    assert company.eligible is True, company.exclusion_reasons
    assert company.fcf_yield is not None and company.fcf_yield > 0
    assert company.expected_fcf_usd > 0
    assert company.revenue_trend is not None
    assert company.enterprise_value_source == "calculated"
    assert company.normalized_margin_observations >= 3


def test_missing_metadata_excludes_with_an_explicit_reason(entry_factory, fx):
    bundle = CompanyFundamentals(symbol="GHOST")
    company = evaluate_company(entry_factory("GHOST"), bundle, fx, AS_OF)
    assert company.eligible is False
    assert company.exclusion_reasons == ["symbol_unresolved"]


def test_a_non_equity_quote_type_is_excluded(healthy_company, entry_factory, fx):
    bundle = healthy_company("FUND")
    bundle.metadata = replace(bundle.metadata, quote_type="ETF")
    company = evaluate_company(entry_factory("FUND"), bundle, fx, AS_OF)
    assert company.exclusion_reasons == ["quote_type:ETF"]


def test_financial_sector_companies_are_excluded(healthy_company, entry_factory, fx):
    bundle = healthy_company("BANK", sector="Financial Services")
    company = evaluate_company(entry_factory("BANK"), bundle, fx, AS_OF)
    assert company.exclusion_reasons == ["excluded_sector:Financial Services"]


def test_reits_are_excluded_by_industry(healthy_company, entry_factory, fx):
    bundle = healthy_company("PROP", sector="Real Estate")
    bundle.metadata = replace(bundle.metadata, industry="REIT - Office")
    company = evaluate_company(entry_factory("PROP"), bundle, fx, AS_OF)
    assert company.exclusion_reasons == ["excluded_industry:reit"]


def test_a_company_below_the_market_cap_floor_is_excluded(healthy_company, entry_factory, fx):
    bundle = healthy_company("TINY", price=0.10, shares=10_000_000.0)
    company = evaluate_company(entry_factory("TINY"), bundle, fx, AS_OF)
    assert company.eligible is False
    assert company.exclusion_reasons[0].startswith("below_min_market_cap")


def test_too_little_annual_history_is_excluded(healthy_company, entry_factory, fx):
    bundle = healthy_company("SHORT")
    bundle.annual = bundle.annual[-2:]
    company = evaluate_company(entry_factory("SHORT"), bundle, fx, AS_OF)
    assert company.exclusion_reasons == ["insufficient_annual_history:2"]


def test_three_years_of_history_is_accepted_and_flagged(healthy_company, entry_factory, fx):
    bundle = healthy_company("THREE")
    bundle.annual = bundle.annual[-3:]
    company = evaluate_company(entry_factory("THREE"), bundle, fx, AS_OF)
    assert company.eligible is True, company.exclusion_reasons
    assert "annual_history_below_preferred:3" in company.flags


def test_a_loss_making_company_is_excluded_on_ebitda(healthy_company, entry_factory, fx):
    bundle = healthy_company("LOSS")
    bundle.quarterly = [replace(q, ebitda=-10_000.0) for q in bundle.quarterly]
    bundle.annual = [replace(a, ebitda=-10_000.0) for a in bundle.annual]
    company = evaluate_company(entry_factory("LOSS"), bundle, fx, AS_OF)
    assert company.exclusion_reasons == ["ttm_ebitda_not_positive"]


def test_negative_expected_fcf_is_excluded(healthy_company, entry_factory, fx):
    bundle = healthy_company("CASHBURN", margin=-0.50)
    company = evaluate_company(entry_factory("CASHBURN"), bundle, fx, AS_OF)
    assert company.eligible is False
    assert "expected_fcf_not_positive" in company.exclusion_reasons


def test_a_negative_enterprise_value_is_excluded(healthy_company, entry_factory, fx):
    bundle = healthy_company("NETCASH")
    bundle.annual_balance = [
        replace(bundle.annual_balance[0], total_debt=0.0, cash_and_equivalents=1e12)
    ]
    bundle.quarterly_balance = []
    company = evaluate_company(entry_factory("NETCASH"), bundle, fx, AS_OF)
    assert company.exclusion_reasons == ["enterprise_value_not_positive"]


def test_an_unconvertible_reporting_currency_is_excluded(healthy_company, entry_factory, fx):
    bundle = healthy_company("EXOTIC", currency="XYZ", quote_currency="USD")
    company = evaluate_company(entry_factory("EXOTIC"), bundle, fx, AS_OF)
    assert company.exclusion_reasons == ["fx_rate_unavailable:XYZ"]


def test_a_missing_balance_sheet_falls_back_to_the_provider_ev(healthy_company, entry_factory, fx):
    bundle = healthy_company("NOBS")
    bundle.annual_balance = []
    bundle.quarterly_balance = []
    company = evaluate_company(entry_factory("NOBS"), bundle, fx, AS_OF)
    assert company.eligible is True, company.exclusion_reasons
    assert company.enterprise_value_source == "provider"
    assert "ev_from_provider_fallback" in company.flags


def test_a_company_reporting_in_a_different_currency_converts_correctly(
    healthy_company, entry_factory, fx
):
    """A CAD-listed company reporting in USD must not be converted twice."""
    usd_reporter = healthy_company("DUAL", currency="USD", quote_currency="CAD")
    company = evaluate_company(entry_factory("DUAL", "CA"), usd_reporter, fx, AS_OF)
    assert company.eligible is True, company.exclusion_reasons
    # Market cap is CAD-denominated, expected FCF is USD-denominated.
    assert company.market_cap_usd == pytest.approx(company.market_cap_local * 0.75)
    assert company.expected_fcf_usd == pytest.approx(company.expected_fcf)
    assert any(f.startswith("reporting_currency_differs") for f in company.flags)


def test_fcf_per_share_trend_falls_back_to_basic_shares(healthy_company, entry_factory, fx):
    bundle = healthy_company("BASIC")
    bundle.annual = [replace(a, diluted_shares=None) for a in bundle.annual]
    company = evaluate_company(entry_factory("BASIC"), bundle, fx, AS_OF)
    assert company.eligible is True, company.exclusion_reasons
    assert "fcf_per_share_used_basic_shares" in company.flags
    assert company.fcf_per_share_trend is not None


def test_no_share_counts_omits_the_component_rather_than_inventing_it(
    healthy_company, entry_factory, fx
):
    bundle = healthy_company("NOSHARES")
    bundle.annual = [replace(a, diluted_shares=None, basic_shares=None) for a in bundle.annual]
    company = evaluate_company(entry_factory("NOSHARES"), bundle, fx, AS_OF)
    assert company.fcf_per_share_trend is None
    assert "fcf_per_share_trend_unavailable" in company.flags
    # Revenue plus EBITDA trends still satisfy the requirement.
    assert company.eligible is True, company.exclusion_reasons


def test_revenue_trend_plus_no_secondary_trend_is_excluded(healthy_company, entry_factory, fx):
    bundle = healthy_company("THIN")
    bundle.annual = [
        replace(a, ebitda=None, diluted_shares=None, basic_shares=None) for a in bundle.annual
    ]
    bundle.quarterly = [replace(q, ebitda=None) for q in bundle.quarterly]
    bundle.metadata = replace(bundle.metadata, ttm_ebitda=5_000_000.0)
    company = evaluate_company(entry_factory("THIN"), bundle, fx, AS_OF)
    assert company.exclusion_reasons == ["no_secondary_trend_available"]


def test_cross_section_scores_the_whole_market_before_selection(
    healthy_company, entry_factory, fx
):
    companies = []
    for i in range(40):
        bundle = healthy_company(
            f"C{i:02d}",
            growth=0.02 + i * 0.01,
            margin=0.05 + (i % 7) * 0.02,
            price=5.0 + i,
        )
        companies.append(evaluate_company(entry_factory(f"C{i:02d}"), bundle, fx, AS_OF))

    eligible = [c for c in companies if c.eligible]
    assert len(eligible) >= 30

    result = run_market_factor("US", eligible)
    assert result.selection is not None
    # z-scores exist for every eligible name, not just the selected ones
    assert all(c.growth_score is not None for c in result.eligible)
    assert all(c.fcf_yield_rank is not None for c in result.eligible)
    assert 0 < len(result.selected) <= len(result.shortlist)
    assert sum(c.target_weight for c in result.selected) == pytest.approx(1.0, abs=1e-9)


def test_a_company_row_serialises_every_documented_column(healthy_company, entry_factory, fx):
    from fcf_factor.storage import SELECTED_COLUMNS

    company = evaluate_company(entry_factory("ROW"), healthy_company("ROW"), fx, AS_OF)
    row = company.to_row()
    required = {
        "ticker", "company", "sector", "market", "market_cap_local", "market_cap_usd",
        "enterprise_value_usd", "ttm_fcf", "normalized_fcf_margin", "forward_growth",
        "forward_fcf", "expected_fcf", "fcf_yield", "revenue_trend", "ebitda_trend",
        "fcf_per_share_trend", "z_revenue", "z_ebitda", "z_fcf_per_share", "growth_score",
        "fcf_yield_rank", "growth_rank", "target_weight", "data_quality_flags",
    }
    assert required <= set(row)
    assert required <= set(SELECTED_COLUMNS)


def test_a_uk_company_gets_a_pound_denominated_provider_ev_fallback(
    healthy_company, entry_factory, fx
):
    """The EV fallback must not be divided by 100 for a pence-quoted listing."""
    bundle = healthy_company("LON", currency="GBP", quote_currency="GBp", price=250.0, shares=100_000_000.0)
    # Yahoo reports market cap and EV in pounds while quoting the price in pence.
    bundle.metadata = replace(
        bundle.metadata, market_cap=250_000_000.0, enterprise_value=300_000_000.0
    )
    bundle.annual_balance = []
    bundle.quarterly_balance = []
    company = evaluate_company(entry_factory("LON.L", "UK"), bundle, fx, AS_OF)
    assert company.eligible is True, company.exclusion_reasons
    assert company.enterprise_value_source == "provider"
    # GBP 300m at 1.25 USD/GBP, not GBP 3m.
    assert company.enterprise_value_usd == pytest.approx(375_000_000.0)


def test_an_implausible_provider_ev_is_discarded_rather_than_used(
    healthy_company, entry_factory, fx
):
    bundle = healthy_company("BADEV", currency="GBP", quote_currency="GBp", price=250.0, shares=100_000_000.0)
    bundle.metadata = replace(
        bundle.metadata, market_cap=250_000_000.0, enterprise_value=2_000_000.0
    )
    bundle.annual_balance = []
    bundle.quarterly_balance = []
    company = evaluate_company(entry_factory("BADEV.L", "UK"), bundle, fx, AS_OF)
    assert company.exclusion_reasons == ["enterprise_value_unavailable"]
    assert any(f.startswith("provider_ev_implausible") for f in company.flags)
