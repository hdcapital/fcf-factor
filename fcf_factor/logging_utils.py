"""Minimal, dependency-free logging setup shared by the CLI and the workflows."""

from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def setup_logging(level: str | int | None = None) -> None:
    """Configure root logging once, writing to stderr in a CI-friendly format."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    lvl = level or os.environ.get("FCF_LOG_LEVEL", "INFO")
    if isinstance(lvl, str):
        lvl = getattr(logging, lvl.upper(), logging.INFO)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(lvl)
    # yfinance/urllib3 are extremely chatty at DEBUG.
    logging.getLogger("yfinance").setLevel(logging.ERROR)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("peewee").setLevel(logging.ERROR)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
