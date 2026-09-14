"""Universe adapter interface and shared HTTP plumbing."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime

import requests

from ..logging_utils import get_logger

log = get_logger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 fcf-factor/1.0 (research use)"
)


class UniverseFetchError(RuntimeError):
    """Raised when every configured source for a market fails."""


@dataclass(frozen=True)
class UniverseEntry:
    """One candidate security, before any provider call has been made."""

    market: str
    exchange: str
    exchange_symbol: str
    yahoo_symbol: str
    name: str | None = None
    industry: str | None = None
    source: str | None = None

    def to_row(self) -> dict:
        return {
            "market": self.market,
            "exchange": self.exchange,
            "exchange_symbol": self.exchange_symbol,
            "yahoo_symbol": self.yahoo_symbol,
            "name": self.name or "",
            "industry": self.industry or "",
            "source": self.source or "",
        }


@dataclass
class UniverseResult:
    """Outcome of a universe refresh for one market."""

    market: str
    entries: list[UniverseEntry] = field(default_factory=list)
    #: ``{exchange_symbol: reason}`` for securities removed while parsing.
    excluded: list[dict] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    fetched_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    #: True when the live refresh failed and a previously stored universe was
    #: reused.  Always surfaced in the run metadata and the email.
    is_stale: bool = False
    stale_reason: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.entries)


def http_get(
    url: str,
    *,
    timeout: float = 45.0,
    retries: int = 3,
    backoff: float = 2.0,
    headers: dict | None = None,
    params: dict | None = None,
) -> bytes:
    """GET with exponential backoff.  Raises the last error if all tries fail."""
    merged = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        merged.update(headers)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            response = requests.get(url, timeout=timeout, headers=merged, params=params)
            response.raise_for_status()
            return response.content
        except Exception as exc:  # noqa: BLE001 - network errors are varied
            last = exc
            if attempt < retries - 1:
                delay = backoff**attempt
                log.debug("GET %s failed (%s); retrying in %.1fs", url, exc, delay)
                time.sleep(delay)
    raise UniverseFetchError(f"GET {url} failed: {last}") from last


class UniverseAdapter(ABC):
    """Builds the raw candidate list for one market from official sources."""

    market: str = ""

    @abstractmethod
    def fetch(self) -> UniverseResult:
        """Retrieve and parse the exchange listing(s) for this market."""


def dedupe_entries(entries: list[UniverseEntry], result: UniverseResult) -> list[UniverseEntry]:
    """Drop duplicate Yahoo symbols, recording every collision as an exclusion.

    Duplicate tickers across venues (a TSX and a TSXV line for the same issuer,
    say) would otherwise be screened twice and double-weighted.
    """
    seen: dict[str, UniverseEntry] = {}
    for entry in entries:
        existing = seen.get(entry.yahoo_symbol)
        if existing is None:
            seen[entry.yahoo_symbol] = entry
            continue
        result.excluded.append(
            {
                "exchange_symbol": entry.exchange_symbol,
                "name": entry.name or "",
                "reason": f"duplicate_symbol:{existing.exchange}",
                "exchange": entry.exchange,
            }
        )
    return sorted(seen.values(), key=lambda e: e.yahoo_symbol)
