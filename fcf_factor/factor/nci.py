"""Non-controlling (minority) interests.

When a listed company consolidates a subsidiary it only partly owns, *all* of
that subsidiary's cash flow lands in the group's operating cash flow, even
though the minority holders own a slice of it.  Left alone, that overstates the
free cash flow available to the listed company's own shareholders.

Treatment (methodology 1.1.0):

* Minority interests are **not** added to enterprise value.
* Instead, expected free cash flow is scaled down to the parent's economic share:
  ``ExpectedFCF_attributable = ExpectedFCF_group * (1 - nci_share)``.

The minorities' share is measured, in order of preference:

1. **Earnings** -- NCI earnings / group net income, summed over the latest
   three fiscal years so one odd year cannot dominate.
2. **Book equity** -- NCI book value / total equity including NCI, used only
   when earnings cannot give a sensible answer (for example group losses).

A company that shows minority interests on its balance sheet but gives us no
way to measure their share is excluded rather than assumed to be zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..providers.base import BalanceSheetSnapshot, IncomeCashflowPeriod

#: Years of earnings summed to measure the minorities' share.
NCI_LOOKBACK_YEARS = 3
#: A share above this is treated as a data problem rather than a real business
#: (a parent keeping less than 5% of its own group's cash flow).
NCI_SHARE_CEILING = 0.95


@dataclass
class NciShare:
    """The minorities' economic share of group cash flow, and how we got it."""

    share: float | None = 0.0
    source: str = "none"  # none | earnings | book_equity | unknown
    flags: list[str] = field(default_factory=list)

    @property
    def parent_share(self) -> float | None:
        return None if self.share is None else 1.0 - self.share


def _clamp(value: float, result: NciShare) -> float:
    if value < 0:
        # Minorities absorbing losses would imply the parent owns over 100%.
        result.flags.append("nci_share_negative_floored")
        return 0.0
    if value > NCI_SHARE_CEILING:
        result.flags.append("nci_share_capped")
        return NCI_SHARE_CEILING
    return value


def nci_share(
    annual: list[IncomeCashflowPeriod],
    balance: BalanceSheetSnapshot | None,
) -> NciShare:
    """Estimate the fraction of group cash flow belonging to minority holders."""
    result = NciShare()
    recent = sorted(annual, key=lambda p: p.period_end)[-NCI_LOOKBACK_YEARS:]

    paired = [
        (p.nci_earnings, p.net_income_incl_nci)
        for p in recent
        if p.nci_earnings is not None and p.net_income_incl_nci is not None
    ]
    nci_total = sum(n for n, _ in paired)
    group_total = sum(g for _, g in paired)
    book_nci = balance.minority_interest if balance is not None else None
    has_book_nci = book_nci is not None and abs(book_nci) > 0
    has_nci_earnings = any(abs(n) > 0 for n, _ in paired)

    if not has_book_nci and not has_nci_earnings:
        return result  # genuinely wholly owned: share stays 0

    if any(p.nci_earnings_from_minority_row for p in recent):
        result.flags.append("nci_earnings_from_minority_row")

    if paired and group_total > 0:
        result.share = _clamp(nci_total / group_total, result)
        result.source = "earnings"
        result.flags.append(f"nci_adjusted:{result.share:.3f}")
        return result

    equity = balance.total_equity_incl_nci if balance is not None else None
    if has_book_nci and equity is not None and equity > 0:
        result.share = _clamp(float(book_nci) / float(equity), result)
        result.source = "book_equity"
        result.flags.append(f"nci_adjusted_from_book:{result.share:.3f}")
        return result

    result.share = None
    result.source = "unknown"
    return result
