"""Provider contract and the Yahoo-specific parsing, without any network use."""

from __future__ import annotations

from datetime import date

import pytest

from fcf_factor.providers import get_provider
from fcf_factor.providers.base import CompanyFundamentals, DataProvider
from fcf_factor.providers.cache import JsonCache
from fcf_factor.providers.yahoo import YahooProvider, _first, _to_date


class FakeFrame:
    """A minimal stand-in for the DataFrames yfinance returns."""

    def __init__(self, data: dict[str, dict]):
        # data: {column_timestamp: {row_label: value}}
        self._data = data
        self.columns = list(data)
        self.empty = not data

    def __getitem__(self, column):
        return FakeSeries(self._data[column])


class FakeSeries:
    def __init__(self, mapping: dict):
        self._mapping = mapping

    def items(self):
        return self._mapping.items()


def test_the_interface_declares_every_documented_method():
    for name in (
        "get_company_metadata",
        "get_annual_financials",
        "get_quarterly_financials",
        "get_balance_sheet",
        "get_prices",
        "get_fx_rate",
    ):
        assert hasattr(DataProvider, name)


def test_the_factory_returns_the_requested_provider():
    assert get_provider("synthetic").name == "synthetic"
    assert get_provider("yahoo").name == "yahoo"
    with pytest.raises(ValueError, match="unknown data provider"):
        get_provider("bloomberg")


def test_the_factor_engine_never_imports_yfinance():
    """The whole point of the provider layer: the factor must stay decoupled."""
    import subprocess
    import sys

    code = (
        "import sys; import fcf_factor.factor as f; "
        "import fcf_factor.pipeline;"
        "print('yfinance' in sys.modules)"
    )
    output = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert output.stdout.strip() == "False"


def test_statement_frames_are_flattened_with_dates_and_finite_values():
    frame = FakeFrame(
        {
            "2025-12-31": {"Total Revenue": 100.0, "EBITDA": 20.0, "Junk": float("nan")},
            "2024-12-31": {"Total Revenue": 90.0},
        }
    )
    records = YahooProvider._frame_to_periods(frame, "annual")
    assert [r["period_end"] for r in records] == ["2024-12-31", "2025-12-31"]
    assert records[1]["values"] == {"Total Revenue": 100.0, "EBITDA": 20.0}


def test_an_empty_frame_yields_no_periods():
    assert YahooProvider._frame_to_periods(None, "annual") == []
    assert YahooProvider._frame_to_periods(FakeFrame({}), "annual") == []


def test_row_label_aliases_are_tried_in_order():
    assert _first({"Operating Revenue": 5.0}, ("Total Revenue", "Operating Revenue")) == 5.0
    assert _first({"Total Revenue": 7.0, "Operating Revenue": 5.0}, ("Total Revenue", "Operating Revenue")) == 7.0
    assert _first({"Total Revenue": None}, ("Total Revenue",)) is None
    assert _first({}, ("Total Revenue",)) is None


def test_date_parsing_accepts_the_shapes_yfinance_produces():
    assert _to_date("2025-12-31 00:00:00") == date(2025, 12, 31)
    assert _to_date(date(2025, 12, 31)) == date(2025, 12, 31)
    assert _to_date("not a date") is None


def test_fx_for_the_same_currency_needs_no_lookup():
    provider = YahooProvider()
    assert provider.get_fx_rate("USD", "USD") == 1.0
    # Pence to pounds is a pure unit conversion, not a market rate.
    assert provider.get_fx_rate("GBp", "GBP") == pytest.approx(0.01)


def test_the_balance_sheet_period_argument_is_validated():
    with pytest.raises(ValueError, match="annual"):
        YahooProvider().get_balance_sheet("AAPL", period="monthly")


def test_the_cache_round_trips_and_honours_its_ttl(tmp_path):
    cache = JsonCache("test", ttl_hours=1.0, root=tmp_path)
    assert cache.get("missing") is None
    cache.set("key", {"a": 1})
    assert cache.get("key") == {"a": 1}

    expired = JsonCache("test", ttl_hours=-1.0, root=tmp_path)
    expired.ttl_seconds = 0.000001
    assert expired.get("key") is None


def test_the_cache_can_be_disabled_by_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("FCF_DISABLE_CACHE", "1")
    cache = JsonCache("test", root=tmp_path)
    cache.set("key", {"a": 1})
    assert cache.get("key") is None


def test_get_fundamentals_tolerates_a_partial_failure():
    class Flaky(DataProvider):
        name = "flaky"

        def get_company_metadata(self, symbol):
            return None

        def get_annual_financials(self, symbol):
            raise RuntimeError("upstream exploded")

        def get_quarterly_financials(self, symbol):
            return []

        def get_balance_sheet(self, symbol, period="annual"):
            return []

        def get_prices(self, symbol, start, end):
            return []

        def get_fx_rate(self, base, quote="USD"):
            return None

    bundle = Flaky().get_fundamentals("X")
    assert isinstance(bundle, CompanyFundamentals)
    assert any("upstream exploded" in err for err in bundle.fetch_errors)


def test_the_synthetic_provider_is_deterministic():
    a = get_provider("synthetic").get_company_metadata("ABC.NZ")
    b = get_provider("synthetic").get_company_metadata("ABC.NZ")
    assert a.market_cap == b.market_cap
    assert a.sector == b.sector


def test_annual_periods_are_returned_oldest_first():
    bundle = CompanyFundamentals(
        symbol="X", annual=get_provider("synthetic").get_annual_financials("ABC.NZ")
    )
    ends = [p.period_end for p in bundle.annual_sorted]
    assert ends == sorted(ends)


def test_total_equity_is_never_mistaken_for_minority_interest():
    """``Total Equity Gross Minority Interest`` is total equity, not the NCI.

    Treating it as a fallback adds the whole equity base to enterprise value,
    which understates the FCF yield of every company without a separate NCI
    line. This test pins the distinction.
    """
    from fcf_factor.providers.yahoo import MINORITY_LABELS, _first

    assert "Total Equity Gross Minority Interest" not in MINORITY_LABELS
    equity_only = {"Total Equity Gross Minority Interest": 1_200_000_000.0}
    assert _first(equity_only, MINORITY_LABELS) is None
    real_nci = {"Minority Interest": 6_500_000.0, "Total Equity Gross Minority Interest": 1.2e9}
    assert _first(real_nci, MINORITY_LABELS) == 6_500_000.0
