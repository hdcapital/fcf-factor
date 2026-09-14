"""Quarterly email: one message per market, sent over authenticated TLS SMTP.

Credentials come only from the environment (GitHub Secrets in CI).  Nothing is
hard-coded, nothing is logged, and a missing credential raises
:class:`EmailConfigurationError` rather than letting a run report success while
sending nothing.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import date
from email.message import EmailMessage

from ..config import CONFIG, METHODOLOGY_VERSION
from ..logging_utils import get_logger
from ..performance import compare_signals, performance_summary
from ..reporting import EXECUTION_NOTE
from ..schedule import quarter_label
from ..storage import load_signal, signal_dir

log = get_logger(__name__)

REQUIRED_ENV = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO")


class EmailConfigurationError(RuntimeError):
    """Raised when SMTP settings are missing or unusable."""


@dataclass
class EmailConfig:
    host: str
    port: int
    user: str
    password: str
    sender: str
    recipients: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: dict | None = None) -> EmailConfig:
        source = env if env is not None else os.environ
        missing = [name for name in REQUIRED_ENV if not (source.get(name) or "").strip()]
        if missing:
            raise EmailConfigurationError(
                "SMTP configuration is incomplete; missing environment variable(s): "
                + ", ".join(missing)
                + ". Set these as GitHub repository secrets (Settings -> Secrets and "
                "variables -> Actions) before running the quarterly workflow."
            )
        raw_port = str(source.get("SMTP_PORT", "")).strip()
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise EmailConfigurationError(f"SMTP_PORT is not a number: {raw_port!r}") from exc
        recipients = [
            part.strip()
            for part in str(source.get("EMAIL_TO", "")).replace(";", ",").split(",")
            if part.strip()
        ]
        if not recipients:
            raise EmailConfigurationError("EMAIL_TO did not contain a usable address")
        return cls(
            host=str(source["SMTP_HOST"]).strip(),
            port=port,
            user=str(source["SMTP_USER"]).strip(),
            password=str(source["SMTP_PASSWORD"]),
            sender=str(source["EMAIL_FROM"]).strip(),
            recipients=recipients,
        )


def _pct(value, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if number != number:
        return "n/a"
    return f"{number * 100:.{digits}f}%"


def _num(value, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def _millions(value) -> str:
    try:
        return f"${float(value) / 1e6:,.1f}m"
    except (TypeError, ValueError):
        return "n/a"


def _escape(text) -> str:
    from html import escape

    return escape("" if text is None else str(text))


def _row(cells: list[str], tag: str = "td") -> str:
    style = (
        "padding:6px 10px;border-bottom:1px solid #e3e6ea;"
        + ("text-align:left;font-weight:600;background:#f4f6f8;" if tag == "th" else "")
    )
    return "<tr>" + "".join(f'<{tag} style="{style}">{c}</{tag}>' for c in cells) + "</tr>"


def build_email_html(market: str, signal_date: date) -> str:
    """Render the HTML body for one market's quarterly email."""
    market = market.upper()
    snapshot = load_signal(market, signal_date)
    metadata = snapshot.get("metadata", {})
    selected = snapshot.get("selected", [])
    coverage = metadata.get("coverage", {}) or {}
    universe_meta = metadata.get("universe", {}) or {}
    summary = performance_summary(market)
    comparison = compare_signals(
        market, signal_date, current_tickers=[r.get("ticker", "") for r in selected]
    )

    warnings: list[str] = list(coverage.get("warnings", []) or [])
    warnings.extend(metadata.get("notes", []) or [])
    if universe_meta.get("is_stale"):
        warnings.insert(0, f"STALE UNIVERSE: {universe_meta.get('stale_reason')}")
    flag_counts = coverage.get("flag_counts", {}) or {}
    for flag in ("market_cap_discrepancy", "market_cap_unit_mismatch_100x", "extreme_fcf_yield", "stale_statements"):
        if flag_counts.get(flag):
            warnings.append(f"{flag_counts[flag]} companies flagged: {flag}")

    funnel = _row(["Stage", "Count"], "th") + "".join(
        _row([_escape(label), _escape(value)])
        for label, value in (
            ("Raw universe", coverage.get("universe_size", 0)),
            (f"Above US${CONFIG.MIN_MARKET_CAP_USD:,.0f} market cap", coverage.get("above_market_cap", 0)),
            ("Surviving financial-data filters", coverage.get("eligible", 0)),
            ("FCF-yield shortlist", coverage.get("value_shortlist", 0)),
            ("Finally selected", coverage.get("selected", 0)),
        )
    )

    picks_rows = _row(
        ["Ticker", "Company", "Sector", "Market cap (USD)", "FCF yield", "Growth score", "Target weight"],
        "th",
    )
    for row in sorted(selected, key=lambda r: -_float(r.get("target_weight"))):
        picks_rows += _row(
            [
                f"<strong>{_escape(row.get('ticker'))}</strong>",
                _escape((row.get("company") or "")[:48]),
                _escape(row.get("sector") or ""),
                _millions(row.get("market_cap_usd")),
                _pct(row.get("fcf_yield")),
                _num(row.get("growth_score"), 3),
                f"<strong>{_pct(row.get('target_weight'))}</strong>",
            ]
        )

    performance_rows = _row(["Metric", "Value"], "th") + "".join(
        _row([_escape(label), value])
        for label, value in (
            ("Forward-test NAV", _num(summary.nav, 4) if summary.started else "not started (starts at 100.00)"),
            ("Since-inception return", _pct(summary.since_inception_return) if summary.started else "n/a"),
            ("Inception date", _escape(summary.inception_date or "pending first execution")),
            ("Return since last rebalance", _pct(summary.latest_quarter_return) if summary.started else "n/a"),
            ("Current holdings", _escape(summary.holdings)),
        )
    )

    warning_html = ""
    if warnings:
        items = "".join(f"<li>{_escape(w)}</li>" for w in warnings[:15])
        warning_html = (
            '<div style="background:#fff5f5;border-left:4px solid #d64545;padding:10px 14px;'
            'margin:16px 0;border-radius:4px;">'
            '<strong style="color:#a32222;">Data-quality warnings</strong>'
            f'<ul style="margin:8px 0 0 18px;padding:0;">{items}</ul></div>'
        )

    table_style = (
        'style="border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 18px 0;'
        'border:1px solid #e3e6ea;"'
    )

    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f7f8fa;">
