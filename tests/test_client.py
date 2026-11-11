from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests
import serpapi

from lead_radar.cache import ResponseCache
from lead_radar.client import (
    RetryPolicy,
    SerpApiGateway,
    SerpApiTransport,
    api_key_from_env,
    translate_error,
)
from lead_radar.errors import (
    BadRequestError,
    InvalidApiKeyError,
    MissingApiKeyError,
    OutOfCreditsError,
    RateLimitedError,
    SerpApiRequestError,
    TransientApiError,
)
from tests.conftest import ScriptedTransport

SECRET = "sk-test-0123456789abcdef"
PARAMS = {"engine": "google_maps", "type": "search", "q": "roofing contractor"}
OK = {"search_metadata": {"status": "Success"}, "local_results": [{"title": "Acme"}]}


def http_error(status: int, body: dict[str, Any] | None = None) -> serpapi.HTTPError:
    """Build the exception the official client raises, URL (with the key) included."""
    response = requests.Response()
    response.status_code = status
    response.url = f"https://serpapi.com/search?engine=google_maps&api_key={SECRET}"
    response._content = json.dumps(body or {}).encode()
    original = requests.HTTPError(f"{status} Error for url: {response.url}", response=response)
    return serpapi.HTTPError(original)


def gateway_for(transport: ScriptedTransport, **kwargs: Any) -> tuple[SerpApiGateway, list[float]]:
    sleeps: list[float] = []
    return SerpApiGateway(transport, sleep=sleeps.append, **kwargs), sleeps


# --- error translation -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, {"error": "Invalid API key. Your API key should be here: ..."}, InvalidApiKeyError),
        (403, {}, InvalidApiKeyError),
        (429, {"error": "Your account has run out of searches."}, OutOfCreditsError),
        (429, {"error": "You are exceeding your hourly throughput limit."}, RateLimitedError),
        (400, {"error": "Missing query `q` parameter."}, BadRequestError),
        (500, {}, TransientApiError),
        (503, {"error": "We couldn't get valid results for this search."}, TransientApiError),
    ],
)
def test_http_status_maps_to_typed_error(
    status: int, body: dict[str, Any], expected: type[SerpApiRequestError]
) -> None:
    error = translate_error(http_error(status, body), secret=SECRET)
    assert type(error) is expected
    assert error.status_code == status
    assert SECRET not in str(error)


def test_bad_request_keeps_serpapi_explanation() -> None:
    error = translate_error(http_error(400, {"error": "Missing query `q` parameter."}))
    assert "Missing query `q` parameter." in str(error)


def test_network_failures_are_transient() -> None:
    timeout = serpapi.TimeoutError(requests.Timeout("timed out"))
    connection = serpapi.HTTPConnectionError(requests.ConnectionError(f"api_key={SECRET}"))
    for exc in (timeout, connection):
        error = translate_error(exc, secret=SECRET)
        assert isinstance(error, TransientApiError)
        assert SECRET not in str(error)


def test_unknown_errors_are_redacted() -> None:
    error = translate_error(RuntimeError(f"boom at ?api_key={SECRET}"), secret=SECRET)
    assert SECRET not in str(error)
    assert "boom" in str(error)


