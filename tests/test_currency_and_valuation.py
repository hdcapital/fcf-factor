"""Currency normalisation, USD conversion, enterprise value and FCF yield."""

from __future__ import annotations

import pytest

from fcf_factor.currency import (
    FxConverter,
    canonical_currency,
    minor_unit_divisor,
    normalise_quote,
)
from fcf_factor.factor.valuation import calculate_enterprise_value, fcf_yield, weight_fcf_yield


# --------------------------------------------------------------------------
# GBp / GBP
# --------------------------------------------------------------------------
def test_pence_quotes_are_converted_to_pounds():
    result = normalise_quote("GBp", 250.0)
    assert result.currency == "GBP"
    assert result.price == pytest.approx(2.50)
    assert result.was_minor_units is True


def test_gbx_is_treated_the_same_as_gbp_pence():
    assert normalise_quote("GBX", 250.0).price == pytest.approx(2.50)
    assert canonical_currency("GBX") == "GBP"


def test_pounds_are_left_alone():
    result = normalise_quote("GBP", 2.50)
    assert result.price == pytest.approx(2.50)
    assert result.was_minor_units is False


def test_minor_unit_divisor_is_case_sensitive_where_it_matters():
    assert minor_unit_divisor("GBp") == 100.0
    assert minor_unit_divisor("GBP") == 1.0


def test_canonical_currency_handles_blanks():
    assert canonical_currency(None) is None
    assert canonical_currency("   ") is None
    assert canonical_currency("aud") == "AUD"


# --------------------------------------------------------------------------
# FX conversion
# --------------------------------------------------------------------------
def test_conversion_to_usd(fx):
    assert fx.to_usd(100.0, "AUD") == pytest.approx(65.0)
    assert fx.to_usd(100.0, "USD") == pytest.approx(100.0)


def test_pence_amounts_convert_at_one_hundredth_of_the_pound_rate(fx):
    assert fx.to_usd(100.0, "GBp") == pytest.approx(1.25)
    assert fx.to_usd(1.0, "GBP") == pytest.approx(1.25)


def test_conversion_returns_none_rather_than_guessing(fx):
    assert fx.to_usd(100.0, "XYZ") is None
    assert fx.to_usd(None, "AUD") is None
    assert fx.has("XYZ") is False


def test_unknown_or_non_positive_rates_are_rejected_at_construction():
    converter = FxConverter({"AUD": 0.0, "NZD": -1.0, "CAD": 0.75})
    assert converter.has("AUD") is False
    assert converter.has("NZD") is False
    assert converter.rate("CAD") == pytest.approx(0.75)


# --------------------------------------------------------------------------
# Enterprise value
# --------------------------------------------------------------------------
def test_enterprise_value_preferred_calculation():
    ev = calculate_enterprise_value(
        market_cap_usd=1_000.0,
        total_debt_usd=300.0,
        cash_usd=100.0,
        preferred_usd=50.0,
    )
    assert ev.value == pytest.approx(1_250.0)
    assert ev.source == "calculated"


def test_enterprise_value_never_includes_minority_interests():
    """Minorities are removed from cash flow instead, so EV must not carry them."""
    import inspect

    assert "minority" not in " ".join(inspect.signature(calculate_enterprise_value).parameters)


def test_absent_preferred_is_treated_as_zero_and_flagged():
    ev = calculate_enterprise_value(1_000.0, 300.0, 100.0)
    assert ev.value == pytest.approx(1_200.0)
    assert "preferred_stock_assumed_zero" in ev.flags


# --------------------------------------------------------------------------
# Leases as debt
# --------------------------------------------------------------------------
from fcf_factor.factor.valuation import resolve_debt  # noqa: E402


def test_leases_already_inside_total_debt_are_not_added_twice():
    """Accent Group's shape: tiny borrowings, large leases, total = both."""
    r = resolve_debt(total_debt=442.0, borrowings=12.0, lease_liabilities=430.0)
    assert r.debt == pytest.approx(442.0)
    assert r.treatment == "leases_included_verified"
    assert r.flags == []


