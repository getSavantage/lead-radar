"""On-disk cache of raw SerpApi responses, keyed by a hash of the request parameters.

SerpApi already serves identical requests from its own cache for free, but only for one hour.
This cache has no such limit, so iterating on scoring rules, re-exporting a report or
developing against real responses never spends a credit twice.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_CACHE_DIR = Path(".lead_radar_cache")

# Parameters that must not influence the key: the secret, and flags that do not change results.
_IGNORED_PARAMS = frozenset({"api_key", "no_cache", "async", "output"})


def cache_key(params: dict[str, Any]) -> str:
    """Stable SHA-256 of the request parameters, independent of key order and of the API key."""
    relevant = {k: str(v) for k, v in params.items() if k not in _IGNORED_PARAMS and v is not None}
    canonical = json.dumps(relevant, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ResponseCache:
    """Stores one JSON file per request under ``<directory>/<engine>/<hash>.json``."""

    def __init__(self, directory: Path = DEFAULT_CACHE_DIR, ttl: timedelta | None = None) -> None:
        self.directory = directory
        self.ttl = ttl

    def _path(self, params: dict[str, Any]) -> Path:
        engine = str(params.get("engine", "unknown")).replace("/", "_")
        return self.directory / engine / f"{cache_key(params)}.json"

    def get(self, params: dict[str, Any]) -> dict[str, Any] | None:
        """Return the cached response, or ``None`` if missing, expired or unreadable."""
        path = self._path(params)
        try:
            entry = json.loads(path.read_text("utf-8"))
            cached_at = datetime.fromisoformat(entry["cached_at"])
            response = entry["response"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if not isinstance(response, dict):
            return None
        if self.ttl is not None and datetime.now(UTC) - cached_at > self.ttl:
            return None
        return response

    def put(self, params: dict[str, Any], response: dict[str, Any]) -> None:
        """Write atomically, so an interrupted run never leaves a half-written entry."""
        path = self._path(params)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "cached_at": datetime.now(UTC).isoformat(),
            "params": {k: v for k, v in params.items() if k not in _IGNORED_PARAMS},
            "response": response,
        }
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(entry, handle, ensure_ascii=False)
            Path(tmp_name).replace(path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(tmp_name).unlink()
            raise

    def _entries(self) -> list[Path]:
        return sorted(self.directory.glob("*/*.json")) if self.directory.is_dir() else []

    def summary(self) -> dict[str, tuple[int, int]]:
        """``{engine: (entries, bytes)}`` for ``lead-radar cache info``."""
        totals: dict[str, tuple[int, int]] = {}
        for path in self._entries():
            count, size = totals.get(path.parent.name, (0, 0))
            totals[path.parent.name] = (count + 1, size + path.stat().st_size)
        return totals

    def clear(self) -> int:
        """Delete every cached response. Returns the number of entries removed."""
        entries = self._entries()
        for path in entries:
            path.unlink()
        return len(entries)