def test_transport_never_leaks_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = SerpApiTransport(SECRET)

    def explode(params: dict[str, Any]) -> None:
        raise http_error(401, {"error": "Invalid API key."})

    monkeypatch.setattr(transport._client, "search", explode)
    with pytest.raises(InvalidApiKeyError) as caught:
        transport.search(dict(PARAMS))

    assert SECRET not in str(caught.value)
    # The original exception (whose message has the URL with the key) must not be chained.
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_transport_does_not_mutate_caller_params(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = SerpApiTransport(SECRET)

    def fake_search(params: dict[str, Any]) -> dict[str, Any]:
        params["api_key"] = SECRET  # what the real client does to the dict it is given
        return OK

    monkeypatch.setattr(transport._client, "search", fake_search)
    params = dict(PARAMS)
    assert transport.search(params) == OK
    assert "api_key" not in params


def test_missing_key_has_actionable_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    with pytest.raises(MissingApiKeyError, match=r"\.env"):
        api_key_from_env()


# --- retries -----------------------------------------------------------------------------


def test_retries_transient_errors_with_growing_backoff() -> None:
    transport = ScriptedTransport(TransientApiError("503"), TransientApiError("timeout"), OK)
    gateway, sleeps = gateway_for(transport, retry=RetryPolicy(attempts=4, base_delay=1.0))

    assert gateway.search(dict(PARAMS)) == OK
    assert len(transport.calls) == 3
    assert gateway.stats.retries == 2
    assert gateway.stats.api_calls == 1
    assert 0.5 <= sleeps[0] <= 1.0
    assert 1.0 <= sleeps[1] <= 2.0


def test_gives_up_after_max_attempts() -> None:
    transport = ScriptedTransport(*[TransientApiError("503")] * 3)
    gateway, sleeps = gateway_for(transport, retry=RetryPolicy(attempts=3))

    with pytest.raises(TransientApiError, match="Gave up after 3 attempts"):
        gateway.search(dict(PARAMS))
    assert len(sleeps) == 2


@pytest.mark.parametrize(
    "error", [InvalidApiKeyError("bad key"), OutOfCreditsError("empty"), BadRequestError("bad")]
)
def test_permanent_errors_are_not_retried(error: SerpApiRequestError) -> None:
    transport = ScriptedTransport(error)
    gateway, sleeps = gateway_for(transport)

    with pytest.raises(type(error)):
        gateway.search(dict(PARAMS))
    assert len(transport.calls) == 1
    assert sleeps == []


def test_search_status_error_is_retried_and_never_cached(tmp_path: Path) -> None:
    failed = {"search_metadata": {"status": "Error"}, "error": "We couldn't get valid results."}
    transport = ScriptedTransport(failed, OK)
    cache = ResponseCache(tmp_path)
    gateway, _ = gateway_for(transport, cache=cache)

    assert gateway.search(dict(PARAMS)) == OK
    assert cache.get(PARAMS) == OK


def test_backoff_is_capped() -> None:
    policy = RetryPolicy(base_delay=1.0, max_delay=5.0)
    assert all(policy.delay(attempt) <= 5.0 for attempt in range(1, 12))


# --- caching -----------------------------------------------------------------------------


def test_second_identical_search_is_served_from_cache(tmp_path: Path) -> None:
    transport = ScriptedTransport(OK)
    gateway, _ = gateway_for(transport, cache=ResponseCache(tmp_path))

    assert gateway.search(dict(PARAMS)) == OK
    assert gateway.search(dict(PARAMS)) == OK  # ScriptedTransport would fail on a 2nd call

    assert len(transport.calls) == 1
    assert (gateway.stats.api_calls, gateway.stats.cache_hits) == (1, 1)


def test_cache_survives_across_gateways(tmp_path: Path) -> None:
    first, _ = gateway_for(ScriptedTransport(OK), cache=ResponseCache(tmp_path))
    first.search(dict(PARAMS))

    second, _ = gateway_for(ScriptedTransport(), cache=ResponseCache(tmp_path))
    assert second.search(dict(PARAMS)) == OK
    assert second.stats.api_calls == 0


def test_without_cache_every_search_hits_the_api() -> None:
    transport = ScriptedTransport(OK, OK)
    gateway, _ = gateway_for(transport)

    gateway.search(dict(PARAMS))
    gateway.search(dict(PARAMS))
    assert len(transport.calls) == 2
    assert gateway.peek(PARAMS) is None


def test_empty_results_are_a_cacheable_success(tmp_path: Path) -> None:
    empty = {
        "search_metadata": {"status": "Success"},
        "search_information": {"local_results_state": "Fully empty"},
        "error": "Google hasn't returned any results for this query.",
    }
    gateway, _ = gateway_for(ScriptedTransport(empty), cache=ResponseCache(tmp_path))
    assert gateway.search(dict(PARAMS)) == empty
    assert gateway.peek(PARAMS) == empty


# --- account -----------------------------------------------------------------------------


def test_account_parses_documented_fields_and_drops_the_key(demo_gateway: SerpApiGateway) -> None:
    account = demo_gateway.account()
    assert account.plan_name == "Demo Plan"
    assert account.total_searches_left == 962
    assert account.account_rate_limit_per_hour == 200
    assert "SECRET_API_KEY" not in account.model_dump_json()
    assert demo_gateway.stats.api_calls == 0, "the Account API is free and must not be counted"
