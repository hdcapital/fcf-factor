"""Yahoo Finance implementation of :class:`~fcf_factor.providers.base.DataProvider`.

Everything Yahoo-specific is confined to this file: statement row-label
aliases, the ``GBp`` quoting quirk, retry/backoff and caching.  The factor
engine never sees any of it.

Caveats worth knowing (all documented in the README as well):

* Yahoo usually exposes only ~4 annual periods, so the factor is designed to
  work with three.
* ``marketCap`` for London listings is reported in **pounds** while
  ``currentPrice`` is reported in **pence**.  We reconcile the two rather than
  trusting either blindly.
* EBITDA is missing for many smaller companies.  We reconstruct it as
  ``EBIT + D&A`` and flag the fallback rather than dropping the company.
* Statement row labels change between yfinance releases, hence the alias lists.
"""

from __future__ import annotations

import math
import random
import threading
import time
from datetime import date, datetime
from typing import Any

from ..config import (
    PROVIDER_BACKOFF_BASE,
    PROVIDER_MAX_RETRIES,
    PROVIDER_REQUEST_PAUSE,
    PROVIDER_THROTTLE_MAX_PAUSE,
    PROVIDER_THROTTLE_MAX_WAITS,
    PROVIDER_THROTTLE_PAUSE,
)
from ..currency import canonical_currency, minor_unit_divisor
from ..logging_utils import get_logger
from .base import (
    BalanceSheetSnapshot,
    CompanyMetadata,
    DataProvider,
    IncomeCashflowPeriod,
    PriceBar,
)
from .cache import JsonCache

log = get_logger(__name__)

# --------------------------------------------------------------------------
# Statement row-label aliases.  First match wins, so order matters.
# --------------------------------------------------------------------------
REVENUE_LABELS = (
    "Total Revenue",
    "Operating Revenue",
    "Revenue",
    "TotalRevenue",
)
EBITDA_LABELS = ("EBITDA", "Normalized EBITDA", "NormalizedEBITDA")
EBIT_LABELS = ("EBIT", "Operating Income", "OperatingIncome", "Total Operating Income As Reported")
DA_LABELS = (
    "Reconciled Depreciation",
    "Depreciation And Amortization",
    "Depreciation Amortization Depletion",
    "Depreciation & Amortization",
    "Depreciation",
)
CFO_LABELS = (
    "Operating Cash Flow",
    "Cash Flow From Continuing Operating Activities",
    "Total Cash From Operating Activities",
    # Australian and New Zealand companies mostly report operating cash flow
    # by the *direct* method (receipts from customers less payments to
    # suppliers), which Yahoo files under this line and not the ones above.
    # Without it nine in ten ASX and NZX companies had no annual FCF history.
    "Cash Flowsfromusedin Operating Activities Direct",
    "OperatingCashFlow",
    "CashFlowsfromusedinOperatingActivitiesDirect",
)
CAPEX_LABELS = (
    "Capital Expenditure",
    "Capital Expenditures",
    "Purchase Of PPE",
    "Net PPE Purchase And Sale",
    "CapitalExpenditure",
)
DILUTED_SHARE_LABELS = ("Diluted Average Shares", "DilutedAverageShares")
BASIC_SHARE_LABELS = ("Basic Average Shares", "BasicAverageShares")

