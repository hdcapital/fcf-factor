"""Universe parsing, security-type filtering and symbol mapping.

The parsers are exercised against captured fixture files, so a change in an
exchange's format shows up as a test failure rather than as a silently empty
universe in production.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fcf_factor.universe.au import parse_asx_directory
from fcf_factor.universe.base import UniverseEntry, UniverseResult, dedupe_entries
from fcf_factor.universe.ca import parse_tmx_payload
from fcf_factor.universe.filters import (
    classify_security,
    quote_type_exclusion_reason,
    sector_exclusion_reason,
    us_symbol_exclusion,
)
from fcf_factor.universe.nz import parse_nzx_page
from fcf_factor.universe.registry import UniverseIntegrityError, check_universe_integrity
from fcf_factor.universe.symbols import is_plausible_yahoo_symbol, to_yahoo_symbol
from fcf_factor.universe.uk import parse_lse_rows
from fcf_factor.universe.us import parse_nasdaq_listed, parse_other_listed

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Symbol mapping
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("symbol", "market", "exchange", "expected"),
    [
        ("BHP", "AU", "ASX", "BHP.AX"),
        ("AIR", "NZ", "NZX", "AIR.NZ"),
        ("VOD", "UK", "LSE", "VOD.L"),
        ("BT.A", "UK", "LSE", "BT-A.L"),
        ("SHOP", "CA", "TSX", "SHOP.TO"),
        ("ABC", "CA", "TSXV", "ABC.V"),
        ("CCL.B", "CA", "TSX", "CCL-B.TO"),
        ("AAPL", "US", "NASDAQ", "AAPL"),
        ("BRK.A", "US", "NYSE", "BRK-A"),
        ("cat", "US", "NYSE", "CAT"),
    ],
)
def test_symbol_mapping(symbol, market, exchange, expected):
    assert to_yahoo_symbol(symbol, market, exchange) == expected


def test_unmappable_symbols_return_none_rather_than_a_guess():
    assert to_yahoo_symbol("", "AU", "ASX") is None
    assert to_yahoo_symbol("...", "AU", "ASX") is None
    assert to_yahoo_symbol("ABC", "AU", "NOT_A_VENUE") is None


def test_exchange_and_yahoo_symbols_stay_separate():
    entry = UniverseEntry(
        market="CA", exchange="TSX", exchange_symbol="CCL.B", yahoo_symbol="CCL-B.TO"
    )
    assert entry.exchange_symbol != entry.yahoo_symbol
    assert entry.to_row()["exchange_symbol"] == "CCL.B"


def test_symbol_plausibility_check():
    assert is_plausible_yahoo_symbol("BHP.AX")
    assert not is_plausible_yahoo_symbol("bad symbol!")


# --------------------------------------------------------------------------
# Security-type filters
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("iShares Core MSCI World ETF", "etf"),
        ("Vanguard Australian Shares Index ETF", "etf"),
        ("Scottish Mortgage Investment Trust plc", "investment_company"),
        ("Example plc 6% Cum Pref Shares", "preferred"),
        ("Some Holdings Warrant", "warrant"),
        ("Example Property Trust", "property_trust"),
        ("Simon Property Group REIT", "reit"),
        ("Acme Acquisition Corp", "spac"),
        ("Capital Pool Company Ltd", "shell"),
        ("Example 5.5% Notes", "debt"),
        ("BHP Group Limited", None),
        ("Fisher & Paykel Healthcare Corporation Limited", None),
    ],
)
def test_security_name_classification(name, expected):
    assert classify_security(name) == expected


def test_us_symbol_shapes_that_are_not_common_stock():
    assert us_symbol_exclusion("ABC$A") == "preferred"
    assert us_symbol_exclusion("ABC.W") == "warrant"
    assert us_symbol_exclusion("ABC.U") == "unit"
    assert us_symbol_exclusion("ABC.R") == "right"
    assert us_symbol_exclusion("BRK.A") is None
    assert us_symbol_exclusion("AAPL") is None


def test_sector_and_quote_type_filters():
    assert sector_exclusion_reason("Financial Services", "Banks") == "excluded_sector:Financial Services"
    assert sector_exclusion_reason("Real Estate", "REIT - Office") == "excluded_industry:reit"
    assert sector_exclusion_reason("Technology", "Software") is None
    assert quote_type_exclusion_reason("ETF") == "quote_type:ETF"
    assert quote_type_exclusion_reason("EQUITY") is None
    assert quote_type_exclusion_reason(None) == "quote_type_missing"


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------
def test_nasdaq_listed_parser_keeps_only_common_stock():
    entries, excluded = parse_nasdaq_listed(_read("nasdaqlisted.txt"))
    symbols = {e.yahoo_symbol for e in entries}
    assert symbols == {"AAPL", "BRKS"}
    reasons = {e["exchange_symbol"]: e["reason"] for e in excluded}
    assert reasons["QQQ"] == "etf"
    assert reasons["ZTEST"] == "test_issue"
    assert reasons["ABCDW"] == "warrant"
    assert reasons["SPCEU"] == "unit"
    assert reasons["ACQR"] == "spac"
    assert all(e.exchange == "NASDAQ" for e in entries)


def test_other_listed_parser_keeps_nyse_and_nyse_american_only():
    entries, excluded = parse_other_listed(_read("otherlisted.txt"))
    symbols = {e.yahoo_symbol for e in entries}
    assert symbols == {"BRK-A", "BRK-B", "CAT", "AAU"}
    exchanges = {e.yahoo_symbol: e.exchange for e in entries}
    assert exchanges["AAU"] == "NYSE AMERICAN"
    assert exchanges["CAT"] == "NYSE"
    reasons = {e["exchange_symbol"]: e["reason"] for e in excluded}
    assert reasons["SPY"].startswith("exchange_not_targeted")
    assert reasons["IWM"].startswith("exchange_not_targeted")
    assert reasons["XYZ.W"] == "warrant"
    assert reasons["ABC$A"] == "preferred"


def test_asx_parser_skips_the_preamble_and_non_ordinary_codes():
    entries, excluded = parse_asx_directory(_read("asx_directory.csv"))
    symbols = {e.yahoo_symbol for e in entries}
    assert "BHP.AX" in symbols
    assert "WTC.AX" in symbols
    assert "GMG.AX" in symbols  # sector filtering happens later, from metadata
    reasons = {e["exchange_symbol"]: e["reason"] for e in excluded}
    assert reasons["VAS"] == "etf"
    assert reasons["ABCOA"] == "non_ordinary_code"
    assert entries[0].industry is not None


def test_nzx_parser_extracts_ordinary_codes_only():
    entries, excluded = parse_nzx_page(_read("nzx_main_board.html"))
    symbols = {e.yahoo_symbol for e in entries}
    assert {"AIR.NZ", "FPH.NZ", "MEL.NZ", "SML.NZ"} <= symbols
    assert "ARG010.NZ" not in symbols
    reasons = {e["exchange_symbol"]: e["reason"] for e in excluded}
    assert reasons["ARG010"] == "non_ordinary_code"
    assert reasons["KFL"] == "fund"


def test_tmx_parser_maps_classes_and_drops_preferreds():
    payload = _read("tmx_tsx.json")
    entries, excluded = parse_tmx_payload(payload, "TSX")
    symbols = {e.yahoo_symbol for e in entries}
    assert {"SHOP.TO", "CCL-A.TO", "CCL-B.TO", "BCE.TO"} <= symbols
    reasons = {e["exchange_symbol"]: e["reason"] for e in excluded}
    assert reasons["BCE.PR.A"] == "preferred"
    assert reasons["XIU"] == "etf"
    assert json.loads(payload)["length"] == 4


def test_lse_parser_keeps_main_market_and_aim_only():
    rows = [
        ["London Stock Exchange issuer list", "", "", ""],
        ["Issuer Name", "TIDM", "Market", "ICB Industry"],
        ["Vodafone Group Plc", "VOD", "Main Market", "Telecommunications"],
        ["Boohoo Group Plc", "BOO", "AIM", "Consumer Discretionary"],
        ["Some Bond Issuer Plc", "XS01", "Professional Securities Market", "Financials"],
        ["Scottish Mortgage Investment Trust plc", "SMT", "Main Market", "Financials"],
        ["BT Group Plc", "BT.A", "Main Market", "Telecommunications"],
    ]
    entries, excluded = parse_lse_rows(rows)
    symbols = {e.yahoo_symbol for e in entries}
    assert symbols == {"VOD.L", "BOO.L", "BT-A.L"}
    reasons = {e["exchange_symbol"]: e["reason"] for e in excluded}
    assert reasons["XS01"].startswith("market_not_targeted")
    assert reasons["SMT"] == "investment_company"


# --------------------------------------------------------------------------
# Integrity
# --------------------------------------------------------------------------
def test_duplicate_symbols_are_removed_and_recorded():
    result = UniverseResult(market="CA")
    entries = [
        UniverseEntry("CA", "TSX", "ABC", "ABC.TO", "Example"),
        UniverseEntry("CA", "TSXV", "ABC", "ABC.TO", "Example"),
    ]
    deduped = dedupe_entries(entries, result)
    assert len(deduped) == 1
    assert result.excluded[0]["reason"].startswith("duplicate_symbol")


def test_a_severely_shrunken_universe_is_reported():
    result = UniverseResult(market="US", entries=[UniverseEntry("US", "NYSE", f"T{i}", f"T{i}") for i in range(2000)])
    problems = check_universe_integrity(result, previous_size=5000)
    assert any("shrank" in p for p in problems)


def test_a_universe_below_the_configured_floor_is_reported():
    result = UniverseResult(market="NZ", entries=[UniverseEntry("NZ", "NZX", "A", "A.NZ")])
    problems = check_universe_integrity(result, previous_size=None)
    assert any("below the configured floor" in p for p in problems)


def test_a_healthy_universe_raises_nothing():
    entries = [UniverseEntry("NZ", "NZX", f"T{i}", f"T{i}.NZ") for i in range(120)]
    assert check_universe_integrity(UniverseResult(market="NZ", entries=entries), 130) == []


def test_integrity_error_is_available_for_callers():
    assert issubclass(UniverseIntegrityError, RuntimeError)
