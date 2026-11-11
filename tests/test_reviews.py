from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lead_radar.config import Detection
from lead_radar.models import Review
from lead_radar.reviews import analyse_reviews, build_reviews_params, parse_reviews
from tests.conftest import business, load_fixture

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def review(text: str, *, rating: float = 1.0, days_ago: int = 10) -> Review:
    return Review(text=text, rating=rating, iso_date=NOW - timedelta(days=days_ago))


def test_params_use_data_id_and_newest_first() -> None:
    params = build_reviews_params(business(), hl="en", num=20)
    assert params == {
        "engine": "google_maps_reviews",
        "hl": "en",
        "sort_by": "newestFirst",
        "num": 20,
        "data_id": "0x1:0x2",
    }


def test_params_fall_back_to_place_id_then_none() -> None:
    by_place = build_reviews_params(business(data_id=None), hl="en", num=10)
    assert by_place is not None
    assert by_place["place_id"] == "place-acme" and "data_id" not in by_place
    assert build_reviews_params(business(data_id=None, place_id=None), hl="en", num=10) is None


def test_signals_from_documented_response() -> None:
    reviews = parse_reviews(load_fixture("reviews_docs_example.json"))
    signals = analyse_reviews(reviews, Detection(), now=NOW)

    assert signals.reviews_fetched == 5
    assert signals.recent_reviews == 4, "the 2024 review is outside the 180-day window"
    assert signals.recent_avg_rating == 2.5
    assert signals.owner_response_rate == 0.25
    assert [c.matched for c in signals.complaints] == ["voicemail", "couldn't reach"]
    assert "Straight to voicemail every time" in signals.complaints[0].excerpt


def test_praise_mentioning_competitors_voicemail_is_not_a_complaint() -> None:
    praise = review("Others sent me to voicemail, these guys answered at once!", rating=5.0)
    assert analyse_reviews([praise], Detection(), now=NOW).complaints == []


def test_old_complaints_do_not_count() -> None:
    old = review("They never called back.", days_ago=400)
    signals = analyse_reviews([old], Detection(), now=NOW)
    assert signals.complaints == [] and signals.recent_reviews == 0
    assert signals.owner_response_rate is None and signals.recent_avg_rating is None


@pytest.mark.parametrize(
    "text",
    [
        "They never called back",
        "never called me back",
        "Didn’t call back after the estimate",  # typographic apostrophe
        "no callback, no email",
        "Never returned my calls",
        "No one answers the phone",
        "nobody picked up",
        "There was no answer all week",
        "They don't answer their phone",
        "Left a voice mail twice",
        "Could not reach anyone",
        "can't get ahold of them",
        "Never heard back",
        "No response to my quote request",
    ],
)
def test_default_phrases_catch_common_wordings(text: str) -> None:
    assert len(analyse_reviews([review(text)], Detection(), now=NOW).complaints) == 1


@pytest.mark.parametrize(
    "text",
    ["Great roof, fair price", "They called back within the hour", "Answered all my questions"],
)
def test_default_phrases_ignore_unrelated_low_ratings(text: str) -> None:
    assert analyse_reviews([review(text)], Detection(), now=NOW).complaints == []


def test_empty_reviews_response() -> None:
    empty = {"error": "Google hasn't returned any results for this query."}
    signals = analyse_reviews(parse_reviews(empty), Detection(), now=NOW)
    assert signals.reviews_fetched == 0 and signals.complaints == []