<div style="max-width:860px;margin:0 auto;padding:24px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1c2024;background:#ffffff;">
  <h1 style="margin:0 0 4px 0;font-size:22px;">FCF Factor &ndash; {_escape(market)} &ndash; {_escape(quarter_label(signal_date))}</h1>
  <p style="margin:0 0 4px 0;color:#5a6470;font-size:14px;">Signal date: <strong>{_escape(signal_date.isoformat())}</strong>
     &middot; generated {_escape(metadata.get('signal_timestamp', 'n/a'))}</p>
  <p style="margin:0 0 16px 0;color:#5a6470;font-size:12px;">Methodology {_escape(METHODOLOGY_VERSION)}
     &middot; config hash <code>{_escape(str(metadata.get('config_hash', ''))[:16])}</code>
     &middot; provider {_escape(metadata.get('data_provider', 'n/a'))}</p>

  <div style="background:#eef4ff;border-left:4px solid #2f5fd0;padding:10px 14px;margin:0 0 18px 0;border-radius:4px;font-size:13px;">
    <strong>{_escape(EXECUTION_NOTE)}</strong>
  </div>

  {warning_html}

  <h2 style="font-size:16px;margin:18px 0 4px 0;">Selection funnel</h2>
  <table {table_style}>{funnel}</table>
  <p style="font-size:12px;color:#5a6470;margin:-10px 0 18px 0;">
    Metadata coverage {_pct(coverage.get('metadata_coverage'))} &middot;
    financial-data coverage of market-cap-qualified names {_pct(coverage.get('financial_coverage'))}
  </p>

  <h2 style="font-size:16px;margin:18px 0 4px 0;">Forward test</h2>
  <table {table_style}>{performance_rows}</table>

  <h2 style="font-size:16px;margin:18px 0 4px 0;">Changes versus the previous rebalance</h2>
  <p style="font-size:13px;margin:0 0 18px 0;">
    <strong>Previous signal:</strong> {_escape(comparison.previous_date.isoformat() if comparison.previous_date else 'none')}<br>
    <strong>Additions ({len(comparison.additions)}):</strong> {_escape(', '.join(comparison.additions) or 'none')}<br>
    <strong>Removals ({len(comparison.removals)}):</strong> {_escape(', '.join(comparison.removals) or 'none')}<br>
    <strong>Retained ({len(comparison.retained)}):</strong> {_escape(', '.join(comparison.retained) or 'none')}
  </p>

  <h2 style="font-size:16px;margin:18px 0 4px 0;">Latest picks ({len(selected)})</h2>
  <table {table_style}>{picks_rows}</table>

  <p style="font-size:11px;color:#8a929c;border-top:1px solid #e3e6ea;padding-top:12px;">
    Generated by the fcf-factor research system. This is systematic research output, not investment advice.
    Full snapshot: <code>data/signals/{_escape(market)}/{_escape(signal_date.isoformat())}/</code>
  </p>