TOTAL_DEBT_LABELS = ("Total Debt", "TotalDebt")
#: Borrowings only -- these Yahoo lines exclude capitalised leases.
LONG_TERM_BORROWING_LABELS = ("Long Term Debt", "LongTermDebt")
SHORT_TERM_BORROWING_LABELS = ("Current Debt", "Short Long Term Debt", "CurrentDebt")
#: Borrowings plus leases, used only to rebuild total debt when it is missing.
LONG_TERM_DEBT_AND_LEASE_LABELS = ("Long Term Debt And Capital Lease Obligation",)
SHORT_TERM_DEBT_AND_LEASE_LABELS = ("Current Debt And Capital Lease Obligation",)
#: Capitalised lease liabilities.  Yahoo publishes no lease line on the cash
#: flow statement, so the balance sheet is the only place leases appear.
LEASE_TOTAL_LABELS = ("Capital Lease Obligations", "CapitalLeaseObligations")
LEASE_LONG_TERM_LABELS = ("Long Term Capital Lease Obligation",)
LEASE_CURRENT_LABELS = ("Current Capital Lease Obligation",)
TOTAL_EQUITY_INCL_NCI_LABELS = ("Total Equity Gross Minority Interest",)
#: Income statement: group net income, parent net income, and the NCI line.
NET_INCOME_INCL_NCI_LABELS = (
    "Net Income Including Noncontrolling Interests",
    "NetIncomeIncludingNoncontrollingInterests",
)
NET_INCOME_PARENT_LABELS = ("Net Income", "NetIncome", "Net Income Common Stockholders")
NCI_EARNINGS_LABELS = ("Minority Interests", "MinorityInterests")
CASH_LABELS = (
    "Cash And Cash Equivalents",
    "Cash Cash Equivalents And Short Term Investments",
    "CashAndCashEquivalents",
    "Cash",
)
PREFERRED_LABELS = ("Preferred Stock", "Preferred Securities Outstanding", "Preferred Stock Equity")
#: Non-controlling interests on the balance sheet.
#:
#: ``Total Equity Gross Minority Interest`` is deliberately NOT listed here.
#: Despite the name it is *total shareholders' equity including* NCI, not the
#: NCI itself. Using it as a fallback adds the entire equity base to enterprise
#: value, which silently wrecks the valuation of every company that reports no
#: separate NCI line at all.
MINORITY_LABELS = ("Minority Interest", "Minority Interests")
SHARES_LABELS = ("Ordinary Shares Number", "Share Issued", "Common Stock Shares Outstanding")


def _first(mapping: dict[str, Any], labels: tuple[str, ...]) -> float | None:
    """Return the first present, finite value among ``labels``."""
    for label in labels:
        if label in mapping:
            value = mapping[label]
            if value is None:
                continue
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            if math.isnan(v) or math.isinf(v):
                continue
            return v
    return None


def _clean(value: Any) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


#: Sentinel for "the call failed", as opposed to "the call returned nothing".
#: Only the second may be cached.
_FAILED = object()


def is_rate_limit_error(exc: BaseException) -> bool:
    """True for Yahoo's throttling responses, whatever wrapper they arrive in."""
    if "ratelimit" in type(exc).__name__.lower():
        return True
    text = str(exc).lower()
    return "too many requests" in text or "rate limit" in text or "429" in text


class RateLimiter:
    """Serialises provider calls and enforces a minimum gap between them.

    It also holds a shared pause: once any worker is throttled, *every* worker
    waits, because the throttle applies to the whole runner's IP address.
    """

    def __init__(self, min_interval: float):
        self.min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._last = 0.0
        self._paused_until = 0.0

    def pause_all(self, seconds: float) -> None:
        with self._lock:
            self._paused_until = max(self._paused_until, time.monotonic() + max(0.0, seconds))

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._paused_until > now:
                time.sleep(self._paused_until - now)
                now = time.monotonic()
            if self.min_interval <= 0:
                return
            gap = now - self._last
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last = time.monotonic()


