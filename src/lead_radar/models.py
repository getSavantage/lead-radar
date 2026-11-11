"""Pydantic models.

Field names on the wire come from the SerpApi docs (https://serpapi.com/maps-local-results,
https://serpapi.com/google-maps-reviews-api, https://serpapi.com/account-api). Where Lead Radar
uses a clearer name (``name`` for ``title``, ``review_count`` for ``reviews``), the SerpApi key
is kept as a validation alias, so the mapping is visible in one place.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import quote_plus, urlparse

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from lead_radar.config import Detection


def _clean_url(value: object) -> str | None:
    """Keep only http(s) URLs. Anything else (``javascript:``, junk) must never reach a link."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    return None


class Business(BaseModel):
    """One item of ``local_results`` from ``engine=google_maps``."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str = Field(validation_alias=AliasChoices("title", "name"))
    place_id: str | None = None
    data_id: str | None = None
    data_cid: str | None = None
    category: str | None = Field(default=None, validation_alias=AliasChoices("type", "category"))
    address: str | None = None
    phone: str | None = None
    website: str | None = None
    rating: float | None = None
    review_count: int | None = Field(
        default=None, validation_alias=AliasChoices("reviews", "review_count")
    )
    hours: str | None = None
    operating_hours: dict[str, str] = Field(default_factory=dict)
    unclaimed_listing: bool = False
    latitude: float | None = None
    longitude: float | None = None

    # Derived by Lead Radar.
    booking_link: str | None = None
    booking_signals: list[str] = Field(default_factory=list)
    search_query: str = ""
    search_location: str = ""

    @field_validator("data_cid", mode="before")
    @classmethod
    def _cid_to_str(cls, value: object) -> object:
        return str(value) if isinstance(value, int) else value

    @field_validator("website", "booking_link", mode="before")
    @classmethod
    def _safe_url(cls, value: object) -> str | None:
        return _clean_url(value)

    @classmethod
    def from_local_result(
        cls,
        raw: dict[str, Any],
        *,
        query: str,
        location: str,
        detection: Detection | None = None,
    ) -> Business:
        """Build a Business from a raw ``local_results`` item."""
        detection = detection or Detection()
        gps = raw.get("gps_coordinates")
        gps = gps if isinstance(gps, dict) else {}
        hours = raw.get("operating_hours")
        booking_link, booking_signals = detect_booking(raw, detection)
        return cls.model_validate(
            {
                **{key: raw.get(key) for key in _PASSTHROUGH_FIELDS if raw.get(key) is not None},
                "operating_hours": (
                    {str(k): str(v) for k, v in hours.items()} if isinstance(hours, dict) else {}
                ),
                "unclaimed_listing": raw.get("unclaimed_listing") is True,
                "latitude": gps.get("latitude"),
                "longitude": gps.get("longitude"),
                "booking_link": booking_link,
                "booking_signals": booking_signals,
                "search_query": query,
                "search_location": location,
            }
        )

    @property
    def dedupe_key(self) -> str:
        """``place_id`` identifies a place; fall back for the rare result without one."""
        return (
            self.place_id
            or self.data_id
            or self.data_cid
            or f"{self.name}|{self.address or ''}".lower()
        )

    @property
    def has_online_booking(self) -> bool:
        return bool(self.booking_link or self.booking_signals)

    @property
    def has_hours(self) -> bool:
        return bool(self.operating_hours or self.hours)

    @property
    def website_domain(self) -> str | None:
        if not self.website:
            return None
        host = urlparse(self.website).netloc.lower()
        return host.removeprefix("www.") or None

    @property
    def maps_url(self) -> str:
        """A stable Google Maps link built from the documented Maps URLs format."""
        url = f"https://www.google.com/maps/search/?api=1&query={quote_plus(self.name)}"
        if self.place_id:
            url += f"&query_place_id={quote_plus(self.place_id)}"
        return url


_PASSTHROUGH_FIELDS = (
    "title",
    "place_id",
    "data_id",
    "data_cid",
    "type",
    "address",
    "phone",
    "website",
    "rating",
    "reviews",
    "hours",
)


def detect_booking(raw: dict[str, Any], detection: Detection) -> tuple[str | None, list[str]]:
    """Find evidence that customers can book or order online.

    Returns the first booking URL found in the configured link fields, plus the list of
    human-readable signals (field names and matching service options/extensions).
    """
    link: str | None = None
    signals: list[str] = []
    for field in detection.booking_link_fields:
        url = _clean_url(raw.get(field))
        if url:
            link = link or url
            signals.append(field)

    keywords = [kw.lower() for kw in detection.booking_keywords]

    def matches(text: str) -> bool:
        normalised = text.replace("_", " ").lower()
        return any(kw in normalised for kw in keywords)

    service_options = raw.get("service_options")
    if isinstance(service_options, dict):
        signals += [str(k) for k, v in service_options.items() if v is True and matches(str(k))]
    extensions = raw.get("extensions")
    if isinstance(extensions, list):
        for group in extensions:
            if not isinstance(group, dict):
                continue
            for values in group.values():
                if isinstance(values, list):
                    signals += [v for v in values if isinstance(v, str) and matches(v)]
    return link, list(dict.fromkeys(signals))


class Review(BaseModel):
    """One item of ``reviews`` from ``engine=google_maps_reviews``."""

    review_id: str | None = None
    rating: float | None = None
    date: str | None = None
    iso_date: datetime | None = None
    text: str = ""
    has_owner_response: bool = False
    link: str | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> Review:
        extracted = raw.get("extracted_snippet")
        original = extracted.get("original") if isinstance(extracted, dict) else None
        text = original or raw.get("snippet") or ""
        return cls.model_validate(
            {
                "review_id": raw.get("review_id"),
                "rating": raw.get("rating"),
                "date": raw.get("date"),
                "iso_date": raw.get("iso_date"),
                "text": text if isinstance(text, str) else "",
                "has_owner_response": isinstance(raw.get("response"), dict),
                "link": _clean_url(raw.get("link")),
            }
        )


class Complaint(BaseModel):
    """A recent review that matched a missed-call phrase."""

    date: str | None = None
    rating: float | None = None
    matched: str
    excerpt: str


class ReviewSignals(BaseModel):
    """What the latest page of reviews says about a business."""

    reviews_fetched: int = 0
    recent_reviews: int = 0
    recent_avg_rating: float | None = None
    owner_response_rate: float | None = None
    complaints: list[Complaint] = Field(default_factory=list)


class Tier(StrEnum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class ScoreReason(BaseModel):
    """One fired rule: the "why" behind part of a score."""

    rule: str
    points: int
    message: str


class Lead(BaseModel):
    business: Business
    score: int
    tier: Tier
    reasons: list[ScoreReason] = Field(default_factory=list)
    review_signals: ReviewSignals | None = None

    @property
    def why(self) -> list[str]:
        return [f"+{reason.points} {reason.message}" for reason in self.reasons]


class AccountInfo(BaseModel):
    """Subset of the Account API response. The ``api_key`` field is deliberately not modelled."""

    model_config = ConfigDict(extra="ignore")

    account_status: str | None = None
    plan_name: str | None = None
    searches_per_month: int | None = None
    plan_searches_left: int | None = None
    extra_credits: int | None = None
    total_searches_left: int | None = None
    this_month_usage: int | None = None
    this_hour_searches: int | None = None
    account_rate_limit_per_hour: int | None = None


class SearchRequest(BaseModel):
    """Everything that defines a run."""

    query: str = Field(min_length=1)
    locations: list[str] = Field(min_length=1)
    max_results: int = Field(default=40, ge=1, le=500)
    """Per location."""
    zoom: int = Field(default=12, ge=3, le=30)
    hl: str = "en"
    with_reviews: bool = False
    review_limit: int | None = Field(default=None, ge=1)
    """Fetch reviews only for the N best leads (by listing-only score). ``None`` means all."""
    reviews_per_lead: int = Field(default=20, ge=1, le=60)

    @field_validator("query")
    @classmethod
    def _strip_query(cls, value: str) -> str:
        return value.strip()

    @field_validator("locations")
    @classmethod
    def _clean_locations(cls, value: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(loc.strip() for loc in value if loc.strip()))
        if not cleaned:
            raise ValueError("at least one location is required")
        return cleaned


class CreditEstimate(BaseModel):
    """Worst-case number of SerpApi searches a run can use."""

    maps_calls: int
    review_calls: int
    cached_calls: int
    """Calls already answered by the local cache (free). Not included in the two counts above."""
    exact: bool
    """True when the whole run can be replayed from cache, so the counts are not upper bounds."""

    @property
    def total(self) -> int:
        return self.maps_calls + self.review_calls


class RunStats(BaseModel):
    api_calls: int = 0
    cache_hits: int = 0
    retries: int = 0


class RunResult(BaseModel):
    request: SearchRequest
    leads: list[Lead] = Field(default_factory=list)
    generated_at: datetime
    stats: RunStats = Field(default_factory=RunStats)
    duplicates_removed: int = 0
    interrupted: bool = False
    stop_reason: str | None = None
    """Set when the run ended early (Ctrl+C, out of credits...) and results are partial."""
    warnings: list[str] = Field(default_factory=list)
    resolved_locations: dict[str, str] = Field(default_factory=dict)
    """Location as typed -> the place the map was centred on, e.g. "Tampa, Florida, ..."."""

    def tier_counts(self) -> dict[Tier, int]:
        return {tier: sum(1 for lead in self.leads if lead.tier is tier) for tier in Tier}
