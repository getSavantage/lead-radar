from __future__ import annotations

import json
from pathlib import Path

from lead_radar import pipeline
from lead_radar.cache import ResponseCache
from lead_radar.client import SerpApiGateway
from lead_radar.config import ScoringConfig
from lead_radar.demo import DEMO_DATA_DIR
from lead_radar.locations import pick, split_query
from lead_radar.models import SearchRequest
from tests.conftest import ScriptedTransport, maps_page

# Recorded from the real Locations API (see scripts/build_demo_fixtures.py).
RECORDED = json.loads((DEMO_DATA_DIR / "locations.json").read_text("utf-8"))


def test_split_query_expands_us_state_codes() -> None:
    assert split_query("Tampa, FL") == ("Tampa", ["florida"])
    assert split_query(" St. Petersburg ,  Florida ") == ("St. Petersburg", ["florida"])
    assert split_query("Austin") == ("Austin", [])
    assert split_query("") == ("", [])


def test_prefers_the_city_over_the_media_market() -> None:
    """Regression: plain `location=Tampa, FL` was centred on the DMA, near Clearwater."""
    resolved = pick("Tampa, FL", RECORDED)
    assert resolved is not None
    assert (resolved.canonical_name, resolved.target_type) == (
        "Tampa,Florida,United States",
        "City",
    )
    assert resolved.ll(12) == "@27.950575,-82.4571776,12z"  # gps is [lng, lat] in the API


def test_name_must_match_exactly_and_qualifiers_must_agree() -> None:
    assert pick("Tampa, TX", RECORDED) is None  # no Tampa in Texas
    assert pick("Tamp, FL", RECORDED) is None
    assert pick("New Tampa, FL", RECORDED) is not None


def test_among_equal_types_the_largest_reach_wins() -> None:
    small = {"name": "Springfield", "canonical_name": "Springfield,Illinois,United States",
             "target_type": "City", "reach": 10, "gps": [-89.6, 39.8]}  # fmt: skip
    big = {**small, "canonical_name": "Springfield,Missouri,United States", "reach": 99,
           "gps": [-93.3, 37.2]}  # fmt: skip
    chosen = pick("Springfield", [small, big])
    assert chosen is not None and "Missouri" in chosen.canonical_name
    only_il = pick("Springfield, IL", [small, big])
    assert only_il is not None and "Illinois" in only_il.canonical_name


def test_unresolved_location_falls_back_to_text_with_a_warning() -> None:
    transport = ScriptedTransport(maps_page(["a"], more=False))
    transport.locations_response = []
    request = SearchRequest(query="roofer", locations=["Smallville, KS"])

    result = pipeline.run(request, SerpApiGateway(transport), ScoringConfig())

    assert transport.calls[0]["location"] == "Smallville, KS" and "ll" not in transport.calls[0]
    assert "Could not pin 'Smallville, KS'" in result.warnings[0]
    assert result.resolved_locations == {}


def test_explicit_coordinates_skip_the_lookup() -> None:
    transport = ScriptedTransport(maps_page(["a"], more=False))
    request = SearchRequest(query="roofer", locations=["@27.95,-82.46,13z"])

    result = pipeline.run(request, SerpApiGateway(transport), ScoringConfig())

    assert transport.location_queries == []
    assert transport.calls[0]["ll"] == "@27.95,-82.46,13z"
    assert result.warnings == []


def test_location_lookups_are_free_and_cached(tmp_path: Path) -> None:
    transport = ScriptedTransport()
    gateway = SerpApiGateway(transport, cache=ResponseCache(tmp_path))
    gateway.locations("Tampa")
    gateway.locations("Tampa")

    assert transport.location_queries == ["Tampa"]
    assert gateway.stats.api_calls == 0
