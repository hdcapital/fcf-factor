"""Minority interests: measured from earnings, deducted from cash flow."""

from __future__ import annotations

from datetime import date

import pytest

from fcf_factor.factor.nci import NCI_SHARE_CEILING, nci_share
from fcf_factor.providers.base import BalanceSheetSnapshot, IncomeCashflowPeriod


def year(y: int, group_ni: float | None, nci: float | None) -> IncomeCashflowPeriod:
    return IncomeCashflowPeriod(
        period_end=date(y, 6, 30),
        period_type="annual",
        net_income_incl_nci=group_ni,
        nci_earnings=nci,
    )


def bs(minority=None, equity=None) -> BalanceSheetSnapshot:
    return BalanceSheetSnapshot(
        period_end=date(2025, 6, 30),
        period_type="annual",
        minority_interest=minority,
        total_equity_incl_nci=equity,
    )


def test_a_wholly_owned_group_keeps_all_its_cash_flow():
    r = nci_share([year(2023, 10.0, 0.0), year(2024, 12.0, 0.0)], bs(minority=None))
    assert r.share == 0.0
    assert r.parent_share == 1.0
    assert r.source == "none"


def test_minorities_share_is_measured_from_earnings():
    """A Joyce-style group: minorities take a fifth of the profit."""
    annual = [year(2023, 10.0, 2.0), year(2024, 12.0, 2.4), year(2025, 15.0, 3.0)]
    r = nci_share(annual, bs(minority=6.5, equity=60.0))
    assert r.share == pytest.approx(7.4 / 37.0)
    assert r.parent_share == pytest.approx(0.8)
    assert r.source == "earnings"


def test_only_the_latest_three_years_are_used():
    annual = [year(2021, 10.0, 9.0), year(2023, 10.0, 1.0), year(2024, 10.0, 1.0), year(2025, 10.0, 1.0)]
    assert nci_share(annual, None).share == pytest.approx(0.1)


def test_book_equity_is_the_fallback_when_the_group_made_losses():
    annual = [year(2024, -5.0, 1.0), year(2025, -3.0, 0.5)]
    r = nci_share(annual, bs(minority=25.0, equity=100.0))
    assert r.share == pytest.approx(0.25)
    assert r.source == "book_equity"


def test_book_equity_is_used_when_no_earnings_split_is_reported():
    r = nci_share([year(2025, 10.0, None)], bs(minority=30.0, equity=120.0))
    assert r.share == pytest.approx(0.25)
    assert r.source == "book_equity"


def test_minorities_with_no_way_to_measure_them_are_not_assumed_away():
    r = nci_share([year(2025, None, None)], bs(minority=30.0, equity=None))
    assert r.share is None
    assert r.parent_share is None
    assert r.source == "unknown"


def test_minorities_absorbing_losses_never_credit_the_parent_with_over_100pct():
    r = nci_share([year(2025, 10.0, -2.0)], bs(minority=5.0, equity=50.0))
    assert r.share == 0.0
    assert "nci_share_negative_floored" in r.flags


def test_an_implausible_share_is_capped_and_flagged():
    r = nci_share([year(2025, 10.0, 9.99)], bs(minority=5.0, equity=50.0))
    assert r.share == pytest.approx(NCI_SHARE_CEILING)
    assert "nci_share_capped" in r.flags
