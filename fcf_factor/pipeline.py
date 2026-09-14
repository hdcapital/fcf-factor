"""Quarterly screening orchestration.

One function builds a market's factor result (:func:`run_screen`) and another
freezes it to disk (:func:`persist_screen`).  Keeping them apart is what makes
``--dry-run`` meaningful: a dry run does exactly the same work and simply never
writes, so what you inspect locally is what CI would have committed.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

from .config import (
    CONFIG,
    MARKET_CURRENCY,
    METHODOLOGY_VERSION,
    PROVIDER_MAX_WORKERS,
    FactorConfig,
)
from .factor.cross_section import MarketFactorResult, run_market_factor
from .factor.engine import CompanyFactors, evaluate_company
from .factor.quality import CoverageSummary
from .fx import build_fx_converter, collect_currencies
from .logging_utils import get_logger
from .providers.base import CompanyFundamentals, DataProvider
from .storage import SELECTED_COLUMNS, utc_now_iso, write_signal_snapshot
from .universe.base import UniverseEntry, UniverseResult
from .universe.registry import build_universe

log = get_logger(__name__)


class SyntheticDataRefused(RuntimeError):
    """Raised on any attempt to persist a snapshot built from invented data."""


@dataclass
class ScreenResult:
    """Everything one market's screening run produced."""

    market: str
    signal_date: date
    signal_timestamp: str
    provider: str
    universe: UniverseResult
    factors: list[CompanyFactors] = field(default_factory=list)
    factor_result: MarketFactorResult | None = None
    coverage: CoverageSummary = field(default_factory=CoverageSummary)
    fx_rates: dict[str, float] = field(default_factory=dict)
    raw_financials: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: The configuration actually used, so metadata reflects overrides.
    config: FactorConfig = CONFIG
    config_hash: str = ""
    snapshot_path: str | None = None

    @property
    def selected(self) -> list[CompanyFactors]:
        return self.factor_result.selected if self.factor_result else []

    @property
    def shortlist(self) -> list[CompanyFactors]:
        return self.factor_result.shortlist if self.factor_result else []

    @property
    def eligible(self) -> list[CompanyFactors]:
        return self.factor_result.eligible if self.factor_result else []

    def selected_rows(self) -> list[dict]:
        rows = []
        for company in self.selected:
            row = company.to_row()
            rows.append({key: row.get(key) for key in SELECTED_COLUMNS})
        return rows

    def excluded_rows(self) -> list[dict]:
        rows = []
        for company in self.factors:
            if company.eligible:
                continue
            rows.append(
                {
                    "yahoo_symbol": company.ticker,
                    "exchange_symbol": company.exchange_symbol,
                    "name": company.company or "",
                    "stage": _exclusion_stage(company.exclusion_reasons),
                    "reasons": "|".join(company.exclusion_reasons),
                }
            )
        rows.sort(key=lambda r: r["yahoo_symbol"])
        return rows

    def metadata(self) -> dict:
        weighting = self.factor_result.weighting if self.factor_result else None
        return {
            "market": self.market,
            "signal_date": self.signal_date.isoformat(),
            "signal_timestamp": self.signal_timestamp,
            "execution_rule": (
                "Signal generated after market close. Execution occurs at the next "
                "available market open; same-session closing prices are never used as "
                "entry prices."
            ),
            "methodology_version": METHODOLOGY_VERSION,
            "config_hash": self.config_hash,
            "config": self.config.to_dict(),
            "data_provider": self.provider,
            "listing_currency": MARKET_CURRENCY[self.market],
            "fx_rates_to_usd": self.fx_rates,
            "universe": {
                "size": self.universe.size,
                "sources": self.universe.sources,
                "fetched_at": self.universe.fetched_at,
                "is_stale": self.universe.is_stale,
                "stale_reason": self.universe.stale_reason,
                "warnings": self.universe.warnings,
                "parse_exclusions": len(self.universe.excluded),
            },
            "coverage": self.coverage.to_dict(),
            "selection": {
                "eligible": self.factor_result.selection.eligible_count if self.factor_result and self.factor_result.selection else 0,
                "n_value": self.factor_result.selection.n_value if self.factor_result and self.factor_result.selection else 0,
                "n_final": len(self.selected),
                "value_screen_percentile": self.config.VALUE_SCREEN_PERCENTILE,
                "growth_keep_ratio": self.config.GROWTH_KEEP_RATIO,
            },
            "weighting": {
                "effective_stock_cap": weighting.effective_stock_cap if weighting else None,
                "effective_sector_cap": weighting.effective_sector_cap if weighting else None,
                "sector_weights": weighting.sector_weights if weighting else {},
                "notes": weighting.notes if weighting else [],
            },
            "notes": self.notes,
        }


