"""The only module that talks to SerpApi.

``SerpApiTransport`` is a thin adapter over the official ``serpapi`` client that turns its
exceptions into Lead Radar's typed errors. ``SerpApiGateway`` adds what a credit-metered API
needs on top: a disk cache, retries with exponential backoff, and call accounting.

Everything above this module depends on the ``Transport`` protocol only, which is how the
test-suite and ``lead-radar demo`` run fully offline.
"""

from __future__ import annotations

import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

import serpapi

from lead_radar.cache import ResponseCache
from lead_radar.errors import (
    BadRequestError,
    InvalidApiKeyError,
    LeadRadarError,
    MissingApiKeyError,
    OutOfCreditsError,
    RateLimitedError,
    SerpApiRequestError,
    TransientApiError,
)
from lead_radar.models import AccountInfo, RunStats

API_KEY_ENV = "SERPAPI_API_KEY"

T = TypeVar("T")


class Transport(Protocol):
    """What the gateway needs from an HTTP client."""

    def search(self, params: dict[str, Any]) -> dict[str, Any]: ...

    def account(self) -> dict[str, Any]: ...


def api_key_from_env() -> str:
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise MissingApiKeyError
    return key


def redact(text: str, secret: str) -> str:
    """Remove the API key from a message.

    ``requests`` puts the full request URL, ``api_key`` included, into its exception messages,
    so no upstream error text is ever shown without passing through here.
    """
    return text.replace(secret, "***") if secret else text


def translate_error(exc: Exception, *, secret: str = "") -> SerpApiRequestError:
    """Map a ``serpapi`` exception to a typed error with a message that is safe to print."""
    if isinstance(exc, serpapi.TimeoutError):
        return TransientApiError("The request to SerpApi timed out.")
    if isinstance(exc, serpapi.HTTPConnectionError):
        return TransientApiError("Could not reach serpapi.com. Check your network connection.")
    if isinstance(exc, serpapi.HTTPError):
        status: int = getattr(exc, "status_code", -1)
        detail = redact(str(getattr(exc, "error", None) or ""), secret)
        if status == 401:
            return InvalidApiKeyError(
                "SerpApi rejected the API key (HTTP 401). Check SERPAPI_API_KEY against "
                "https://serpapi.com/manage-api-key",
                status_code=status,
            )
        if status == 403:
            return InvalidApiKeyError(
                "This SerpApi account is not allowed to make requests (HTTP 403). "
                + (detail or "The account may have been deleted."),
                status_code=status,
            )
        if status == 429:
            if "run out of searches" in detail.lower():
                return OutOfCreditsError(
                    "Your SerpApi account has run out of searches. Cached results still work; "
                    "see https://serpapi.com/change-plan to add more.",
                    status_code=status,
                )
            return RateLimitedError(
                "SerpApi hourly throughput limit reached (HTTP 429). "
                + (detail or "Wait for the next hour or lower the request rate."),
                status_code=status,
            )
        if status == 400:
            return BadRequestError(
                f"SerpApi rejected the request (HTTP 400): {detail or 'bad request'}",
                status_code=status,
            )
        if status >= 500:
            return TransientApiError(
                f"SerpApi server error (HTTP {status}). {detail}".strip(), status_code=status
            )
        return SerpApiRequestError(
            f"SerpApi request failed (HTTP {status}). {detail}".strip(), status_code=status
        )
    return SerpApiRequestError(f"Unexpected error talking to SerpApi: {redact(str(exc), secret)}")


