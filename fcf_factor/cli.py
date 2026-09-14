"""Command-line interface.

Every operation the GitHub Actions workflows perform is available here, so the
whole system can be driven and debugged from a laptop:

.. code-block:: console

    python -m fcf_factor screen --market NZ --limit 20 --dry-run --provider synthetic
    python -m fcf_factor quarterly-run --markets AU,NZ --manual
    python -m fcf_factor daily-prices
    python -m fcf_factor update-portfolio
    python -m fcf_factor report
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime

from .config import CONFIG, MARKETS, METHODOLOGY_VERSION, PROVIDER_MAX_WORKERS
from .logging_utils import get_logger, setup_logging
from .notify.email import EmailConfigurationError, send_quarterly_email
from .performance import performance_summary
from .pipeline import persist_screen, run_screen
from .portfolio.nav import active_tickers, update_portfolio
from .portfolio.prices import DEFAULT_LOOKBACK_DAYS, collect_prices
from .providers import get_provider
from .reporting import write_index_report, write_market_report
from .schedule import quarter_label, resolve_signal_date, scheduled_rebalance_decision
from .storage import SnapshotExistsError, latest_signal_date, signal_exists
from .universe.registry import build_universe

log = get_logger(__name__)


def _parse_markets(value: str | None) -> list[str]:
    if not value or value.strip().lower() == "all":
        return list(MARKETS)
    markets = []
    for part in value.replace(";", ",").split(","):
        key = part.strip().upper()
        if not key:
            continue
        if key not in MARKETS:
            raise SystemExit(f"unknown market {key!r}; known markets are {', '.join(MARKETS)}")
        markets.append(key)
    return markets


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"invalid date {value!r}; expected YYYY-MM-DD") from exc


def _emit(payload: dict) -> None:
    """Print a JSON result and, under GitHub Actions, expose it as an output."""
    text = json.dumps(payload, indent=2, default=str)
    print(text)
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as fh:
            fh.write(f"result<<EOF_FCF\n{json.dumps(payload, default=str)}\nEOF_FCF\n")
            for key in ("due", "signal_date", "selected"):
                if key in payload:
                    fh.write(f"{key}={payload[key]}\n")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def cmd_universe(args: argparse.Namespace) -> int:
    results = {}
    exit_code = 0
    for market in _parse_markets(args.markets):
        try:
            universe = build_universe(
                market,
                as_of=_parse_date(args.as_of) or date.today(),
                refresh=not args.no_refresh,
                persist=not args.dry_run,
                allow_integrity_failure=args.allow_integrity_failure,
            )
            results[market] = {
                "size": universe.size,
                "sources": universe.sources,
                "is_stale": universe.is_stale,
                "stale_reason": universe.stale_reason,
                "parse_exclusions": len(universe.excluded),
                "warnings": universe.warnings,
                "sample": [e.yahoo_symbol for e in universe.entries[:10]],
            }
        except Exception as exc:  # noqa: BLE001
            log.error("universe build failed for %s: %s", market, exc)
            results[market] = {"error": str(exc)}
            exit_code = 1
    _emit({"command": "universe", "markets": results})
    return exit_code


def cmd_screen(args: argparse.Namespace) -> int:
    provider = get_provider(args.provider)
    markets = _parse_markets(args.markets or args.market)
    results: dict[str, dict] = {}
    exit_code = 0

    for market in markets:
        decision = resolve_signal_date(market, manual=True) if args.signal_date is None else None
        signal_date = _parse_date(args.signal_date) or (decision.signal_date if decision else None)
        if signal_date is None:  # pragma: no cover - defensive
            raise SystemExit("could not resolve a signal date")

        universe = None
        if args.provider == "synthetic":
            universe = _synthetic_universe(market, args.limit or 60)

        try:
            result = run_screen(
                market,
                provider,
                signal_date,
                limit=args.limit,
                refresh_universe=not args.no_universe_refresh,
                allow_universe_integrity_failure=args.allow_integrity_failure or args.limit is not None,
                max_workers=args.max_workers,
                universe=universe,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("screen failed for %s: %s", market, exc)
            results[market] = {"error": str(exc)}
            exit_code = 1
            continue

        summary = {
            "signal_date": signal_date.isoformat(),
            "universe": result.coverage.universe_size,
            "eligible": result.coverage.eligible,
            "shortlist": result.coverage.value_shortlist,
            "selected": result.coverage.selected,
            "notes": result.notes,
            "picks": [
                {
                    "ticker": c.ticker,
                    "company": c.company,
                    "sector": c.sector,
                    "market_cap_usd": c.market_cap_usd,
                    "fcf_yield": c.fcf_yield,
                    "growth_score": c.growth_score,
                    "target_weight": c.target_weight,
                }
                for c in result.selected
            ],
        }
        if not args.dry_run:
            try:
                summary["snapshot"] = persist_screen(result, allow_overwrite=args.allow_overwrite)
            except SnapshotExistsError as exc:
                log.error("%s", exc)
                summary["error"] = str(exc)
                exit_code = 1
            else:
                write_market_report(market, signal_date)
        else:
            summary["snapshot"] = None
            summary["dry_run"] = True
        results[market] = summary
        _print_picks(market, signal_date, result)

    _emit({"command": "screen", "provider": args.provider, "markets": results})
    return exit_code


def _synthetic_universe(market: str, count: int):
    """Build an offline universe so the smoke test never touches the network."""
    from .providers.synthetic import synthetic_universe_symbols
    from .universe.base import UniverseEntry, UniverseResult

    entries = [
        UniverseEntry(
            market=market,
            exchange="SYNTHETIC",
            exchange_symbol=symbol.split(".")[0],
            yahoo_symbol=symbol,
            name=None,
            source="synthetic",
        )
        for symbol in synthetic_universe_symbols(market, count)
    ]
    result = UniverseResult(market=market, entries=entries, sources=["synthetic"])
    result.warnings.append("SYNTHETIC UNIVERSE: invented data, never persisted")
    return result


def _print_picks(market: str, signal_date: date, result) -> None:
    print(f"\n=== {market} {quarter_label(signal_date)} ({signal_date}) ===", file=sys.stderr)
    print(
        f"universe {result.coverage.universe_size} -> eligible {result.coverage.eligible} -> "
        f"shortlist {result.coverage.value_shortlist} -> selected {result.coverage.selected}",
        file=sys.stderr,
    )
    header = f"{'TICKER':<12}{'WEIGHT':>9}{'FCF YLD':>10}{'GROWTH':>9}  COMPANY"
    print(header, file=sys.stderr)
    for company in result.selected:
        print(
            f"{company.ticker:<12}"
            f"{(company.target_weight or 0) * 100:>8.2f}%"
            f"{(company.fcf_yield or 0) * 100:>9.2f}%"
            f"{company.growth_score or 0:>9.3f}"
            f"  {(company.company or '')[:38]}",
            file=sys.stderr,
        )
    print("", file=sys.stderr)


def cmd_quarterly_run(args: argparse.Namespace) -> int:
    """Full quarterly pipeline: screen, freeze, price, execute, report, email."""
    run_date = _parse_date(args.run_date) or datetime.now(UTC).date()
    markets = _parse_markets(args.markets)
    provider = get_provider(args.provider)
    results: dict[str, dict] = {}
    exit_code = 0

    for market in markets:
        decision = resolve_signal_date(
            market, run_date=run_date, manual=args.manual or args.force
        )
        if not decision.due:
            log.info("%s: no rebalance due (%s)", market, decision.reason)
            results[market] = {"skipped": True, "reason": decision.reason}
            continue
        signal_date = decision.signal_date
        assert signal_date is not None

        if signal_exists(market, signal_date) and not args.allow_overwrite:
            log.info("%s: snapshot for %s already exists; leaving it untouched", market, signal_date)
            results[market] = {
                "skipped": True,
                "reason": f"snapshot for {signal_date} already exists (snapshots are immutable)",
                "signal_date": signal_date.isoformat(),
            }
            continue

        try:
            result = run_screen(
                market,
                provider,
                signal_date,
                refresh_universe=not args.no_universe_refresh,
                allow_universe_integrity_failure=args.allow_integrity_failure,
                max_workers=args.max_workers,
            )
            entry = {
                "signal_date": signal_date.isoformat(),
                "reason": decision.reason,
                "universe": result.coverage.universe_size,
                "eligible": result.coverage.eligible,
                "selected": result.coverage.selected,
                "notes": result.notes,
            }
            if args.dry_run:
                entry["dry_run"] = True
                _print_picks(market, signal_date, result)
                results[market] = entry
                continue

            entry["snapshot"] = persist_screen(result, allow_overwrite=args.allow_overwrite)

            # Start collecting prices immediately so the next session can execute.
            tickers = active_tickers(market)
            price_stats = collect_prices(
                market, provider, tickers, lookback_days=args.lookback_days
            )
            entry["prices"] = price_stats.to_dict()

            update = update_portfolio(market)
            entry["portfolio"] = update.to_dict()

            write_market_report(market, signal_date)
            entry["report"] = f"reports/latest/{market}.md"

            if not args.no_email:
                try:
                    entry["email"] = send_quarterly_email(market, signal_date, dry_run=args.email_dry_run)
                except EmailConfigurationError as exc:
                    log.error("EMAIL NOT SENT for %s: %s", market, exc)
                    entry["email"] = {"sent": False, "error": str(exc)}
                    exit_code = 1
                except Exception as exc:  # noqa: BLE001
                    log.error("EMAIL FAILED for %s: %s", market, exc)
                    entry["email"] = {"sent": False, "error": str(exc)}
                    exit_code = 1
            results[market] = entry
            _print_picks(market, signal_date, result)
        except Exception as exc:  # noqa: BLE001
            log.exception("quarterly run failed for %s", market)
            results[market] = {"error": str(exc)}
            exit_code = 1

    write_index_report(list(MARKETS))
    _emit({"command": "quarterly-run", "run_date": run_date.isoformat(), "markets": results})
    return exit_code


def cmd_daily_prices(args: argparse.Namespace) -> int:
    provider = get_provider(args.provider)
    results: dict[str, dict] = {}
    exit_code = 0
    for market in _parse_markets(args.markets):
        tickers = active_tickers(market)
        if not tickers:
            results[market] = {"skipped": True, "reason": "no active or pending positions"}
            continue
        try:
            stats = collect_prices(market, provider, tickers, lookback_days=args.lookback_days)
            results[market] = stats.to_dict()
            if stats.failed:
                log.warning("%s: price fetch failed for %s", market, ", ".join(stats.failed))
        except Exception as exc:  # noqa: BLE001
            log.error("price collection failed for %s: %s", market, exc)
            results[market] = {"error": str(exc)}
            exit_code = 1
    _emit({"command": "daily-prices", "markets": results})
    return exit_code


def cmd_update_portfolio(args: argparse.Namespace) -> int:
    results: dict[str, dict] = {}
    exit_code = 0
    for market in _parse_markets(args.markets):
        try:
            update = update_portfolio(market, today=_parse_date(args.today), persist=not args.dry_run)
            results[market] = update.to_dict()
        except Exception as exc:  # noqa: BLE001
            log.exception("portfolio update failed for %s", market)
            results[market] = {"error": str(exc)}
            exit_code = 1
    _emit({"command": "update-portfolio", "markets": results})
    return exit_code


def cmd_report(args: argparse.Namespace) -> int:
    markets = _parse_markets(args.markets)
    written = []
    for market in markets:
        written.append(str(write_market_report(market)))
    written.append(str(write_index_report(list(MARKETS))))
    _emit({"command": "report", "written": written})
    return 0


def cmd_send_email(args: argparse.Namespace) -> int:
    results: dict[str, dict] = {}
    exit_code = 0
    for market in _parse_markets(args.markets or args.market):
        signal_date = _parse_date(args.signal_date) or latest_signal_date(market)
        if signal_date is None:
            results[market] = {"sent": False, "error": "no signal snapshot exists for this market"}
            exit_code = 1
            continue
        try:
            results[market] = send_quarterly_email(market, signal_date, dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001
            log.error("email failed for %s: %s", market, exc)
            results[market] = {"sent": False, "error": str(exc)}
            exit_code = 1
    _emit({"command": "send-email", "markets": results})
    return exit_code


def cmd_check_rebalance(args: argparse.Namespace) -> int:
    run_date = _parse_date(args.date) or datetime.now(UTC).date()
    decision = scheduled_rebalance_decision(run_date)
    _emit(
        {
            "command": "check-rebalance",
            "run_date": run_date.isoformat(),
            "due": str(decision.due).lower(),
            "signal_date": decision.signal_date.isoformat() if decision.signal_date else "",
            "reason": decision.reason,
        }
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    payload = {
        "command": "status",
        "methodology_version": METHODOLOGY_VERSION,
        "config_hash": CONFIG.config_hash(),
        "config": CONFIG.to_dict(),
        "markets": {},
    }
    for market in _parse_markets(args.markets):
        summary = performance_summary(market)
        signal_date = latest_signal_date(market)
        payload["markets"][market] = {
            "latest_signal": signal_date.isoformat() if signal_date else None,
            **summary.to_dict(),
        }
    _emit(payload)
    return 0


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fcf-factor",
        description="Free-cash-flow factor screening and forward testing for AU/US/UK/NZ/CA.",
    )
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING or ERROR")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_provider(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--provider",
            default="yahoo",
            choices=("yahoo", "synthetic"),
            help="data provider; 'synthetic' is offline invented data for smoke tests",
        )
        p.add_argument(
            "--max-workers",
            type=int,
            default=PROVIDER_MAX_WORKERS,
            help="concurrent provider requests (keep this low; the free API is shared)",
        )

    p_universe = sub.add_parser("universe", help="refresh and store the candidate universe")
    p_universe.add_argument("--markets", "--market", dest="markets", default="all")
    p_universe.add_argument("--as-of", default=None)
    p_universe.add_argument("--no-refresh", action="store_true", help="reuse the stored universe")
    p_universe.add_argument("--dry-run", action="store_true")
    p_universe.add_argument("--allow-integrity-failure", action="store_true")
    p_universe.set_defaults(func=cmd_universe)

    p_screen = sub.add_parser("screen", help="run the factor for one or more markets")
    p_screen.add_argument("--market", default=None, help="a single market, e.g. NZ")
    p_screen.add_argument("--markets", default=None, help="comma-separated list, or 'all'")
    p_screen.add_argument("--signal-date", default=None)
    p_screen.add_argument("--limit", type=int, default=None, help="only screen the first N securities")
    p_screen.add_argument("--dry-run", action="store_true", help="compute but never write")
    p_screen.add_argument("--no-universe-refresh", action="store_true")
    p_screen.add_argument("--allow-integrity-failure", action="store_true")
    p_screen.add_argument("--allow-overwrite", action="store_true", help="local use only")
    add_provider(p_screen)
    p_screen.set_defaults(func=cmd_screen)

    p_quarterly = sub.add_parser("quarterly-run", help="the full quarterly pipeline")
    p_quarterly.add_argument("--markets", default="all")
    p_quarterly.add_argument("--run-date", default=None, help="pretend today is this date")
    p_quarterly.add_argument("--manual", action="store_true", help="ignore the first-Friday schedule")
    p_quarterly.add_argument("--force", action="store_true", help="alias for --manual")
    p_quarterly.add_argument("--dry-run", action="store_true")
    p_quarterly.add_argument("--no-email", action="store_true")
    p_quarterly.add_argument("--email-dry-run", action="store_true", help="build but do not send")
    p_quarterly.add_argument("--no-universe-refresh", action="store_true")
    p_quarterly.add_argument("--allow-integrity-failure", action="store_true")
    p_quarterly.add_argument("--allow-overwrite", action="store_true")
    p_quarterly.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    add_provider(p_quarterly)
    p_quarterly.set_defaults(func=cmd_quarterly_run)

    p_prices = sub.add_parser("daily-prices", help="capture daily bars for live portfolios")
    p_prices.add_argument("--markets", default="all")
    p_prices.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    add_provider(p_prices)
    p_prices.set_defaults(func=cmd_daily_prices)

    p_portfolio = sub.add_parser("update-portfolio", help="execute signals and roll NAV forward")
    p_portfolio.add_argument("--markets", default="all")
    p_portfolio.add_argument("--today", default=None, help="process sessions up to this date")
    p_portfolio.add_argument("--dry-run", action="store_true")
    p_portfolio.set_defaults(func=cmd_update_portfolio)

    p_report = sub.add_parser("report", help="rewrite reports/latest/*.md")
    p_report.add_argument("--markets", default="all")
    p_report.set_defaults(func=cmd_report)

    p_email = sub.add_parser("send-email", help="email the latest picks for a market")
    p_email.add_argument("--market", default=None)
    p_email.add_argument("--markets", default=None)
    p_email.add_argument("--signal-date", default=None)
    p_email.add_argument("--dry-run", action="store_true", help="build the message but do not send")
    p_email.set_defaults(func=cmd_send_email)

    p_check = sub.add_parser("check-rebalance", help="is a scheduled rebalance due today?")
    p_check.add_argument("--date", default=None)
    p_check.set_defaults(func=cmd_check_rebalance)

    p_status = sub.add_parser("status", help="configuration and live portfolio summary")
    p_status.add_argument("--markets", default="all")
    p_status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
