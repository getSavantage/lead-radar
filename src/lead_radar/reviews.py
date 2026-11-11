"""Turn a page of Google Maps reviews into outreach signals."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from lead_radar.config import Detection
from lead_radar.models import Business, Complaint, Review, ReviewSignals

_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'"})
_EXCERPT_RADIUS = 70


def build_reviews_params(business: Business, *, hl: str, num: int) -> dict[str, Any] | None:
    """Parameters for ``engine=google_maps_reviews``, newest first.

    The engine accepts either ``data_id`` or ``place_id``; ``None`` means the listing has
    neither and cannot be looked up.
    """
    params: dict[str, Any] = {
        "engine": "google_maps_reviews",
        "hl": hl,
        "sort_by": "newestFirst",
        "num": num,
    }
    if business.data_id:
        params["data_id"] = business.data_id
    elif business.place_id:
        params["place_id"] = business.place_id
    else:
        return None
    return params


def parse_reviews(response: dict[str, Any]) -> list[Review]:
    raw_reviews = response.get("reviews")
    if not isinstance(raw_reviews, list):
        return []
    return [Review.from_raw(raw) for raw in raw_reviews if isinstance(raw, dict)]


def _excerpt(text: str, start: int, end: int) -> str:
    left = max(0, start - _EXCERPT_RADIUS)
    right = min(len(text), end + _EXCERPT_RADIUS)
    snippet = " ".join(text[left:right].split())
    return ("…" if left > 0 else "") + snippet + ("…" if right < len(text) else "")


def analyse_reviews(reviews: list[Review], detection: Detection, *, now: datetime) -> ReviewSignals:
    """Count recent missed-call complaints, owner response rate and recent average rating.

    Reviews without an ``iso_date`` cannot be placed in time, so they are not treated as recent.
    """
    cutoff = now - timedelta(days=detection.recent_days)
    patterns = [re.compile(phrase, re.IGNORECASE) for phrase in detection.complaint_phrases]

    recent = [r for r in reviews if r.iso_date is not None and _as_utc(r.iso_date) >= cutoff]
    complaints: list[Complaint] = []
    for review in recent:
        if review.rating is None or review.rating > detection.complaint_max_rating:
            continue
        text = review.text.translate(_APOSTROPHES)
        matches = [m for p in patterns if (m := p.search(text))]
        if matches:
            match = min(matches, key=lambda m: m.start())  # quote the first mention
            complaints.append(
                Complaint(
                    date=review.date,
                    rating=review.rating,
                    matched=match.group(0).lower(),
                    excerpt=_excerpt(text, match.start(), match.end()),
                )
            )

    ratings = [r.rating for r in recent if r.rating is not None]
    return ReviewSignals(
        reviews_fetched=len(reviews),
        recent_reviews=len(recent),
        recent_avg_rating=round(sum(ratings) / len(ratings), 2) if ratings else None,
        owner_response_rate=(
            round(sum(r.has_owner_response for r in recent) / len(recent), 2) if recent else None
        ),
        complaints=complaints,
    )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