class SerpApiTransport:
    """Adapter over the official ``serpapi`` Python client."""

    def __init__(self, api_key: str, *, timeout: float = 30.0) -> None:
        self._api_key = api_key
        self._client = serpapi.Client(api_key=api_key, timeout=timeout)

    def search(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            # The client injects ``api_key`` into the dict it is given, so pass a copy.
            results = self._client.search(dict(params))
        except Exception as exc:
            # ``from None`` keeps the original exception, whose message contains the request
            # URL with the key, out of any traceback.
            raise translate_error(exc, secret=self._api_key) from None
        if isinstance(results, str):
            raise SerpApiRequestError("SerpApi returned a non-JSON response.")
        return dict(results)

    def account(self) -> dict[str, Any]:
        try:
            data = self._client.account()
        except Exception as exc:
            raise translate_error(exc, secret=self._api_key) from None
        if not isinstance(data, dict):
            raise SerpApiRequestError("The SerpApi Account API returned an unexpected response.")
        return data


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with jitter for transient failures (network, timeout, 5xx)."""

    attempts: int = 4
    base_delay: float = 1.0
    max_delay: float = 20.0

    def delay(self, attempt: int) -> float:
        """Seconds to wait after failed attempt number ``attempt`` (1-based)."""
        ceiling = min(self.max_delay, self.base_delay * 2 ** (attempt - 1))
        return float(ceiling * random.uniform(0.5, 1.0))


class SerpApiGateway:
    """Cache-first, retrying access to SerpApi with call accounting."""

    def __init__(
        self,
        transport: Transport,
        *,
        cache: ResponseCache | None = None,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        on_retry: Callable[[int, float, SerpApiRequestError], None] | None = None,
    ) -> None:
        self._transport = transport
        self._cache = cache
        self._retry = retry or RetryPolicy()
        self._sleep = sleep
        self._on_retry = on_retry
        self.stats = RunStats()

    def peek(self, params: dict[str, Any]) -> dict[str, Any] | None:
        """Return the cached response for ``params`` without ever calling the API."""
        return self._cache.get(params) if self._cache else None

    def search(self, params: dict[str, Any]) -> dict[str, Any]:
        """Run a search, serving it from the local cache when possible."""
        cached = self.peek(params)
        if cached is not None:
            self.stats.cache_hits += 1
            return cached
        response = self._with_retries(lambda: self._checked_search(params))
        self.stats.api_calls += 1
        if self._cache is not None:
            self._cache.put(params, response)
        return response

    def account(self) -> AccountInfo:
        """Account API: free, never counted against the quota, never cached."""
        return AccountInfo.model_validate(self._with_retries(self._transport.account))

    def _checked_search(self, params: dict[str, Any]) -> dict[str, Any]:
        response = self._transport.search(params)
        metadata = response.get("search_metadata")
        status = metadata.get("status") if isinstance(metadata, dict) else None
        # "Success" covers empty results too; "Error" means SerpApi could not complete the
        # search (proxy timeout etc.), which is worth retrying and must never be cached.
        if status == "Error":
            raise TransientApiError(
                f"SerpApi could not complete the search: {response.get('error', 'unknown error')}"
            )
        return response

    def _with_retries(self, call: Callable[[], T]) -> T:
        attempt = 1
        while True:
            try:
                return call()
            except TransientApiError as exc:
                if attempt >= self._retry.attempts:
                    raise TransientApiError(
                        f"{exc} Gave up after {attempt} attempts.", status_code=exc.status_code
                    ) from None
                delay = self._retry.delay(attempt)
                self.stats.retries += 1
                if self._on_retry:
                    self._on_retry(attempt, delay, exc)
                self._sleep(delay)
                attempt += 1


def live_gateway(
    *,
    cache: ResponseCache | None,
    on_retry: Callable[[int, float, SerpApiRequestError], None] | None = None,
) -> SerpApiGateway:
    """Gateway for real requests, using the key from the environment."""
    return SerpApiGateway(SerpApiTransport(api_key_from_env()), cache=cache, on_retry=on_retry)


__all__ = [
    "API_KEY_ENV",
    "LeadRadarError",
    "RetryPolicy",
    "SerpApiGateway",
    "SerpApiTransport",
    "Transport",
    "api_key_from_env",
    "live_gateway",
    "redact",
    "translate_error",
]
