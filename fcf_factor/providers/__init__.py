"""Data providers.  The factor engine depends on :mod:`base` only."""

from __future__ import annotations

from .base import (
    BalanceSheetSnapshot,
    CompanyFundamentals,
    CompanyMetadata,
    DataProvider,
    IncomeCashflowPeriod,
    PriceBar,
)

__all__ = [
    "BalanceSheetSnapshot",
    "CompanyFundamentals",
    "CompanyMetadata",
    "DataProvider",
    "IncomeCashflowPeriod",
    "PriceBar",
    "get_provider",
]


def get_provider(name: str = "yahoo", **kwargs) -> DataProvider:
    """Factory used by the CLI so a provider can be swapped from the command line."""
    key = (name or "yahoo").strip().lower()
    if key == "yahoo":
        from .yahoo import YahooProvider

        return YahooProvider(**kwargs)
    if key == "synthetic":
        from .synthetic import SyntheticProvider

        return SyntheticProvider(**kwargs)
    raise ValueError(f"unknown data provider {name!r} (known: yahoo, synthetic)")
