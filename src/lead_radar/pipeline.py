"""Search, paginate, dedupe, enrich and score: the whole run, independent of any UI.

Both the CLI and the Streamlit app call :func:`estimate` and :func:`run` and nothing else.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from lead_radar.client import SerpApiGateway
from lead_radar.config import ScoringConfig
from lead_radar.errors import BadRequestError, SerpApiRequestError
from lead_radar.locations import ResolvedLocation, pick, split_query
from lead_radar.models import (
    Business,
    CreditEstimate,
    ReviewSignals,
    RunResult,
    SearchRequest,
)
from lead_radar.reviews import analyse_reviews, build_reviews_params, parse_reviews
from lead_radar.scoring import score_all

PAGE_SIZE = 20
"""Google Maps returns up to 20 places per page; ``start`` advances in steps of 20."""

MAX_START = 100
"""SerpApi recommends not paging past ``start=100`` (page six), like the Maps web app:
beyond it, results "might be duplicated or irrelevant"."""

# "@40.7455096,-74.0083012,14z" or "@43.8521864,11.2168835,10410m"
_LL_PATTERN = re.compile(r"^@-?\d+(\.\d+)?,-?\d+(\.\d+)?,\d+(\.\d+)?[zm]$")

_Fetch = Callable[[dict[str, Any]], dict[str, Any] | None]


class _StopRequested(Exception):
    """Raised inside :func:`run` when ``should_stop()`` returns True."""


@dataclass(frozen=True)
class Progress:
    stage: Literal["maps", "reviews", "scoring"]
    message: str
    current: int = 0
    total: int | None = None


ProgressCallback = Callable[[Progress], None]


def max_pages(max_results: int) -> int:
    """Pages needed for ``max_results`` places, bounded by SerpApi's recommended ceiling."""
    return min(math.ceil(max_results / PAGE_SIZE), MAX_START // PAGE_SIZE + 1)


def build_maps_params(
    query: str,
    location: str,
    *,
    start: int = 0,
    zoom: int = 12,
    hl: str = "en",
    resolved: ResolvedLocation | None = None,
) -> dict[str, Any]:
    """Parameters for ``engine=google_maps`` with ``type=search``.

    The search origin is chosen in this order:

    * explicit coordinates in SerpApi's ``ll`` format ("@27.9506,-82.4572,12z"): sent as is;
    * a place name resolved through the Locations API: sent as ``ll`` built from its GPS
      coordinates and ``zoom`` (see :mod:`lead_radar.locations` for why);
    * otherwise the raw name as ``location`` plus ``z``, left to SerpApi to interpret.

    ``ll`` cannot be combined with ``location`` or ``z``, so those are only sent in the last case.
    """
    params: dict[str, Any] = {"engine": "google_maps", "type": "search", "q": query, "hl": hl}
    location = location.strip()
    if _LL_PATTERN.match(location):
        params["ll"] = location
    elif resolved is not None:
        params["ll"] = resolved.ll(zoom)
    else:
        params["location"] = location
        params["z"] = zoom
    if start:
        params["start"] = start
    return params


@dataclass
class _Collection:
    """Mutable state of a run. Mutated in place so partial progress survives an interruption."""

    businesses: dict[str, Business] = field(default_factory=dict)
    duplicates: int = 0
    missing_pages: int = 0
    warnings: list[str] = field(default_factory=list)


def _collect_location(
    fetch: _Fetch,
    request: SearchRequest,
    location: str,
    config: ScoringConfig,
    state: _Collection,
    on_progress: ProgressCallback | None = None,
    resolved: ResolvedLocation | None = None,
) -> None:
    """Page through one location until results run out or ``max_results`` is reached.

    ``fetch`` returning ``None`` means "not available" (used by :func:`estimate`, which only
    reads the cache); the remaining pages are then counted as missing.
    """
    pages = max_pages(request.max_results)
    location_keys: set[str] = set()

    for page in range(pages):
        start = page * PAGE_SIZE
        if on_progress:
            on_progress(
                Progress("maps", f"{location}: page {page + 1} (start={start})", page, pages)
            )
        params = build_maps_params(
            request.query,
            location,
            start=start,
            zoom=request.zoom,
            hl=request.hl,
            resolved=resolved,
        )
        response = fetch(params)
        if response is None:
            state.missing_pages += pages - page
            return

        raw_results = response.get("local_results")
        if not isinstance(raw_results, list) or not raw_results:
            # An empty page is a successful search (HTTP 200 with an "error" message such as
            # "Google hasn't returned any results for this query."): the end of pagination.
            if page == 0 and "place_results" in response:
                state.warnings.append(
                    f"{location}: Google answered with a single place instead of a list. "
                    "Use a broader niche (e.g. 'roofing contractor', not a business name)."
                )
            return

        new_on_page = 0
        for raw in raw_results:
            if len(location_keys) >= request.max_results:
                break
            if not isinstance(raw, dict) or not raw.get("title"):
                continue
            business = Business.from_local_result(
                raw, query=request.query, location=location, detection=config.detection
            )
            key = business.dedupe_key
            if key in location_keys:
                state.duplicates += 1
                continue
            location_keys.add(key)
            new_on_page += 1
            if key in state.businesses:
                state.duplicates += 1  # already found via another location
            else:
                state.businesses[key] = business

        if len(location_keys) >= request.max_results:
            return
        pagination = response.get("serpapi_pagination")
        if not (isinstance(pagination, dict) and pagination.get("next")):
            return
        if new_on_page == 0:
            return  # Google is repeating itself; stop spending credits.


def resolve_location(gateway: SerpApiGateway, location: str) -> ResolvedLocation | None:
    """Pin a place name to coordinates with the free Locations API; ``None`` if not possible."""
    if _LL_PATTERN.match(location.strip()):
        return None
    name, _ = split_query(location)
    if not name:
        return None
    try:
        return pick(location, gateway.locations(name))
    except SerpApiRequestError:
        return None


def _resolve_all(gateway: SerpApiGateway, request: SearchRequest) -> dict[str, ResolvedLocation]:
    resolved = {}
    for location in request.locations:
        match = resolve_location(gateway, location)
        if match is not None:
            resolved[location] = match
    return resolved


def _review_targets(
    businesses: list[Business], request: SearchRequest, config: ScoringConfig
) -> list[Business]:
    """Businesses to fetch reviews for: all, or the best ``review_limit`` by listing-only score."""
    ranked = [lead.business for lead in score_all(businesses, config)]
    return ranked if request.review_limit is None else ranked[: request.review_limit]


def estimate(
    request: SearchRequest, gateway: SerpApiGateway, config: ScoringConfig
) -> CreditEstimate:
    """Worst-case credits for a run, without calling the API.

    The run is replayed against the local cache. Every page that is cached is free; as soon
    as a page is missing, the rest of that location is charged at its upper bound. If the
    whole Maps phase is cached, the review calls can be counted exactly too.
    """
    state = _Collection()
    cached = 0

    def peek(params: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal cached
        response = gateway.peek(params)
        if response is not None:
            cached += 1
        return response

    resolved = _resolve_all(gateway, request)
    for location in request.locations:
        _collect_location(peek, request, location, config, state, resolved=resolved.get(location))

    exact = state.missing_pages == 0
    review_calls = 0
    if request.with_reviews:
        if exact:
            for business in _review_targets(list(state.businesses.values()), request, config):
                params = build_reviews_params(business, hl=request.hl, num=request.reviews_per_lead)
                if params is not None and peek(params) is None:
                    review_calls += 1
        else:
            review_calls = len(request.locations) * request.max_results
            if request.review_limit is not None:
                review_calls = min(review_calls, request.review_limit)

    return CreditEstimate(
        maps_calls=state.missing_pages,
        review_calls=review_calls,
        cached_calls=cached,
        exact=exact,
    )


def run(
    request: SearchRequest,
    gateway: SerpApiGateway,
    config: ScoringConfig,
    *,
    now: datetime | None = None,
    on_progress: ProgressCallback | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> RunResult:
    """Execute a run and return scored leads.

    Ctrl+C, a ``should_stop()`` request (the web UI's Cancel button), running out of credits
    or a rate limit mid-run do not lose work: whatever was collected so far is scored and
    returned with ``stop_reason`` set. An error before anything was collected (for example an
    invalid key) is raised.
    """
    now = now or datetime.now(UTC)
    state = _Collection()
    signals: dict[str, ReviewSignals] = {}
    interrupted = False
    stop_reason: str | None = None

    def progress(event: Progress) -> None:
        # Called before every API request, so a stop request never waits for more than one.
        if should_stop is not None and should_stop():
            raise _StopRequested
        if on_progress:
            on_progress(event)

    resolved: dict[str, ResolvedLocation] = {}
    try:
        resolved = _resolve_all(gateway, request)
        for location in request.locations:
            if location not in resolved and not _LL_PATTERN.match(location):
                state.warnings.append(
                    f"Could not pin '{location}' to a place with the SerpApi Locations API, so "
                    "Google interpreted the text itself. Results may be off-centre; try "
                    "'City, ST' or coordinates like @27.95,-82.46,12z."
                )
            _collect_location(
                gateway.search,
                request,
                location,
                config,
                state,
                progress,
                resolved=resolved.get(location),
            )

        if request.with_reviews:
            targets = _review_targets(list(state.businesses.values()), request, config)
            for index, business in enumerate(targets):
                progress(Progress("reviews", business.name, index, len(targets)))
                params = build_reviews_params(business, hl=request.hl, num=request.reviews_per_lead)
                if params is None:
                    continue
                try:
                    response = gateway.search(params)
                except BadRequestError as exc:
                    state.warnings.append(f"Reviews unavailable for {business.name}: {exc}")
                    continue
                signals[business.dedupe_key] = analyse_reviews(
                    parse_reviews(response), config.detection, now=now
                )
    except KeyboardInterrupt:
        interrupted = True
        stop_reason = "Interrupted with Ctrl+C. Results below are partial."
    except _StopRequested:
        interrupted = True
        stop_reason = "Cancelled. Results below are partial."
    except SerpApiRequestError as exc:
        if not state.businesses:
            raise
        stop_reason = f"Stopped early, results are partial. {exc}"

    if on_progress:
        on_progress(Progress("scoring", "Scoring leads"))
    leads = score_all(list(state.businesses.values()), config, signals)
    return RunResult(
        request=request,
        leads=leads,
        generated_at=now,
        stats=gateway.stats.model_copy(),
        duplicates_removed=state.duplicates,
        interrupted=interrupted,
        stop_reason=stop_reason,
        warnings=state.warnings,
        resolved_locations={
            text: f"{match.canonical_name.replace(',', ', ')} ({match.target_type.lower()})"
            for text, match in resolved.items()
        },
    )
