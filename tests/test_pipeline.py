from __future__ import annotations

from pathlib import Path

import pytest

from lead_radar import pipeline
from lead_radar.cache import ResponseCache
from lead_radar.client import SerpApiGateway
from lead_radar.config import ScoringConfig
from lead_radar.demo import DEMO_NOW, DEMO_REQUEST, EMPTY_RESULTS, FixtureTransport
from lead_radar.errors import BadRequestError, InvalidApiKeyError, OutOfCreditsError
from lead_radar.models import SearchRequest
from tests.conftest import ScriptedTransport, maps_page

CONFIG = ScoringConfig()


def request(**overrides: object) -> SearchRequest:
    defaults: dict[str, object] = {"query": "roofing contractor", "locations": ["Tampa, FL"]}
    return SearchRequest.model_validate({**defaults, **overrides})


def titles(prefix: str, count: int) -> list[str]:
    return [f"{prefix}{index}" for index in range(count)]


# --- request parameters ------------------------------------------------------------------


def test_named_location_is_sent_as_location_plus_zoom() -> None:
    params = pipeline.build_maps_params("roofing contractor", "Tampa, FL", zoom=11)
    assert params == {
        "engine": "google_maps",
        "type": "search",
        "q": "roofing contractor",
        "hl": "en",
        "location": "Tampa, FL",
        "z": 11,
    }


@pytest.mark.parametrize("ll", ["@27.9506,-82.4572,12z", "@43.8521864,11.2168835,10410m"])
def test_coordinates_are_sent_as_ll_without_location_or_zoom(ll: str) -> None:
    params = pipeline.build_maps_params("plumber", ll, start=40)
    assert params["ll"] == ll and params["start"] == 40
    assert "location" not in params and "z" not in params, "ll can't be combined with them"


def test_max_pages_respects_serpapi_recommended_ceiling() -> None:
    assert [pipeline.max_pages(n) for n in (1, 20, 21, 40, 120, 500)] == [1, 1, 2, 2, 6, 6]


# --- pagination --------------------------------------------------------------------------


def test_follows_start_until_no_next_page() -> None:
    transport = ScriptedTransport(
        maps_page(titles("a", 20), more=True),
        maps_page(titles("b", 20), more=True),
        maps_page(titles("c", 7), more=False),
    )
    result = pipeline.run(request(max_results=100), SerpApiGateway(transport), CONFIG)

    assert [call.get("start", 0) for call in transport.calls] == [0, 20, 40]
    assert len(result.leads) == 47
    assert result.stats.api_calls == 3


def test_stops_at_max_results_without_fetching_another_page() -> None:
    transport = ScriptedTransport(
        maps_page(titles("a", 20), more=True), maps_page(titles("b", 20), more=True)
    )
    result = pipeline.run(request(max_results=25), SerpApiGateway(transport), CONFIG)

    assert len(transport.calls) == 2
    assert len(result.leads) == 25


def test_empty_page_ends_pagination() -> None:
    transport = ScriptedTransport(maps_page(titles("a", 20), more=True), dict(EMPTY_RESULTS))
    result = pipeline.run(request(max_results=100), SerpApiGateway(transport), CONFIG)
    assert len(result.leads) == 20 and len(transport.calls) == 2


def test_stops_when_google_repeats_the_same_page() -> None:
    same = maps_page(titles("a", 20), more=True)
    transport = ScriptedTransport(same, same)  # a third call would raise
    result = pipeline.run(request(max_results=100), SerpApiGateway(transport), CONFIG)

    assert len(result.leads) == 20
    assert result.duplicates_removed == 20


def test_no_results_at_all_is_an_empty_result_not_an_error() -> None:
    result = pipeline.run(request(), SerpApiGateway(ScriptedTransport(dict(EMPTY_RESULTS))), CONFIG)
    assert result.leads == [] and result.stop_reason is None


def test_single_place_answer_produces_a_warning() -> None:
    place = {"search_metadata": {"status": "Success"}, "place_results": {"title": "Acme Roofing"}}
    result = pipeline.run(request(), SerpApiGateway(ScriptedTransport(place)), CONFIG)
    assert result.leads == []
    assert "single place" in result.warnings[0]


# --- dedupe ------------------------------------------------------------------------------


def test_dedupes_by_place_id_across_pages_and_locations() -> None:
    transport = ScriptedTransport(
        maps_page(["a", "b", "c"], more=True),
        maps_page(["c", "d"], more=False),  # "c" again on page 2
        maps_page(["b", "e"], more=False),  # "b" again in the second location
    )
    result = pipeline.run(
        request(locations=["Tampa, FL", "St. Petersburg, FL"]), SerpApiGateway(transport), CONFIG
    )

    by_name = {lead.business.name: lead.business for lead in result.leads}
    assert sorted(by_name) == ["a", "b", "c", "d", "e"]
    assert result.duplicates_removed == 2
    assert by_name["b"].search_location == "Tampa, FL", "first sighting wins"
    assert by_name["e"].search_location == "St. Petersburg, FL"


def test_demo_data_end_to_end(
    demo_gateway: SerpApiGateway, demo_transport: FixtureTransport
) -> None:
    result = pipeline.run(DEMO_REQUEST, demo_gateway, CONFIG, now=DEMO_NOW)

    place_ids = [lead.business.place_id for lead in result.leads]
    assert len(place_ids) == len(set(place_ids)) == 38
    assert result.duplicates_removed == 3
    maps_calls = [c for c in demo_transport.calls if c["engine"] == "google_maps"]
    assert [(c["location"], c.get("start", 0)) for c in maps_calls] == [
        ("Tampa, FL", 0),
        ("Tampa, FL", 20),
        ("St. Petersburg, FL", 0),
    ]
    assert result.stats.api_calls == 3 + 38
    scores = [lead.score for lead in result.leads]
    assert scores == sorted(scores, reverse=True)
    assert all(lead.score == sum(r.points for r in lead.reasons) for lead in result.leads)


