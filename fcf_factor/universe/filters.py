"""Security-type and sector filters.

The strategy targets ordinary operating companies, so the universe adapters
strip out everything that is structurally not one: funds, trusts, preference
shares, warrants, rights, units, notes and shells.  Two layers do the work:

1. **Name/code based** (:func:`classify_security`), applied when the exchange
   list is parsed.  This is the only information available before any provider
   call, so it has to be conservative but decisive.
2. **Sector based** (:func:`sector_exclusion_reason`), applied after company
   metadata is fetched, which is where banks, insurers and REITs are removed.

Every rejection returns a machine-readable reason string; nothing is dropped
silently.
"""

from __future__ import annotations

import re

from ..config import ALLOWED_QUOTE_TYPES, EXCLUDED_INDUSTRY_KEYWORDS, EXCLUDED_SECTORS

#: Patterns in a security's *name* that identify a non-ordinary instrument.
NAME_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\betfs?\b", "etf"),
    (r"\bexchange[- ]traded\b", "etf"),
    (r"\bishares\b|\bspdr\b|\bvanguard\b|\bwisdomtree\b|\bproshares\b|\bvaneck\b", "etf"),
    (r"\bbetashares\b|\bglobal x\b|\bhorizons\b|\bpurpose\b\s+\w*\s*\bfund\b", "etf"),
    (r"\bindex fund\b|\betp\b|\betc\b\s*$", "etf"),
    (r"\bunit trust\b|\bmanaged fund\b|\bmutual fund\b|\binvestment fund\b", "fund"),
    (r"\bfunds?\b\s*$", "fund"),
    (r"\binvestment trust\b|\binvestment compan\w*\b|\binvestments? plc\b", "investment_company"),
    (r"\bcapital trust\b|\bincome trust\b|\bincome fund\b|\bgrowth fund\b", "fund"),
    (r"\bventure capital trust\b|\bvct\b", "fund"),
    (r"\bclosed[- ]end\b", "closed_end_fund"),
    (r"\breit\b|\breal estate investment trust\b", "reit"),
    (r"\bproperty trust\b|\bstapled\b", "property_trust"),
    (r"\bpref(?:erence|erred)?\b\.?\s*(shares?|stock|sec)", "preferred"),
    (r"\bcum\s?pref\b|\bcnv\s?pref\b|\bpfd\b", "preferred"),
    (r"\bwarrants?\b|\bwts?\b\.?$", "warrant"),
    (r"\boptions?\b(?!\s*(group|technolog))", "option"),
    (r"\brights?\b(?:\s+issue)?\s*$|\bnil paid\b", "right"),
    (r"\bunits?\b\s*$|\bstapled units?\b|\bsubordinate(d)? units?\b", "unit"),
    (r"\bdepositary (interest|receipt)s?\b|\badr\b|\bgdr\b", "depositary_receipt"),
    (r"\bnotes?\b\s*$|\bdebenture\b|\bbonds?\b\s*$|\bloan stock\b|\bfloating rate\b", "debt"),
    (r"\bspecial purpose acquisition\b|\bacquisition corp\b|\bacquisition compan\w*\b", "spac"),
    (r"\bblank check\b|\bshell compan\w*\b|\bcapital pool\b|\bcpc\b", "shell"),
    (r"\btest (issue|stock)\b", "test_issue"),
    (r"\bcontingent value right\b|\bcvr\b", "right"),
    (r"\bescrow\b|\bwhen[- ]issued\b", "other"),
)

_COMPILED = tuple((re.compile(pattern, re.IGNORECASE), label) for pattern, label in NAME_PATTERNS)

#: Ticker-root suffixes used by US exchanges for non-ordinary share classes.
US_NON_COMMON_SUFFIXES = ("W", "WS", "R", "RT", "U", "UN", "P", "PR", "CL")


def classify_security(name: str | None, symbol: str | None = None) -> str | None:
    """Return an exclusion label for non-ordinary securities, else ``None``."""
    text = (name or "").strip()
    if text:
        for pattern, label in _COMPILED:
            if pattern.search(text):
                return label
    if symbol:
        raw = symbol.strip().upper()
        if "$" in raw or "^" in raw:
            return "preferred"
        if raw.endswith("=") or raw.endswith("+"):
            return "warrant"
    return None


def us_symbol_exclusion(act_symbol: str) -> str | None:
    """Reject US ticker shapes that denote warrants, units, rights or preferreds.

    Nasdaq Trader encodes share class in the *ACT symbol*: ``BRK.A`` is a class
    share (kept), while ``ABC.W``/``ABC.U``/``ABC.R``/``ABC.P`` are warrants,
    units, rights and preferreds (dropped).  ``$`` always means preferred.
    """
    raw = (act_symbol or "").strip().upper()
    if not raw:
        return "symbol_missing"
    if "$" in raw:
        return "preferred"
    if raw.endswith("+") or raw.endswith("="):
        return "warrant"
    if "." in raw:
        tail = raw.split(".")[-1]
        if tail in US_NON_COMMON_SUFFIXES:
            return {
                "W": "warrant", "WS": "warrant",
                "R": "right", "RT": "right",
                "U": "unit", "UN": "unit",
                "P": "preferred", "PR": "preferred",
                "CL": "other",
            }[tail]
        if len(tail) > 1:
            return "other"
    return None


def sector_exclusion_reason(sector: str | None, industry: str | None) -> str | None:
    """Reject financials, banks, insurers and REITs using provider metadata."""
    sec = (sector or "").strip()
    ind = (industry or "").strip().lower()
    if sec and sec in EXCLUDED_SECTORS:
        return f"excluded_sector:{sec}"
    for keyword in EXCLUDED_INDUSTRY_KEYWORDS:
        if keyword in ind:
            return f"excluded_industry:{keyword}"
    return None


def quote_type_exclusion_reason(quote_type: str | None) -> str | None:
    """Reject anything the provider does not classify as an ordinary equity."""
    qt = (quote_type or "").strip().upper()
    if not qt:
        return "quote_type_missing"
    if qt not in ALLOWED_QUOTE_TYPES:
        return f"quote_type:{qt}"
    return None