def _exclusion_stage(reasons: list[str]) -> str:
    """Bucket an exclusion so the report can summarise where names are lost."""
    if not reasons:
        return "unknown"
    first = reasons[0]
    if first.startswith(("symbol_unresolved", "quote_type", "excluded_sector", "excluded_industry")):
        return "identity"
    if first.startswith(("market_cap", "below_min_market_cap", "quote_currency", "fx_rate")):
        return "market_cap"
    if first.startswith(("insufficient_annual", "ttm_", "ebitda", "latest_fy_ebitda", "normalized_")):
        return "financial_data"
    if first.startswith(("forward_growth", "expected_fcf")):
        return "expected_fcf"
    if first.startswith("enterprise_value"):
        return "enterprise_value"
    if first.startswith(("revenue_trend", "no_secondary_trend", "fcf_yield")):
        return "growth_trend"
    return "other"


def _raw_financial_rows(symbol: str, bundle: CompanyFundamentals) -> list[dict]:
    """Flatten a fundamentals bundle into auditable statement rows."""
    rows: list[dict] = []
    balances = {
        (b.period_type, b.period_end): b for b in [*bundle.annual_balance, *bundle.quarterly_balance]
    }
    for period in [*bundle.annual, *bundle.quarterly]:
        balance = balances.get((period.period_type, period.period_end))
        rows.append(
            {
                "ticker": symbol,
                "period_type": period.period_type,
                "period_end": period.period_end.isoformat(),
                "currency": period.currency or "",
                "revenue": period.revenue,
                "ebitda": period.ebitda,
                "ebitda_is_derived": period.ebitda_is_derived,
                "ebit": period.ebit,
                "operating_cashflow": period.operating_cashflow,
                "capex": period.capex,
                "depreciation_amortization": period.depreciation_amortization,
                "diluted_shares": period.diluted_shares,
                "basic_shares": period.basic_shares,
                "total_debt": balance.total_debt if balance else None,
                "cash_and_equivalents": balance.cash_and_equivalents if balance else None,
                "preferred_stock": balance.preferred_stock if balance else None,
                "minority_interest": balance.minority_interest if balance else None,
                "shares_outstanding": balance.shares_outstanding if balance else None,
            }
        )
    rows.sort(key=lambda r: (r["period_type"], r["period_end"]))
    return rows


def _fetch_all(
    provider: DataProvider, entries: list[UniverseEntry], max_workers: int
) -> dict[str, CompanyFundamentals]:
    """Fetch fundamentals for the whole universe with bounded concurrency."""
    results: dict[str, CompanyFundamentals] = {}
    total = len(entries)
    if total == 0:
        return results

    def work(entry: UniverseEntry) -> tuple[str, CompanyFundamentals]:
        return entry.yahoo_symbol, provider.get_fundamentals(entry.yahoo_symbol)

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        for index, (symbol, bundle) in enumerate(pool.map(work, entries), start=1):
            results[symbol] = bundle
            if index % 100 == 0 or index == total:
                log.info("fetched fundamentals for %d/%d securities", index, total)
    return results


