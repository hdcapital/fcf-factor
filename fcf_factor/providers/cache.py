"""Small, honest, on-disk JSON cache used to avoid hammering the provider.

The cache is deliberately *not* committed to git (see ``.gitignore``): it is a
politeness device, not a data store.  Anything that must survive lives under
``data/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from ..config import CACHE_DIR, CACHE_TTL_HOURS
from ..logging_utils import get_logger

log = get_logger(__name__)


class JsonCache:
    def __init__(self, namespace: str, ttl_hours: float | None = None, root: Path | None = None):
        self.root = Path(root or CACHE_DIR) / namespace
        self.ttl_seconds = float(ttl_hours if ttl_hours is not None else CACHE_TTL_HOURS) * 3600.0
        self.enabled = os.environ.get("FCF_DISABLE_CACHE", "").lower() not in {"1", "true", "yes"}

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / digest[:2] / f"{digest}.json"

    def get(self, key: str) -> Any | None:
        if not self.enabled:
            return None
        path = self._path(key)
        if not path.exists():
            return None
        try:
            if self.ttl_seconds > 0 and (time.time() - path.stat().st_mtime) > self.ttl_seconds:
                return None
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:  # pragma: no cover - corrupt cache entry
            log.debug("cache read failed for %s: %s", key, exc)
            return None

    def set(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(value, fh)
            os.replace(tmp, path)
        except Exception as exc:  # pragma: no cover - disk problems
            log.debug("cache write failed for %s: %s", key, exc)
