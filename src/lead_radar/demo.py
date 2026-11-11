"""Offline replay of SerpApi responses from JSON files.

Used by the test-suite, by ``lead-radar demo`` and by the Streamlit demo mode, so the whole
pipeline can be exercised without an API key and without spending a credit.

The bundled files in ``demo_data/`` are synthetic: fictional businesses laid out exactly like
the documented SerpApi response format (see ``scripts/build_demo_fixtures.py``).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lead_radar.models import SearchRequest

DEMO_DATA_DIR = Path(__file__).parent / "demo_data"

# The demo pins "now" so that "recent" reviews stay recent and the committed example
# output is reproducible byte for byte.
DEMO_NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

DEMO_REQUEST = SearchRequest(
    query="roofing contractor",
    locations=["Tampa, FL", "St. Petersburg, FL"],
    max_results=40,
    with_reviews=True,
)

EMPTY_RESULTS: dict[str, Any] = {
    "search_metadata": {"status": "Success"},
    "search_information": {"local_results_state": "Fully empty"},
    "error": "Google hasn't returned any results for this query.",
}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def maps_fixture_name(location: str, start: int) -> str:
    return f"maps_{slug(location)}_start{start}.json"


def reviews_fixture_name(identifier: str) -> str:
    return f"reviews_{slug(identifier)}.json"


class FixtureTransport:
    """A ``Transport`` that answers from JSON files and records every call it receives."""

    def __init__(self, directory: Path = DEMO_DATA_DIR) -> None:
        self.directory = directory
        self.calls: list[dict[str, Any]] = []

    def _load(self, name: str) -> dict[str, Any] | None:
        path = self.directory / name
        if not path.is_file():
            return None
        data: dict[str, Any] = json.loads(path.read_text("utf-8"))
        return data

    def search(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(params))
        engine = params.get("engine")
        if engine == "google_maps":
            # Resolved locations arrive as "ll", so their fixtures are named after the coordinates.
            location = str(params.get("ll") or params.get("location") or "")
            name = maps_fixture_name(location, int(params.get("start", 0)))
        elif engine == "google_maps_reviews":
            name = reviews_fixture_name(str(params.get("data_id") or params.get("place_id")))
        else:
            raise ValueError(f"FixtureTransport has no fixtures for engine {engine!r}")
        return self._load(name) or dict(EMPTY_RESULTS)

    def account(self) -> dict[str, Any]:
        return self._load("account.json") or {}

    def locations(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Candidates recorded from the real (free, keyless) Locations API."""
        recorded = json.loads((self.directory / "locations.json").read_text("utf-8"))
        needle = query.lower()
        return [c for c in recorded if needle in str(c.get("name", "")).lower()][:limit]
