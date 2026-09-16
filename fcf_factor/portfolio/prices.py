"""Daily price capture for every security in the five live portfolios.

Design decisions that matter for the forward test:

* **Raw prices, not adjusted.**  Bars are stored with ``auto_adjust=False``, so
  the file holds what the market actually printed plus the dividend and split
  that occurred.  Total return is then reconstructed from cash flows we saw at
  the time, instead of from a series Yahoo re-adjusts retroactively.
* **Append only.**  An existing ``(date, ticker)`` row is never rewritten.  A
  late correction therefore cannot quietly rewrite last quarter's returns.
* **Generous lookback.**  Every run re-requests the last 7-10 calendar days and
  fills any gaps, so one failed GitHub Actions run does not leave a hole.
* **Pence normalised to pounds.**  London bars arrive in ``GBp``; they are
  divided by 100 and stored as GBP, dividends included, so a human reading the
  CSV sees one consistent unit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from ..config import MARKET_CURRENCY
from ..currency import canonical_currency, minor_unit_divisor
from ..logging_utils import get_logger
from ..providers.base import DataProvider
from ..storage import load_prices, upsert_prices, utc_now_iso

log = get_logger(__name__)

DEFAULT_LOOKBACK_DAYS = 10


@dataclass
class PriceCollectionResult:
    market: str
    tickers: list[str] = field(default_factory=list)
    added: int = 0
    skipped_existing: int = 0
    replaced_placeholder: int = 0
    failed: list[str] = field(default_factory=list)
    start: date | None = None
    end: date | None = None

    def to_dict(self) -> dict:
        return {
            "market": self.market,
            "tickers": len(self.tickers),
            "rows_added": self.added,
            "rows_already_present": self.skipped_existing,
            "placeholder_rows_healed": self.replaced_placeholder,
            "failed_tickers": self.failed,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
        }


def collect_prices(
    market: str,
    provider: DataProvider,
    tickers: list[str],
    *,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    end: date | None = None,
    fx_rate_to_usd: float | None = None,
) -> PriceCollectionResult:
    """Fetch and persist recent daily bars for ``tickers``.

    Securities with no new session simply contribute no rows -- market holidays
    need no special handling.
    """
    market = market.upper()
    end = end or date.today()
    start = end - timedelta(days=max(1, lookback_days))
    result = PriceCollectionResult(market=market, tickers=list(tickers), start=start, end=end)
    if not tickers:
        return result

    if fx_rate_to_usd is None:
        fx_rate_to_usd = provider.get_fx_rate(MARKET_CURRENCY[market], "USD")

    rows: list[dict] = []
    retrieved_at = utc_now_iso()
    for ticker in tickers:
        try:
            bars = provider.get_prices(ticker, start, end)
        except Exception as exc:  # noqa: BLE001
            log.warning("price fetch failed for %s: %s", ticker, exc)
            result.failed.append(ticker)
            continue
        if not bars:
            continue
        for bar in bars:
            if bar.close is None or bar.close <= 0:
                # A session that has not opened yet comes back as a blank bar.
                # Storing it would create a phantom session the NAV engine
                # could try to trade on, and the append-only rule would then
                # stop the real bar from ever replacing it.
                continue
            divisor = minor_unit_divisor(bar.currency)
            currency = canonical_currency(bar.currency) or MARKET_CURRENCY[market]

            def scale(value: float | None, _divisor: float = divisor) -> float | None:
                return None if value is None else value / _divisor

            rows.append(
                {
                    "date": bar.date.isoformat(),
                    "ticker": ticker,
                    "market": market,
                    "currency": currency,
                    "open": scale(bar.open),
                    "high": scale(bar.high),
                    "low": scale(bar.low),
                    "close": scale(bar.close),
                    "adj_close": scale(bar.adj_close),
                    "volume": bar.volume,
                    "dividend": scale(bar.dividend) or 0.0,
                    # A split factor is a ratio, so it is never rescaled.
                    "split": bar.split or 0.0,
                    "fx_rate_to_usd": fx_rate_to_usd,
                    "retrieved_at": retrieved_at,
                }
            )

    if rows:
        stats = upsert_prices(market, rows)
        result.added = stats["added"]
        result.skipped_existing = stats["skipped_existing"]
        result.replaced_placeholder = stats.get("replaced_placeholder", 0)
    return result


def price_index(market: str) -> dict[str, dict[str, dict]]:
    """``{date: {ticker: row}}`` for every stored price row in a market."""
    index: dict[str, dict[str, dict]] = {}
    for row in load_prices(market):
        day = str(row.get("date", ""))[:10]
        ticker = str(row.get("ticker", ""))
        if not day or not ticker:
            continue
        index.setdefault(day, {})[ticker] = row
    return index
