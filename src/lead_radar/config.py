"""Scoring configuration.

Every weight, threshold and phrase the scorer uses lives here, so a score can always be
traced back to a line of YAML. The pydantic defaults below are the single source of truth;
``default_scoring.yaml`` is a commented copy for users to edit (a test keeps the two in sync).
"""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from lead_radar.errors import ConfigError

DEFAULT_CONFIG_RESOURCE = "default_scoring.yaml"


class _Strict(BaseModel):
    """Reject unknown keys so a typo in the YAML fails loudly instead of being ignored."""

    model_config = ConfigDict(extra="forbid")


class Rule(_Strict):
    enabled: bool = True
    points: int = Field(ge=0, le=100)


class LowReviewCountRule(Rule):
    points: int = Field(default=15, ge=0, le=100)
    ratio: float = Field(default=0.5, gt=0, le=1)
    """Fires when a business has fewer than ``ratio`` x the local median review count."""
    min_peers: int = Field(default=5, ge=2)
    """Minimum businesses in the same location needed for the median to mean anything."""


class LowRatingRule(Rule):
    points: int = Field(default=5, ge=0, le=100)
    below: float = Field(default=4.0, ge=1, le=5)


class MissedCallComplaintsRule(_Strict):
    enabled: bool = True
    points_per_complaint: int = Field(default=10, ge=0, le=100)
    max_points: int = Field(default=20, ge=0, le=100)


class UnansweredReviewsRule(Rule):
    points: int = Field(default=10, ge=0, le=100)
    max_response_rate: float = Field(default=0.3, ge=0, le=1)
    min_reviews: int = Field(default=5, ge=1)


class RatingDroppingRule(Rule):
    points: int = Field(default=5, ge=0, le=100)
    min_drop: float = Field(default=0.4, gt=0, le=4)
    min_reviews: int = Field(default=5, ge=1)


class Rules(_Strict):
    no_website: Rule = Rule(points=20)
    social_only_website: Rule = Rule(points=12)
    no_online_booking: Rule = Rule(points=10)
    low_review_count: LowReviewCountRule = LowReviewCountRule()
    unclaimed_listing: Rule = Rule(points=10)
    low_rating: LowRatingRule = LowRatingRule()
    no_hours_listed: Rule = Rule(points=5)
    # The three rules below need review data (run with --reviews).
    missed_call_complaints: MissedCallComplaintsRule = MissedCallComplaintsRule()
    unanswered_reviews: UnansweredReviewsRule = UnansweredReviewsRule()
    rating_dropping: RatingDroppingRule = RatingDroppingRule()


class Tiers(_Strict):
    hot: int = Field(default=50, ge=0, le=100)
    warm: int = Field(default=25, ge=0, le=100)


class Detection(_Strict):
    """How raw SerpApi fields are turned into signals."""

    booking_link_fields: list[str] = ["reserve_a_table", "order_online"]
    """Keys of a ``local_results`` item whose value is a booking/ordering URL.

    These two are the ones documented at https://serpapi.com/maps-local-results. Add a key
    here if SerpApi starts returning another one for your niche.
    """
    booking_keywords: list[str] = ["online appointments", "book online", "online booking"]
    """Matched (case-insensitively) against ``service_options`` keys and ``extensions`` values."""
    social_domains: list[str] = [
        "facebook.com",
        "instagram.com",
        "linktr.ee",
        "yelp.com",
        "nextdoor.com",
        "business.site",
        "sites.google.com",
    ]
    """A "website" on one of these is a profile page, not a site that can take bookings."""
    complaint_phrases: list[str] = [
        r"never (called|phoned|rang)( \w+)? back",
        r"(didn'?t|did not|don'?t|do not|doesn'?t|won'?t|wouldn'?t) (call|phone)( \w+)? back",
        r"no (call ?back|return call)",
        r"(never|didn'?t|did not|don'?t|won'?t) return(ed)? (my|our|any|the) (phone )?calls?",
        r"no (one|body) (answer(s|ed)?|picks? up|picked up)",
        r"nobody (answer(s|ed)?|picks? up|picked up)",
        r"no answer",
        r"(never|don'?t|doesn'?t|didn'?t|won'?t) (answer|pick up)",
        r"voice ?mail",
        r"(couldn'?t|could not|can'?t|cannot|unable to) (reach|get (a ?hold|ahold|through))",
        r"never (heard|got) back",
        r"never (responded|replied|got back to)",
        r"no (response|reply)",
    ]
    """Regexes (case-insensitive) that mark a review as a missed-call complaint."""
    complaint_max_rating: float = Field(default=3.0, ge=1, le=5)
    """Only reviews at or below this rating count, so praise like "they actually answer,
    unlike the others who sent me to voicemail" is not counted against the business."""
    recent_days: int = Field(default=180, ge=1)
    """How far back a review still counts as "recent"."""

    @field_validator("complaint_phrases")
    @classmethod
    def _phrases_compile(cls, value: list[str]) -> list[str]:
        for phrase in value:
            try:
                re.compile(phrase)
            except re.error as exc:
                raise ValueError(f"invalid regex {phrase!r}: {exc}") from exc
        return value


class ScoringConfig(_Strict):
    version: int = 1
    tiers: Tiers = Tiers()
    detection: Detection = Detection()
    rules: Rules = Rules()


def default_config_text() -> str:
    """The commented default YAML shipped with the package."""
    return resources.files("lead_radar").joinpath(DEFAULT_CONFIG_RESOURCE).read_text("utf-8")


def load_config(path: Path | None = None) -> ScoringConfig:
    """Load a scoring config; ``None`` returns the built-in defaults.

    A user file only needs the keys it changes; everything else keeps its default.
    """
    if path is None:
        return ScoringConfig()
    try:
        raw: Any = yaml.safe_load(path.read_text("utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Scoring config not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if raw is None:
        return ScoringConfig()
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level.")
    try:
        return ScoringConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"Invalid scoring config {path}: {problems}") from exc
