"""Markdown audit reports, one per market, written to ``reports/latest/``.

Presentation is deliberately plain.  The purpose of these files is that someone
can open ``reports/latest/AU.md`` in a year's time and check what the model
held, why, and how it has done -- not that they look good.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from . import config as _config
from .config import CONFIG, METHODOLOGY_VERSION
from .performance import (
    PerformanceSummary,
    compare_signals,
    distribution,
    percentiles,
    performance_summary,
)
from .schedule import quarter_label
from .storage import atomic_write_text, latest_signal_date, load_signal

EXECUTION_NOTE = (
    "Signal generated after market close. Forward-test execution occurs at the "
    "next available market open."
)


def _pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.{digits}f}%"


def _num(value, digits: int = 2) -> str:
    if value is None or value == "":
        return "n/a"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _money_millions(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return f"{number / 1e6:,.1f}m"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_none_\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(row) + " |")
    return "\n".join(out) + "\n"


def build_market_report(market: str, signal_date: date | None = None) -> str:
    """Render the latest-state markdown report for one market."""
    market = market.upper()
    signal_date = signal_date or latest_signal_date(market)
    summary = performance_summary(market)

    lines: list[str] = [f"# FCF Factor - {market}", ""]
    lines.append(f"_{EXECUTION_NOTE}_")
    lines.append("")
    lines.append(f"- Methodology version: `{METHODOLOGY_VERSION}`")
    lines.append(f"- Configuration hash: `{CONFIG.config_hash()}`")

    if signal_date is None:
        lines.append("")
        lines.append("No quarterly signal has been generated for this market yet.")
        lines.append("")
        lines.extend(_performance_section(summary))
        return "\n".join(lines) + "\n"

    snapshot = load_signal(market, signal_date)
    metadata = snapshot.get("metadata", {})
    selected = snapshot.get("selected", [])
    coverage = metadata.get("coverage", {})
    universe_meta = metadata.get("universe", {})

    lines.append(f"- Latest signal: **{signal_date.isoformat()}** ({quarter_label(signal_date)})")
    lines.append(f"- Signal generated at: `{metadata.get('signal_timestamp', 'n/a')}`")
    lines.append(f"- Data provider: `{metadata.get('data_provider', 'n/a')}`")
    lines.append("")

    # ---- funnel ---------------------------------------------------------
    lines.append("## Universe and coverage")
    lines.append("")
    lines.append(
        _table(
            ["Stage", "Count"],
            [
                ["Raw universe", str(coverage.get("universe_size", 0))],
                ["Provider metadata resolved", str(coverage.get("metadata_resolved", 0))],
                ["Passed security-type / sector filters", str(coverage.get("passed_security_filters", 0))],
                [f"Above US${CONFIG.MIN_MARKET_CAP_USD:,.0f} market cap", str(coverage.get("above_market_cap", 0))],
                ["Fully eligible (financial data complete)", str(coverage.get("eligible", 0))],
                ["FCF-yield shortlist", str(coverage.get("value_shortlist", 0))],
                ["Selected", str(coverage.get("selected", 0))],
            ],
        )
    )
    lines.append(
        f"Metadata coverage: {_pct(coverage.get('metadata_coverage'))} - "
        f"financial-data coverage of market-cap-qualified names: "
        f"{_pct(coverage.get('financial_coverage'))}"
    )
    lines.append("")
    if universe_meta.get("is_stale"):
        lines.append(f"> **Stale universe warning:** {universe_meta.get('stale_reason')}")
        lines.append("")
    for warning in coverage.get("warnings", []) or []:
        lines.append(f"> **Warning:** {warning}")
    for note in metadata.get("notes", []) or []:
        lines.append(f"> Note: {note}")
    if coverage.get("warnings") or metadata.get("notes"):
        lines.append("")

    # ---- picks -----------------------------------------------------------
    comparison = compare_signals(
        market, signal_date, current_tickers=[r.get("ticker", "") for r in selected]
    )
    lines.append("## Latest picks")
    lines.append("")
    rows = []
    for row in sorted(selected, key=lambda r: -(float(r.get("target_weight") or 0.0))):
        rows.append(
            [
                row.get("ticker", ""),
                (row.get("company", "") or "")[:40],
                row.get("sector", "") or "",
                _money_millions(row.get("market_cap_usd")),
                _pct(_safe_float(row.get("fcf_yield"))),
                _num(row.get("growth_score"), 3),
                _pct(_safe_float(row.get("target_weight"))),
            ]
        )
    lines.append(
        _table(
            ["Ticker", "Company", "Sector", "Mkt cap (USD)", "FCF yield", "Growth score", "Weight"],
            rows,
        )
    )

    lines.append("## Changes versus the previous rebalance")
    lines.append("")
    previous = comparison.previous_date.isoformat() if comparison.previous_date else "none"
    lines.append(f"Previous signal: `{previous}`")
    lines.append("")
    lines.append(f"- **Additions ({len(comparison.additions)}):** {_join(comparison.additions)}")
    lines.append(f"- **Removals ({len(comparison.removals)}):** {_join(comparison.removals)}")
    lines.append(f"- **Retained ({len(comparison.retained)}):** {_join(comparison.retained)}")
    lines.append("")

    # ---- distributions ---------------------------------------------------
    yields = [_safe_float(r.get("fcf_yield")) for r in selected]
    growth = [_safe_float(r.get("growth_score")) for r in selected]
    lines.append("## Distributions (selected names)")
    lines.append("")
    lines.append("### FCF yield")
    lines.append("")
    lines.append(_stats_table(percentiles(yields), as_percent=True))
    lines.append(_table(["Bucket", "Count"], [[b, str(c)] for b, c in distribution(yields)]))
    lines.append("### Growth score")
    lines.append("")
    lines.append(_stats_table(percentiles(growth)))
    lines.append(_table(["Bucket", "Count"], [[b, str(c)] for b, c in distribution(growth)]))

    sector_weights = (metadata.get("weighting", {}) or {}).get("sector_weights", {}) or {}
    if sector_weights:
        lines.append("### Sector weights")
        lines.append("")
        lines.append(
            _table(
                ["Sector", "Weight"],
                [[k, _pct(v)] for k, v in sorted(sector_weights.items(), key=lambda kv: -kv[1])],
            )
        )

    exclusions = coverage.get("exclusion_counts", {}) or {}
    if exclusions:
        lines.append("## Exclusion reasons")
        lines.append("")
        lines.append(
            _table(
                ["Reason", "Count"],
                [[k, str(v)] for k, v in sorted(exclusions.items(), key=lambda kv: -kv[1])[:25]],
            )
        )

    flags = coverage.get("flag_counts", {}) or {}
    if flags:
        lines.append("## Data-quality flags raised")
        lines.append("")
        lines.append(
            _table(
                ["Flag", "Count"],
                [[k, str(v)] for k, v in sorted(flags.items(), key=lambda kv: -kv[1])[:25]],
            )
        )

    lines.extend(_performance_section(summary))
    return "\n".join(lines) + "\n"


def _join(items: list[str], limit: int = 40) -> str:
    if not items:
        return "_none_"
    shown = items[:limit]
    suffix = "" if len(items) <= limit else f" ... (+{len(items) - limit} more)"
    return ", ".join(f"`{i}`" for i in shown) + suffix


def _safe_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


def _stats_table(stats: dict[str, float], as_percent: bool = False) -> str:
    if not stats:
        return "_no data_\n"
    fmt = (lambda v: _pct(v)) if as_percent else (lambda v: _num(v, 3))
    return _table(
        ["Min", "P25", "Median", "P75", "Max"],
        [[fmt(stats["min"]), fmt(stats["p25"]), fmt(stats["median"]), fmt(stats["p75"]), fmt(stats["max"])]],
    )


def _performance_section(summary: PerformanceSummary) -> list[str]:
    lines = ["## Forward-test performance", ""]
    if not summary.started:
        lines.append(
            "The portfolio has not executed yet. NAV starts at "
            f"{summary.starting_nav:.2f} on the first trading session after the first signal."
        )
        if summary.pending_signal_date:
            lines.append("")
            lines.append(f"Pending signal awaiting execution: `{summary.pending_signal_date}`")
        lines.append("")
        return lines

    lines.append(
        _table(
            ["Metric", "Value"],
            [
                ["NAV", _num(summary.nav, 4)],
                ["Inception date", summary.inception_date or "n/a"],
                ["As at", summary.latest_date or "n/a"],
                ["Since-inception return", _pct(summary.since_inception_return)],
                ["Return since last rebalance", _pct(summary.latest_quarter_return)],
                ["Latest daily return", _pct(summary.latest_daily_return)],
                ["Holdings", str(summary.holdings)],
                ["Turnover at last rebalance", _pct(summary.latest_turnover)],
                ["Sessions recorded", str(summary.sessions_recorded)],
            ],
        )
    )
    if summary.positions:
        lines.append("### Current positions (drifted weights)")
        lines.append("")
        lines.append(
            _table(
                ["Ticker", "Target weight", "Drifted weight", "Entry price", "Last price"],
                [
                    [
                        p["ticker"],
                        _pct(p.get("target_weight")),
                        _pct(p.get("drifted_weight")),
                        _num(p.get("entry_price"), 4),
                        _num(p.get("last_price"), 4),
                    ]
                    for p in summary.positions
                ],
            )
        )
    if summary.pending_signal_date:
        lines.append(f"Pending signal awaiting execution: `{summary.pending_signal_date}`")
        lines.append("")
    return lines


def write_market_report(market: str, signal_date: date | None = None) -> Path:
    """Write ``reports/latest/<MARKET>.md`` and return the path."""
    path = _config.REPORTS_DIR / "latest" / f"{market.upper()}.md"
    atomic_write_text(path, build_market_report(market, signal_date))
    return path


def write_index_report(markets: list[str]) -> Path:
    """Write a one-page index across all five markets."""
    lines = ["# FCF Factor - forward test", "", f"_{EXECUTION_NOTE}_", ""]
    rows = []
    for market in markets:
        summary = performance_summary(market)
        signal_date = latest_signal_date(market)
        rows.append(
            [
                f"[{market}]({market}.md)",
                signal_date.isoformat() if signal_date else "none",
                str(summary.holdings),
                _num(summary.nav, 3),
                _pct(summary.since_inception_return),
                summary.inception_date or "not started",
                summary.latest_date or "n/a",
            ]
        )
    lines.append(
        _table(
            ["Market", "Latest signal", "Holdings", "NAV", "Since inception", "Inception", "As at"],
            rows,
        )
    )
    path = _config.REPORTS_DIR / "latest" / "index.md"
    atomic_write_text(path, "\n".join(lines) + "\n")
    return path