# --- reviews -----------------------------------------------------------------------------


def test_review_limit_only_fetches_reviews_for_the_best_leads(
    demo_gateway: SerpApiGateway, demo_transport: FixtureTransport
) -> None:
    limited = DEMO_REQUEST.model_copy(update={"review_limit": 5})
    result = pipeline.run(limited, demo_gateway, CONFIG, now=DEMO_NOW)

    review_calls = [c for c in demo_transport.calls if c["engine"] == "google_maps_reviews"]
    assert len(review_calls) == 5
    assert all(c["sort_by"] == "newestFirst" and "data_id" in c for c in review_calls)
    assert sum(lead.review_signals is not None for lead in result.leads) == 5


def test_a_failed_review_lookup_does_not_sink_the_run() -> None:
    transport = ScriptedTransport(
        maps_page(["a", "b"], more=False),
        BadRequestError("SerpApi rejected the request (HTTP 400): bad data_id"),
        {"reviews": []},
    )
    result = pipeline.run(request(with_reviews=True), SerpApiGateway(transport), CONFIG)

    assert len(result.leads) == 2
    assert len(result.warnings) == 1 and "Reviews unavailable" in result.warnings[0]
    assert result.stop_reason is None


# --- partial results ---------------------------------------------------------------------


def test_ctrl_c_keeps_what_was_collected() -> None:
    transport = ScriptedTransport(maps_page(titles("a", 20), more=True), KeyboardInterrupt())
    result = pipeline.run(request(max_results=100), SerpApiGateway(transport), CONFIG)

    assert result.interrupted
    assert len(result.leads) == 20
    assert result.stop_reason is not None and "Ctrl+C" in result.stop_reason


def test_running_out_of_credits_mid_run_keeps_partial_results() -> None:
    transport = ScriptedTransport(
        maps_page(titles("a", 20), more=True), OutOfCreditsError("run out of searches")
    )
    result = pipeline.run(request(max_results=100), SerpApiGateway(transport), CONFIG)

    assert len(result.leads) == 20 and not result.interrupted
    assert result.stop_reason is not None and "run out of searches" in result.stop_reason


def test_error_before_any_result_is_raised() -> None:
    transport = ScriptedTransport(InvalidApiKeyError("bad key"))
    with pytest.raises(InvalidApiKeyError):
        pipeline.run(request(), SerpApiGateway(transport), CONFIG)


# --- credit estimate ---------------------------------------------------------------------


def test_cold_estimate_is_the_worst_case() -> None:
    gateway = SerpApiGateway(ScriptedTransport())  # any API call would raise
    estimate = pipeline.estimate(
        request(locations=["Tampa, FL", "Orlando, FL"], max_results=50, with_reviews=True),
        gateway,
        CONFIG,
    )
    assert (estimate.maps_calls, estimate.review_calls, estimate.total) == (6, 100, 106)
    assert not estimate.exact and estimate.cached_calls == 0


def test_review_limit_bounds_the_estimate() -> None:
    estimate = pipeline.estimate(
        request(max_results=60, with_reviews=True, review_limit=10),
        SerpApiGateway(ScriptedTransport()),
        CONFIG,
    )
    assert (estimate.maps_calls, estimate.review_calls) == (3, 10)


def test_estimate_never_calls_the_api_and_rerun_is_free(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    first = SerpApiGateway(FixtureTransport(), cache=cache)
    assert pipeline.estimate(DEMO_REQUEST, first, CONFIG).total == 4 + 80
    pipeline.run(DEMO_REQUEST, first, CONFIG, now=DEMO_NOW)
    assert first.stats.api_calls == 41

    # Second run: a transport that fails on any call proves nothing is fetched again.
    second = SerpApiGateway(ScriptedTransport(), cache=cache)
    estimate = pipeline.estimate(DEMO_REQUEST, second, CONFIG)
    assert (estimate.total, estimate.cached_calls, estimate.exact) == (0, 41, True)

    rerun = pipeline.run(DEMO_REQUEST, second, CONFIG, now=DEMO_NOW)
    assert (rerun.stats.api_calls, rerun.stats.cache_hits) == (0, 41)
    assert len(rerun.leads) == 38


def test_estimate_with_partly_cached_pages(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    req = request(max_results=60)
    cache.put(
        pipeline.build_maps_params(req.query, "Tampa, FL"), maps_page(titles("a", 20), more=True)
    )

    estimate = pipeline.estimate(req, SerpApiGateway(ScriptedTransport(), cache=cache), CONFIG)
    assert (estimate.maps_calls, estimate.cached_calls, estimate.exact) == (2, 1, False)


def test_adding_reviews_to_a_cached_search_only_costs_the_reviews(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    without_reviews = DEMO_REQUEST.model_copy(update={"with_reviews": False})
    pipeline.run(without_reviews, SerpApiGateway(FixtureTransport(), cache=cache), CONFIG)

    with_limit = DEMO_REQUEST.model_copy(update={"review_limit": 10})
    estimate = pipeline.estimate(
        with_limit, SerpApiGateway(ScriptedTransport(), cache=cache), CONFIG
    )
    assert (estimate.maps_calls, estimate.review_calls, estimate.exact) == (0, 10, True)
