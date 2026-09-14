"""fcf-factor: a free-data free-cash-flow factor screen and forward test.

Five independent markets (AU, US, UK, NZ, CA), each with its own universe,
cross-sectional ranking and live portfolio.  See ``README.md`` for the
methodology and the operating instructions.
"""

from __future__ import annotations

from .config import CONFIG, MARKETS, METHODOLOGY_VERSION

__version__ = "1.0.0"

__all__ = ["CONFIG", "MARKETS", "METHODOLOGY_VERSION", "__version__"]