class YahooProvider(DataProvider):
    """Free Yahoo Finance data, fetched politely."""

    name = "yahoo"

    def __init__(
        self,
        max_retries: int = PROVIDER_MAX_RETRIES,
        backoff_base: float = PROVIDER_BACKOFF_BASE,
        request_pause: float = PROVIDER_REQUEST_PAUSE,
        cache_ttl_hours: float | None = None,
        throttle_pause: float = PROVIDER_THROTTLE_PAUSE,
        throttle_max_pause: float = PROVIDER_THROTTLE_MAX_PAUSE,
        throttle_max_waits: int = PROVIDER_THROTTLE_MAX_WAITS,
    ):
        self.max_retries = int(max_retries)
        self.backoff_base = float(backoff_base)
        self.limiter = RateLimiter(request_pause)
        self.throttle_pause = float(throttle_pause)
        self.throttle_max_pause = float(throttle_max_pause)
        self.throttle_max_waits = int(throttle_max_waits)
        self.throttle_events = 0
        self._throttle_streak = 0
        self._throttle_lock = threading.Lock()
        self._cache = JsonCache("yahoo", ttl_hours=cache_ttl_hours)
        self._fx_cache: dict[str, float | None] = {}
        self._fx_lock = threading.Lock()
        self._ticker_lock = threading.Lock()
        self._tickers: dict[str, Any] = {}

    # ---- plumbing --------------------------------------------------------
    def _ticker(self, symbol: str):
        import yfinance as yf

        with self._ticker_lock:
            if symbol not in self._tickers:
                self._tickers[symbol] = yf.Ticker(symbol)
            return self._tickers[symbol]

    def forget(self, symbol: str) -> None:
        """Drop the cached ``yf.Ticker`` so a re-fetch starts from scratch."""
        with self._ticker_lock:
            self._tickers.pop(symbol, None)

    def _retry(self, what: str, fn, *args, **kwargs):
        """Call ``fn`` with backoff.  Never raises; ``None`` on failure."""
        value = self._attempt(what, fn, *args, **kwargs)
        return None if value is _FAILED else value

    def _throttled(self, what: str) -> None:
        with self._throttle_lock:
            self.throttle_events += 1
            self._throttle_streak += 1
            pause = min(self.throttle_max_pause, self.throttle_pause * (2 ** (self._throttle_streak - 1)))
        log.warning("%s: provider is throttling requests; pausing all workers for %.0fs", what, pause)
        self.limiter.pause_all(pause)

    def _attempt(self, what: str, fn, *args, **kwargs):
        """Call ``fn`` with backoff; ``_FAILED`` if every attempt failed.

        Throttling ("Too Many Requests") is handled apart from ordinary errors:
        it pauses every worker, and up to ``throttle_max_waits`` of those
        pauses do not use up the ordinary retries.  Before this a throttle
        burnt the four retries in about four seconds and the security was
        recorded as unresolved, so a single throttle window silently removed a
        whole run of the alphabet from the screen.
        """
        last_exc: Exception | None = None
        attempt = 0
        throttle_waits = 0
        while attempt < self.max_retries:
            self.limiter.wait()
            try:
                value = fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - provider errors are varied
                last_exc = exc
                if is_rate_limit_error(exc) and throttle_waits < self.throttle_max_waits:
                    throttle_waits += 1
                    self._throttled(what)
                    continue
                attempt += 1
                if attempt >= self.max_retries:
                    break
                delay = (self.backoff_base ** (attempt - 1)) + random.uniform(0.0, 0.4)
                log.debug("%s failed (%s); retrying in %.1fs", what, exc, delay)
                time.sleep(delay)
                continue
            with self._throttle_lock:
                self._throttle_streak = 0
            return value
        log.warning("%s failed after %d attempts: %s", what, self.max_retries, last_exc)
        return _FAILED

    @staticmethod
    def _frame_to_periods(frame, period_type: str) -> list[dict[str, Any]]:
        """Convert a yfinance statement DataFrame into JSON-friendly records."""
        records: list[dict[str, Any]] = []
        if frame is None or getattr(frame, "empty", True):
            return records
        for column in frame.columns:
            col_date = _to_date(column)
            if col_date is None:
                continue
            values: dict[str, Any] = {}
            for label, value in frame[column].items():
                v = _clean(value)
                if v is not None:
                    values[str(label)] = v
            records.append({"period_end": col_date.isoformat(), "period_type": period_type, "values": values})
        records.sort(key=lambda r: r["period_end"])
        return records

    # ---- metadata --------------------------------------------------------
    def _raw_info(self, symbol: str) -> dict[str, Any] | None:
        cache_key = f"info:{symbol}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached or None
        info = self._attempt(f"info({symbol})", lambda: self._ticker(symbol).get_info())
        if info is _FAILED or not info:
            return None
        if not any(info.get(k) for k in ("quoteType", "longName", "shortName")):
            # A throttled or half-answered request comes back as a near-empty
            # dict.  Caching it would freeze "unknown security" for a day.
            return None
        slim = {
            k: info.get(k)
            for k in (
                "longName", "shortName", "sector", "industry", "quoteType", "exchange",
                "fullExchangeName", "currency", "financialCurrency", "currentPrice",
                "regularMarketPrice", "regularMarketPreviousClose", "sharesOutstanding",
                "impliedSharesOutstanding", "marketCap", "enterpriseValue", "totalRevenue",
                "ebitda", "operatingCashflow", "freeCashflow", "capitalExpenditures",
                "totalDebt", "totalCash", "regularMarketTime",
            )
        }
        self._cache.set(cache_key, slim)
        return slim

    def get_company_metadata(self, symbol: str) -> CompanyMetadata | None:
        info = self._raw_info(symbol)
        if not info:
            return None
        price = _clean(info.get("currentPrice"))
        if price is None:
            price = _clean(info.get("regularMarketPrice"))
        if price is None:
            price = _clean(info.get("regularMarketPreviousClose"))
        shares = _clean(info.get("sharesOutstanding")) or _clean(info.get("impliedSharesOutstanding"))
        price_date = None
        ts = info.get("regularMarketTime")
        if isinstance(ts, (int, float)) and ts > 0:
            try:
                price_date = datetime.utcfromtimestamp(float(ts)).date()
            except (OverflowError, OSError, ValueError):
                price_date = None
        capex = _clean(info.get("capitalExpenditures"))
        return CompanyMetadata(
            symbol=symbol,
            name=info.get("longName") or info.get("shortName"),
            sector=info.get("sector"),
            industry=info.get("industry"),
            quote_type=info.get("quoteType"),
            exchange=info.get("fullExchangeName") or info.get("exchange"),
            quote_currency=info.get("currency"),
            financial_currency=info.get("financialCurrency"),
            price=price,
            price_date=price_date,
            shares_outstanding=shares,
            market_cap=_clean(info.get("marketCap")),
            enterprise_value=_clean(info.get("enterpriseValue")),
            ttm_revenue=_clean(info.get("totalRevenue")),
            ttm_ebitda=_clean(info.get("ebitda")),
            ttm_operating_cashflow=_clean(info.get("operatingCashflow")),
            ttm_free_cashflow=_clean(info.get("freeCashflow")),
            ttm_capex=capex,
        )

    # ---- statements ------------------------------------------------------
    def _statement_records(self, symbol: str, kind: str, period: str) -> list[dict[str, Any]]:
        """``kind`` in {income, cashflow, balance}; ``period`` in {annual, quarterly}."""
        cache_key = f"stmt:{symbol}:{kind}:{period}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        attr = {
            ("income", "annual"): "income_stmt",
            ("income", "quarterly"): "quarterly_income_stmt",
            ("cashflow", "annual"): "cashflow",
            ("cashflow", "quarterly"): "quarterly_cashflow",
            ("balance", "annual"): "balance_sheet",
            ("balance", "quarterly"): "quarterly_balance_sheet",
        }[(kind, period)]

        frame = self._attempt(f"{attr}({symbol})", lambda: getattr(self._ticker(symbol), attr))
        if frame is _FAILED:
            # Not cached: a failed fetch must be retried, never remembered as
            # "this company has no statements".
            return []
        records = self._frame_to_periods(frame, period)
        self._cache.set(cache_key, records)
        return records

    def _income_cashflow(self, symbol: str, period: str) -> list[IncomeCashflowPeriod]:
        income = {r["period_end"]: r["values"] for r in self._statement_records(symbol, "income", period)}
        cash = {r["period_end"]: r["values"] for r in self._statement_records(symbol, "cashflow", period)}
        info = self._raw_info(symbol) or {}
        currency = info.get("financialCurrency")

        out: list[IncomeCashflowPeriod] = []
        for key in sorted(set(income) | set(cash)):
            inc = income.get(key, {})
            cfl = cash.get(key, {})
            period_end = _to_date(key)
            if period_end is None:
                continue
            ebit = _first(inc, EBIT_LABELS)
            ebitda = _first(inc, EBITDA_LABELS)
            da = _first(cfl, DA_LABELS)
            if da is None:
                da = _first(inc, DA_LABELS)
            derived = False
            if ebitda is None and ebit is not None and da is not None:
                ebitda = ebit + abs(da)
                derived = True
            ni_group = _first(inc, NET_INCOME_INCL_NCI_LABELS)
            ni_parent = _first(inc, NET_INCOME_PARENT_LABELS)
            nci_row = _first(inc, NCI_EARNINGS_LABELS)
            nci_from_row = False
            if ni_group is not None and ni_parent is not None:
                # Sign-safe: whatever the group earned that the parent didn't.
                nci = ni_group - ni_parent
            elif nci_row is not None:
                # Yahoo shows the NCI deduction as a negative number.  Taking
                # abs() is the conservative reading: it assumes the minorities
                # took profit rather than absorbed losses.
                nci = abs(nci_row)
                nci_from_row = True
            else:
                nci = None
            out.append(
                IncomeCashflowPeriod(
                    period_end=period_end,
                    period_type=period,
                    currency=currency,
                    revenue=_first(inc, REVENUE_LABELS),
                    ebitda=ebitda,
                    ebit=ebit,
                    operating_cashflow=_first(cfl, CFO_LABELS),
                    capex=_first(cfl, CAPEX_LABELS),
                    depreciation_amortization=da,
                    diluted_shares=_first(inc, DILUTED_SHARE_LABELS),
                    basic_shares=_first(inc, BASIC_SHARE_LABELS),
                    ebitda_is_derived=derived,
                    net_income_incl_nci=ni_group,
                    nci_earnings=nci,
                    nci_earnings_from_minority_row=nci_from_row,
                )
            )
        return out

    def get_annual_financials(self, symbol: str) -> list[IncomeCashflowPeriod]:
        return self._income_cashflow(symbol, "annual")

    def get_quarterly_financials(self, symbol: str) -> list[IncomeCashflowPeriod]:
        return self._income_cashflow(symbol, "quarterly")

    def get_balance_sheet(self, symbol: str, period: str = "annual") -> list[BalanceSheetSnapshot]:
        if period not in {"annual", "quarterly"}:
            raise ValueError(f"period must be 'annual' or 'quarterly', got {period!r}")
        info = self._raw_info(symbol) or {}
        currency = info.get("financialCurrency")
        out: list[BalanceSheetSnapshot] = []
        for record in self._statement_records(symbol, "balance", period):
            values = record["values"]
            period_end = _to_date(record["period_end"])
            if period_end is None:
                continue
            total_debt = _first(values, TOTAL_DEBT_LABELS)
            if total_debt is None:
                lt = _first(values, LONG_TERM_DEBT_AND_LEASE_LABELS)
                st = _first(values, SHORT_TERM_DEBT_AND_LEASE_LABELS)
                if lt is not None or st is not None:
                    total_debt = (lt or 0.0) + (st or 0.0)
            lt_borrow = _first(values, LONG_TERM_BORROWING_LABELS)
            st_borrow = _first(values, SHORT_TERM_BORROWING_LABELS)
            borrowings = None
            if lt_borrow is not None or st_borrow is not None:
                borrowings = (lt_borrow or 0.0) + (st_borrow or 0.0)
            leases = _first(values, LEASE_TOTAL_LABELS)
            if leases is None:
                lt_lease = _first(values, LEASE_LONG_TERM_LABELS)
                st_lease = _first(values, LEASE_CURRENT_LABELS)
                if lt_lease is not None or st_lease is not None:
                    leases = (lt_lease or 0.0) + (st_lease or 0.0)
            out.append(
                BalanceSheetSnapshot(
                    period_end=period_end,
                    period_type=period,
                    currency=currency,
                    total_debt=total_debt,
                    borrowings=borrowings,
                    lease_liabilities=leases,
                    cash_and_equivalents=_first(values, CASH_LABELS),
                    preferred_stock=_first(values, PREFERRED_LABELS),
                    minority_interest=_first(values, MINORITY_LABELS),
                    total_equity_incl_nci=_first(values, TOTAL_EQUITY_INCL_NCI_LABELS),
                    shares_outstanding=_first(values, SHARES_LABELS),
                )
            )
        return out

    # ---- prices ----------------------------------------------------------
    def get_prices(self, symbol: str, start: date, end: date) -> list[PriceBar]:
        """Daily bars with **raw** (non dividend-adjusted) OHLC plus actions.

        ``auto_adjust=False`` matters: the forward test must reconstruct total
        return from the prices and cash flows that were observable at the time,
        not from a retroactively adjusted series.
        """
        def _fetch():
            return self._ticker(symbol).history(
                start=start.isoformat(),
                # yfinance treats ``end`` as exclusive.
                end=(date.fromordinal(end.toordinal() + 1)).isoformat(),
                interval="1d",
                auto_adjust=False,
                actions=True,
                raise_errors=True,
            )

        frame = self._retry(f"history({symbol})", _fetch)
        if frame is None or getattr(frame, "empty", True):
            return []
        info = self._raw_info(symbol) or {}
        currency = info.get("currency")
        bars: list[PriceBar] = []
        for index, row in frame.iterrows():
            bar_date = _to_date(index)
            if bar_date is None:
                continue
            bars.append(
                PriceBar(
                    date=bar_date,
                    open=_clean(row.get("Open")),
                    high=_clean(row.get("High")),
                    low=_clean(row.get("Low")),
                    close=_clean(row.get("Close")),
                    adj_close=_clean(row.get("Adj Close")),
                    volume=_clean(row.get("Volume")),
                    dividend=_clean(row.get("Dividends")) or 0.0,
                    split=_clean(row.get("Stock Splits")) or 0.0,
                    currency=currency,
                )
            )
        bars.sort(key=lambda b: b.date)
        return bars

    # ---- FX --------------------------------------------------------------
    def get_fx_rate(self, base: str, quote: str = "USD") -> float | None:
        """Units of ``quote`` per one unit of ``base``.

        Minor-unit codes are handled: ``get_fx_rate("GBp")`` returns one
        hundredth of the GBP rate.
        """
        base_canon = canonical_currency(base)
        quote_canon = canonical_currency(quote)
        if base_canon is None or quote_canon is None:
            return None
        divisor = minor_unit_divisor(base) * (1.0 / minor_unit_divisor(quote))
        if base_canon == quote_canon:
            return 1.0 / divisor

        key = f"{base_canon}{quote_canon}"
        with self._fx_lock:
            if key in self._fx_cache:
                cached = self._fx_cache[key]
                return None if cached is None else cached / divisor

        pair = f"{base_canon}{quote_canon}=X"

        def _fetch():
            frame = self._ticker(pair).history(period="5d", interval="1d", raise_errors=True)
            if frame is None or frame.empty:
                return None
            return _clean(frame["Close"].dropna().iloc[-1])

        rate = self._retry(f"fx({pair})", _fetch)
        if rate is None or rate <= 0:
            # Try the inverted pair before giving up.
            inverse_pair = f"{quote_canon}{base_canon}=X"

            def _fetch_inverse():
                frame = self._ticker(inverse_pair).history(period="5d", interval="1d", raise_errors=True)
                if frame is None or frame.empty:
                    return None
                return _clean(frame["Close"].dropna().iloc[-1])

            inverse = self._retry(f"fx({inverse_pair})", _fetch_inverse)
            rate = (1.0 / inverse) if inverse and inverse > 0 else None

        with self._fx_lock:
            self._fx_cache[key] = rate
        return None if rate is None else rate / divisor
