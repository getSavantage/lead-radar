"""Typed errors, so the CLI and UI can show a precise message for each failure mode.

The HTTP status mapping follows https://serpapi.com/api-status-and-error-codes.
"""

from __future__ import annotations


class LeadRadarError(Exception):
    """Base class for every error Lead Radar raises on purpose."""


class ConfigError(LeadRadarError):
    """The scoring config (YAML) or a run parameter is invalid."""


class MissingApiKeyError(LeadRadarError):
    """SERPAPI_API_KEY is not set."""

    def __init__(self) -> None:
        super().__init__(
            "SERPAPI_API_KEY is not set. Add a line SERPAPI_API_KEY=your_key to the .env file "
            "in the project folder (see .env.example). Get a key at "
            "https://serpapi.com/manage-api-key"
        )


class SerpApiRequestError(LeadRadarError):
    """SerpApi rejected a request or could not be reached."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class InvalidApiKeyError(SerpApiRequestError):
    """HTTP 401/403: the key is wrong or the account cannot make requests."""


class OutOfCreditsError(SerpApiRequestError):
    """HTTP 429 with "run out of searches": the plan has no searches left."""


class RateLimitedError(SerpApiRequestError):
    """HTTP 429: the hourly throughput limit of the plan was exceeded."""


class BadRequestError(SerpApiRequestError):
    """HTTP 400: a parameter is missing or invalid."""


class TransientApiError(SerpApiRequestError):
    """Network failure, timeout or 5xx. Safe to retry."""