def run_screen(
    market: str,
    provider: DataProvider,
    signal_date: date,
    *,
    limit: int | None = None,
    refresh_universe: bool = True,
    allow_universe_integrity_failure: bool = False,
    config: FactorConfig = CONFIG,
    max_workers: int = PROVIDER_MAX_WORKERS,
    universe: UniverseResult | None = None,
) -> ScreenResult:
    """Run the complete factor for one market as at ``signal_date``."""
    market = market.upper()
    log.info("screening %s as at %s using the %s provider", market, signal_date, provider.name)

    if universe is None:
        universe = build_universe(
            market,
            as_of=signal_date,
            refresh=refresh_universe,
            persist=True,
            allow_integrity_failure=allow_universe_integrity_failure,
        )

    entries = list(universe.entries)
    result = ScreenResult(
        market=market,
        signal_date=signal_date,
        signal_timestamp=utc_now_iso(),
        provider=provider.name,
        universe=universe,
        config=config,
        config_hash=config.config_hash(),
    )
    result.coverage.universe_size = len(entries)
    if universe.is_stale:
        result.notes.append(f"STALE UNIVERSE: {universe.stale_reason}")
    result.coverage.warnings.extend(universe.warnings)

    if limit is not None:
        entries = entries[: max(0, limit)]
        result.notes.append(f"limited to the first {len(entries)} securities (smoke test)")
        result.coverage.universe_size = len(entries)

    fx = build_fx_converter(
        provider,
        collect_currencies([market]),
        required=[MARKET_CURRENCY[market]],
    )
    result.fx_rates = fx.rates

    bundles = _fetch_all(provider, entries, max_workers)

    factors: list[CompanyFactors] = []
    raw_rows: list[dict] = []
    for entry in entries:
        bundle = bundles.get(entry.yahoo_symbol) or CompanyFundamentals(symbol=entry.yahoo_symbol)
        company = evaluate_company(entry, bundle, fx, signal_date, config)
        factors.append(company)
        if bundle.metadata is not None:
            result.coverage.metadata_resolved += 1
        if (
            company.market_cap_usd is not None
            and company.market_cap_usd >= config.MIN_MARKET_CAP_USD
        ):
            result.coverage.above_market_cap += 1
            # Raw statements are archived for the names the factor actually
            # examined; archiving them for every micro-cap in the US would add
            # hundreds of megabytes per quarter for no audit value.
            raw_rows.extend(_raw_financial_rows(entry.yahoo_symbol, bundle))
        if not any(
            r.startswith(("symbol_unresolved", "quote_type", "excluded_sector", "excluded_industry"))
            for r in company.exclusion_reasons
        ):
            result.coverage.passed_security_filters += 1

    result.factors = factors
    result.raw_financials = raw_rows

    eligible = [c for c in factors if c.eligible]
    result.coverage.eligible = len(eligible)
    result.coverage.passed_financial_filters = len(eligible)

    for company in factors:
        for reason in company.exclusion_reasons:
            key = reason.split(":")[0]
            result.coverage.exclusion_counts[key] = result.coverage.exclusion_counts.get(key, 0) + 1
        for flag in company.flags:
            key = flag.split(":")[0]
            result.coverage.flag_counts[key] = result.coverage.flag_counts.get(key, 0) + 1

    factor_result = run_market_factor(market, eligible, config)
    result.factor_result = factor_result
    result.coverage.value_shortlist = len(factor_result.shortlist)
    result.coverage.selected = len(factor_result.selected)
    result.notes.extend(factor_result.notes)

    if not factor_result.selected:
        result.notes.append("no companies were selected: check the exclusion summary")

    log.info(
        "%s: %d in universe, %d eligible, %d shortlisted, %d selected",
        market,
        result.coverage.universe_size,
        result.coverage.eligible,
        result.coverage.value_shortlist,
        result.coverage.selected,
    )
    return result


def persist_screen(result: ScreenResult, *, allow_overwrite: bool = False) -> str:
    """Freeze a screening run into its immutable quarterly directory."""
    if result.provider == "synthetic":
        raise SyntheticDataRefused(
            "refusing to persist a snapshot built from synthetic data; "
            "the forward test must only ever record real observations"
        )
    path = write_signal_snapshot(
        result.market,
        result.signal_date,
        universe_rows=[entry.to_row() for entry in result.universe.entries],
        raw_financial_rows=result.raw_financials,
        factor_rows=[c.to_row() for c in result.factors],
        selected_rows=result.selected_rows(),
        excluded_rows=result.excluded_rows(),
        metadata=result.metadata(),
        allow_overwrite=allow_overwrite,
    )
    result.snapshot_path = str(path)
    log.info("wrote %s signal snapshot to %s", result.market, path)
    return str(path)
