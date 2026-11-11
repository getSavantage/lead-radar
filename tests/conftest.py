"""Shared test helpers. Nothing in this suite touches the network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lead_radar.client import SerpApiGateway
from lead_radar.config import ScoringConfig
from lead_radar.demo import FixtureTransport
from lead_radar.models import Business

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text("utf-8"))
    return data


class ScriptedTransport:
    """Answers ``search`` calls from a queue of responses or exceptions, in order."""

    def __init__(self, *script: dict[str, Any] | BaseException) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.account_response: dict[str, Any] | BaseException = {"total_searches_left": 100}

    def search(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(params))
        if not self.script:
            raise AssertionError(f"Unexpected extra API call: {params}")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def account(self) -> dict[str, Any]:
        if isinstance(self.account_response, BaseException):
            raise self.account_response
        return self.account_response


def maps_page(titles: list[str], *, more: bool, prefix: str = "place") -> dict[str, Any]:
    """A minimal google_maps response in the documented shape."""
    page: dict[str, Any] = {
        "search_metadata": {"status": "Success"},
        "local_results": [
            {
                "position": position,
                "title": title,
                "place_id": f"{prefix}-{title}",
                "data_id": f"0x{position}:{title}",
                "rating": 4.5,
                "reviews": 100,
                "type": "Roofing contractor",
            }
            for position, title in enumerate(titles, start=1)
        ],
    }
    if more:
        page["serpapi_pagination"] = {"next": "https://serpapi.com/search.json?start=20"}
    return page


def business(**overrides: Any) -> Business:
    """A healthy listing: has a site, booking, hours and plenty of reviews. Scores 0."""
    defaults: dict[str, Any] = {
        "name": "Acme Roofing",
        "place_id": "place-acme",
        "data_id": "0x1:0x2",
        "category": "Roofing contractor",
        "phone": "(813) 555-0100",
        "website": "https://acme.example/",
        "rating": 4.8,
        "review_count": 200,
        "hours": "Open",
        "booking_signals": ["online_appointments"],
        "search_location": "Tampa, FL",
    }
    return Business.model_validate({**defaults, **overrides})


@pytest.fixture
def config() -> ScoringConfig:
    return ScoringConfig()


@pytest.fixture
def demo_transport() -> FixtureTransport:
    return FixtureTransport()


@pytest.fixture
def demo_gateway(demo_transport: FixtureTransport) -> SerpApiGateway:
    return SerpApiGateway(demo_transport)