def test_leases_missing_from_total_debt_are_added():
    r = resolve_debt(total_debt=12.0, borrowings=12.0, lease_liabilities=430.0)
    assert r.debt == pytest.approx(442.0)
    assert r.treatment == "leases_added"
    assert "lease_liabilities_added_to_debt" in r.flags


def test_total_debt_smaller_than_leases_cannot_contain_them():
    r = resolve_debt(total_debt=50.0, borrowings=None, lease_liabilities=430.0)
    assert r.debt == pytest.approx(480.0)
    assert r.treatment == "leases_added"


def test_ambiguous_inclusion_is_assumed_and_flagged():
    r = resolve_debt(total_debt=500.0, borrowings=None, lease_liabilities=430.0)
    assert r.debt == pytest.approx(500.0)
    assert r.treatment == "leases_included_assumed"
    assert "lease_inclusion_assumed" in r.flags


def test_no_lease_data_keeps_total_debt_and_says_so():
    r = resolve_debt(total_debt=300.0, borrowings=300.0, lease_liabilities=None)
    assert r.debt == pytest.approx(300.0)
    assert "lease_liabilities_not_reported" in r.flags


def test_zero_leases_needs_no_adjustment():
    r = resolve_debt(total_debt=300.0, borrowings=300.0, lease_liabilities=0.0)
    assert r.debt == pytest.approx(300.0)
    assert r.treatment == "no_leases"


def test_total_debt_is_rebuilt_when_missing():
    r = resolve_debt(total_debt=None, borrowings=100.0, lease_liabilities=250.0)
    assert r.debt == pytest.approx(350.0)
    assert "total_debt_rebuilt" in r.flags


def test_no_debt_information_at_all_is_unavailable_not_zero():
    r = resolve_debt(total_debt=None, borrowings=None, lease_liabilities=None)
    assert r.debt is None


def test_missing_debt_does_not_become_zero_debt():
    """A missing balance-sheet field must not flatter the valuation."""
    ev = calculate_enterprise_value(
        market_cap_usd=1_000.0,
        total_debt_usd=None,
        cash_usd=100.0,
        provider_ev_usd=1_400.0,
        debt_field_available=False,
    )
    assert ev.calculated is None
    assert ev.value == pytest.approx(1_400.0)
    assert ev.source == "provider"
    assert "total_debt_unavailable" in ev.flags
    assert "ev_from_provider_fallback" in ev.flags


def test_enterprise_value_is_none_when_nothing_is_available():
    ev = calculate_enterprise_value(1_000.0, None, None, debt_field_available=False, cash_field_available=False)
    assert ev.value is None


def test_both_ev_figures_are_retained_for_audit():
    ev = calculate_enterprise_value(1_000.0, 300.0, 100.0, provider_ev_usd=1_111.0)
    assert ev.calculated == pytest.approx(1_200.0)
    assert ev.provider == pytest.approx(1_111.0)
    assert ev.source == "calculated"


# --------------------------------------------------------------------------
# FCF yield
# --------------------------------------------------------------------------
def test_fcf_yield():
    assert fcf_yield(120.0, 1_200.0) == pytest.approx(0.10)


def test_fcf_yield_is_undefined_for_non_positive_enterprise_value():
    assert fcf_yield(120.0, 0.0) is None
    assert fcf_yield(120.0, -500.0) is None
    assert fcf_yield(None, 1_200.0) is None


def test_weighting_yield_is_capped_but_the_raw_yield_is_not():
    raw = fcf_yield(600.0, 1_000.0)
    assert raw == pytest.approx(0.60)
    assert weight_fcf_yield(raw) == pytest.approx(0.15)
    assert weight_fcf_yield(0.05) == pytest.approx(0.05)
