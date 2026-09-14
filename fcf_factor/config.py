"""Single source of truth for every tunable assumption in the FCF factor.

Nothing in this repository should hard-code a factor threshold.  Import it from
here instead.  Every value below can be overridden with an environment variable
of the same name (see :func:`_env_float` / :func:`_env_int`), which makes it easy
to experiment locally without editing tracked files -- but note that the
*effective* configuration is hashed into every quarterly snapshot, so an
override is always visible in the audit trail.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

# --------------------------------------------------------------------------
# Methodology version.  Bump this whenever the factor definition changes.
# --------------------------------------------------------------------------
METHODOLOGY_VERSION = "1.0.0"

# --------------------------------------------------------------------------
# Markets
# --------------------------------------------------------------------------
MARKETS: tuple[str, ...] = ("AU", "US", "UK", "NZ", "CA")

#: Listing currency for each market.  ``GBp`` (pence) quotes are normalised to
#: GBP before anything else happens -- see :mod:`fcf_factor.currency`.
MARKET_CURRENCY: dict[str, str] = {
    "AU": "AUD",
    "US": "USD",
    "UK": "GBP",
    "NZ": "NZD",
    "CA": "CAD",
}

#: Yahoo Finance ticker suffixes used by each market's exchanges.
MARKET_SUFFIXES: dict[str, tuple[str, ...]] = {
    "AU": (".AX",),
    "US": ("",),
    "UK": (".L",),
    "NZ": (".NZ",),
    "CA": (".TO", ".V"),
}

#: IANA timezone of each market, used purely to decide "which trading session
#: has definitely closed" on manual runs.
MARKET_TIMEZONE: dict[str, str] = {
    "AU": "Australia/Sydney",
    "US": "America/New_York",
    "UK": "Europe/London",
    "NZ": "Pacific/Auckland",
    "CA": "America/Toronto",
}

#: Months in which the factor rebalances (signal generated after the first
#: Friday of the month has closed).
REBALANCE_MONTHS: tuple[int, ...] = (3, 6, 9, 12)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class FactorConfig:
    """Every number that can change a stock's selection or weight."""

    # ---- eligibility -----------------------------------------------------
    #: Minimum market capitalisation, expressed in USD, applied after FX
    #: conversion at the factor observation date.
    MIN_MARKET_CAP_USD: float = 50_000_000.0
    #: Minimum number of annual statement observations required to compute the
    #: growth/quality trends and the normalised FCF margin.
    MIN_ANNUAL_PERIODS: int = 3
    #: Preferred number of annual observations (used only for coverage stats).
    PREFERRED_ANNUAL_PERIODS: int = 4
    #: Annual statements older than this many days are treated as stale.
    MAX_STATEMENT_AGE_DAYS: int = 640

    # ---- forward growth --------------------------------------------------
    #: Weight on the multi-year revenue CAGR in the mechanical growth estimate.
    FORWARD_GROWTH_CAGR_WEIGHT: float = 0.60
    #: Weight on the latest year-on-year revenue growth.
    FORWARD_GROWTH_YOY_WEIGHT: float = 0.40
    FORWARD_GROWTH_MIN: float = -0.15
    FORWARD_GROWTH_MAX: float = 0.30

    # ---- expected FCF ----------------------------------------------------
    #: ExpectedFCF = CURRENT_FCF_WEIGHT * TTM FCF + FORWARD_FCF_WEIGHT * fwd FCF
    CURRENT_FCF_WEIGHT: float = 0.50
    FORWARD_FCF_WEIGHT: float = 0.50

    # ---- selection funnel ------------------------------------------------
    #: Fraction of the eligible universe kept by the FCF-yield screen.
    VALUE_SCREEN_PERCENTILE: float = 0.1875
    #: Fraction of the FCF-yield shortlist kept by the growth screen.
    GROWTH_KEEP_RATIO: float = 2.0 / 3.0

    # ---- weighting -------------------------------------------------------
    #: FCF yield is capped at this level *for weighting only*.
    FCF_YIELD_WEIGHT_CAP: float = 0.15
    MAX_STOCK_WEIGHT: float = 0.03
    MAX_SECTOR_WEIGHT: float = 0.35

    # ---- z-scores --------------------------------------------------------
    Z_SCORE_CLIP: float = 3.0

    # ---- portfolio -------------------------------------------------------
    TRANSACTION_COST_BPS: float = 0.0
    STARTING_NAV: float = 100.0

    # ---- data-quality safeguards ----------------------------------------
    #: |reported market cap / (price x shares)| must sit inside this band or the
    #: security is flagged (and excluded if the discrepancy is extreme).
    MARKET_CAP_TOLERANCE_LOW: float = 0.5
    MARKET_CAP_TOLERANCE_HIGH: float = 2.0
    #: Beyond this ratio the mismatch is considered unexplainable -> exclude.
    MARKET_CAP_HARD_TOLERANCE: float = 5.0
    #: FCF yields above this are flagged for review (but not re-ranked).
    EXTREME_FCF_YIELD: float = 0.50
    #: If the eligible universe shrinks by more than this fraction versus the
    #: previous quarter, the run raises a prominent warning (or fails).
    UNIVERSE_SHRINK_WARN: float = 0.35
    #: A raw universe smaller than this is never accepted for a market.
    MIN_RAW_UNIVERSE: dict[str, int] = field(
        default_factory=lambda: {"AU": 400, "US": 1500, "UK": 400, "NZ": 40, "CA": 500}
    )

    def to_dict(self) -> dict:
        return asdict(self)

    def config_hash(self) -> str:
        """Stable SHA-256 of the factor configuration + methodology version.

        Stored in every ``metadata.json`` so a future reader can prove which
        rule-set produced a given quarterly snapshot.
        """
        payload = {
            "methodology_version": METHODOLOGY_VERSION,
            "config": self.to_dict(),
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @classmethod
    def from_env(cls) -> FactorConfig:
        """Build a config, letting environment variables override the defaults."""
        kwargs: dict = {}
        blank = cls()
        for f in fields(cls):
            default = getattr(blank, f.name)
            if isinstance(default, dict):
                continue  # MIN_RAW_UNIVERSE is edited in this file, not via env
            if isinstance(default, bool):
                continue
            if isinstance(default, int):
                kwargs[f.name] = _env_int(f.name, default)
            else:
                kwargs[f.name] = _env_float(f.name, float(default))
        return cls(**kwargs)


#: The process-wide configuration instance.
CONFIG = FactorConfig.from_env()

# --------------------------------------------------------------------------
# Sector / security-type exclusions.  The strategy targets ordinary operating
# companies, so financials and property trusts are removed.
# --------------------------------------------------------------------------
EXCLUDED_SECTORS: tuple[str, ...] = ("Financial Services",)

EXCLUDED_INDUSTRY_KEYWORDS: tuple[str, ...] = (
    "bank",
    "insurance",
    "insurer",
    "reit",
    "capital markets",
    "asset management",
    "closed-end fund",
    "shell compan",
    "mortgage",
    "credit services",
    "financial conglomerate",
    "financial data",
)

#: Yahoo ``quoteType`` values that are acceptable.
ALLOWED_QUOTE_TYPES: tuple[str, ...] = ("EQUITY",)

# --------------------------------------------------------------------------
# Provider behaviour (network politeness).  These do not affect the factor and
# are therefore deliberately *not* part of the config hash.
# --------------------------------------------------------------------------
PROVIDER_MAX_WORKERS: int = _env_int("PROVIDER_MAX_WORKERS", 4)
PROVIDER_MAX_RETRIES: int = _env_int("PROVIDER_MAX_RETRIES", 4)
PROVIDER_BACKOFF_BASE: float = _env_float("PROVIDER_BACKOFF_BASE", 1.5)
PROVIDER_REQUEST_PAUSE: float = _env_float("PROVIDER_REQUEST_PAUSE", 0.25)
#: Fundamentals cache time-to-live, in hours.
CACHE_TTL_HOURS: float = _env_float("CACHE_TTL_HOURS", 20.0)

# --------------------------------------------------------------------------
# Filesystem layout
# --------------------------------------------------------------------------
REPO_ROOT = Path(os.environ.get("FCF_REPO_ROOT", Path(__file__).resolve().parent.parent))
DATA_DIR = Path(os.environ.get("FCF_DATA_DIR", REPO_ROOT / "data"))
REPORTS_DIR = Path(os.environ.get("FCF_REPORTS_DIR", REPO_ROOT / "reports"))
CACHE_DIR = Path(os.environ.get("FCF_CACHE_DIR", REPO_ROOT / ".cache"))

UNIVERSE_DIR = DATA_DIR / "universes"
SIGNALS_DIR = DATA_DIR / "signals"
PRICES_DIR = DATA_DIR / "prices"
PERFORMANCE_DIR = DATA_DIR / "performance"
STATE_DIR = DATA_DIR / "state"


def set_data_root(root: str | os.PathLike, reports: str | os.PathLike | None = None) -> None:
    """Point every data path at ``root``.

    Used by the test suite (and handy for a scratch run) so nothing ever writes
    into the committed ``data/`` tree by accident.  Storage resolves these names
    at call time, so a change here takes effect immediately.
    """
    global DATA_DIR, UNIVERSE_DIR, SIGNALS_DIR, PRICES_DIR, PERFORMANCE_DIR, STATE_DIR, REPORTS_DIR
    DATA_DIR = Path(root)
    UNIVERSE_DIR = DATA_DIR / "universes"
    SIGNALS_DIR = DATA_DIR / "signals"
    PRICES_DIR = DATA_DIR / "prices"
    PERFORMANCE_DIR = DATA_DIR / "performance"
    STATE_DIR = DATA_DIR / "state"
    if reports is not None:
        REPORTS_DIR = Path(reports)