</div></body></html>"""


def _float(value) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if out != out else out


def build_message(market: str, signal_date: date, config: EmailConfig) -> EmailMessage:
    """Assemble the full MIME message, attaching ``selected.csv`` when present."""
    market = market.upper()
    message = EmailMessage()
    message["Subject"] = f"FCF Factor – {market} – {quarter_label(signal_date)}"
    message["From"] = config.sender
    message["To"] = ", ".join(config.recipients)

    html = build_email_html(market, signal_date)
    snapshot = load_signal(market, signal_date)
    selected = snapshot.get("selected", [])
    plain_lines = [
        f"FCF Factor - {market} - {quarter_label(signal_date)}",
        f"Signal date: {signal_date.isoformat()}",
        "",
        EXECUTION_NOTE,
        "",
        f"{len(selected)} selected:",
    ]
    for row in sorted(selected, key=lambda r: -_float(r.get("target_weight"))):
        plain_lines.append(
            f"  {row.get('ticker', ''):<12} {_pct(row.get('target_weight')):>8}  "
            f"FCF yield {_pct(row.get('fcf_yield')):>8}  {(row.get('company') or '')[:40]}"
        )
    message.set_content("\n".join(plain_lines))
    message.add_alternative(html, subtype="html")

    csv_path = signal_dir(market, signal_date) / "selected.csv"
    if csv_path.exists():
        message.add_attachment(
            csv_path.read_bytes(),
            maintype="text",
            subtype="csv",
            filename=f"fcf_factor_{market}_{signal_date.isoformat()}_selected.csv",
        )
    return message


def send_quarterly_email(
    market: str,
    signal_date: date,
    *,
    env: dict | None = None,
    dry_run: bool = False,
) -> dict:
    """Send one market's quarterly email.

    Raises :class:`EmailConfigurationError` when credentials are missing, so a
    misconfigured workflow fails visibly instead of silently skipping the mail.
    """
    config = EmailConfig.from_env(env)
    message = build_message(market, signal_date, config)

    if dry_run:
        log.info("dry run: not sending %s", message["Subject"])
        return {
            "market": market.upper(),
            "sent": False,
            "dry_run": True,
            "subject": message["Subject"],
            "recipients": config.recipients,
        }

    context = ssl.create_default_context()
    if config.port == 465:
        with smtplib.SMTP_SSL(config.host, config.port, context=context, timeout=60) as server:
            server.login(config.user, config.password)
            server.send_message(message)
    else:
        with smtplib.SMTP(config.host, config.port, timeout=60) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(config.user, config.password)
            server.send_message(message)

    log.info("sent %s to %s", message["Subject"], ", ".join(config.recipients))
    return {
        "market": market.upper(),
        "sent": True,
        "dry_run": False,
        "subject": message["Subject"],
        "recipients": config.recipients,
    }
