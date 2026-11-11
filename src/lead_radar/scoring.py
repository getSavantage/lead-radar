"""Opportunity scoring.

A score is the sum of the points of every rule that fires, capped at 100. Each fired rule
contributes a ``ScoreReason`` with a plain-English message, so the "why" list in the report is
produced by the same code that produces the number and the two can never disagree.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from statistics import median

from lead_radar.config import ScoringConfig
from lead_radar.models import Business, Lead, ReviewSignals, ScoreReason, Tier


@dataclass(frozen=True)
class _Context:
    business: Business
    signals: ReviewSignals | None
    local_median_reviews: float | None
    config: ScoringConfig


_RuleFn = Callable[[_Context], ScoreReason | None]


def _is_social_only(ctx: _Context) -> bool:
    domain = ctx.business.website_domain
    if not domain:
        return False
    return any(
        domain == social or domain.endswith("." + social)
        for social in ctx.config.detection.social_domains
    )


def _no_website(ctx: _Context) -> ScoreReason | None:
    rule = ctx.config.rules.no_website
    if not rule.enabled or ctx.business.website:
        return None
    return ScoreReason(rule="no_website", points=rule.points, message="No website on the listing")


def _social_only_website(ctx: _Context) -> ScoreReason | None:
    rule = ctx.config.rules.social_only_website
    if not rule.enabled or not _is_social_only(ctx):
        return None
    return ScoreReason(
        rule="social_only_website",
        points=rule.points,
        message=f"Only web presence is a {ctx.business.website_domain} page",
    )


def _no_online_booking(ctx: _Context) -> ScoreReason | None:
    rule = ctx.config.rules.no_online_booking
    if not rule.enabled or ctx.business.has_online_booking:
        return None
    return ScoreReason(
        rule="no_online_booking",
        points=rule.points,
        message="No online booking link on the listing",
    )


def _low_review_count(ctx: _Context) -> ScoreReason | None:
    rule = ctx.config.rules.low_review_count
    local_median = ctx.local_median_reviews
    if not rule.enabled or local_median is None or local_median <= 0:
        return None
    count = ctx.business.review_count or 0
    if count >= rule.ratio * local_median:
        return None
    return ScoreReason(
        rule="low_review_count",
        points=rule.points,
        message=(
            f"{count} reviews vs. a local median of {local_median:g} "
            f"in {ctx.business.search_location}"
        ),
    )


def _unclaimed_listing(ctx: _Context) -> ScoreReason | None:
    rule = ctx.config.rules.unclaimed_listing
    if not rule.enabled or not ctx.business.unclaimed_listing:
        return None
    return ScoreReason(
        rule="unclaimed_listing",
        points=rule.points,
        message="Google Business Profile is unclaimed",
    )


def _low_rating(ctx: _Context) -> ScoreReason | None:
    rule = ctx.config.rules.low_rating
    rating = ctx.business.rating
    if not rule.enabled or rating is None or rating >= rule.below:
        return None
    return ScoreReason(
        rule="low_rating", points=rule.points, message=f"Rating {rating:g} is below {rule.below:g}"
    )


def _no_hours_listed(ctx: _Context) -> ScoreReason | None:
    rule = ctx.config.rules.no_hours_listed
    if not rule.enabled or ctx.business.has_hours:
        return None
    return ScoreReason(
        rule="no_hours_listed", points=rule.points, message="No opening hours on the listing"
    )


def _missed_call_complaints(ctx: _Context) -> ScoreReason | None:
    rule = ctx.config.rules.missed_call_complaints
    if not rule.enabled or ctx.signals is None or not ctx.signals.complaints:
        return None
    count = len(ctx.signals.complaints)
    points = min(rule.max_points, count * rule.points_per_complaint)
    example = ctx.signals.complaints[0].matched
    noun = "review mentions" if count == 1 else "reviews mention"
    return ScoreReason(
        rule="missed_call_complaints",
        points=points,
        message=(
            f"{count} recent {noun} unanswered calls "
            f'(e.g. "{example}") in the last {ctx.config.detection.recent_days} days'
        ),
    )


def _unanswered_reviews(ctx: _Context) -> ScoreReason | None:
    rule = ctx.config.rules.unanswered_reviews
    signals = ctx.signals
    if (
        not rule.enabled
        or signals is None
        or signals.owner_response_rate is None
        or signals.recent_reviews < rule.min_reviews
        or signals.owner_response_rate > rule.max_response_rate
    ):
        return None
    return ScoreReason(
        rule="unanswered_reviews",
        points=rule.points,
        message=(
            f"Owner replied to {signals.owner_response_rate:.0%} "
            f"of the last {signals.recent_reviews} recent reviews"
        ),
    )


def _rating_dropping(ctx: _Context) -> ScoreReason | None:
    rule = ctx.config.rules.rating_dropping
    signals = ctx.signals
    overall = ctx.business.rating
    if (
        not rule.enabled
        or signals is None
        or overall is None
        or signals.recent_avg_rating is None
        or signals.recent_reviews < rule.min_reviews
    ):
        return None
    drop = overall - signals.recent_avg_rating
    if drop < rule.min_drop:
        return None
    return ScoreReason(
        rule="rating_dropping",
        points=rule.points,
        message=(
            f"Recent reviews average {signals.recent_avg_rating:g} "
            f"vs. {overall:g} overall (rating is dropping)"
        ),
    )


# Order here is the order of the "why" list before sorting by points.
_RULES: tuple[_RuleFn, ...] = (
    _no_website,
    _social_only_website,
    _no_online_booking,
    _low_review_count,
    _unclaimed_listing,
    _low_rating,
    _no_hours_listed,
    _missed_call_complaints,
    _unanswered_reviews,
    _rating_dropping,
)


def local_medians(businesses: list[Business], config: ScoringConfig) -> dict[str, float]:
    """Median review count per search location, for locations with enough businesses."""
    by_location: dict[str, list[int]] = defaultdict(list)
    for business in businesses:
        by_location[business.search_location].append(business.review_count or 0)
    min_peers = config.rules.low_review_count.min_peers
    return {
        location: float(median(counts))
        for location, counts in by_location.items()
        if len(counts) >= min_peers
    }


def tier_for(score: int, config: ScoringConfig) -> Tier:
    if score >= config.tiers.hot:
        return Tier.HOT
    if score >= config.tiers.warm:
        return Tier.WARM
    return Tier.COLD


def score_business(
    business: Business,
    config: ScoringConfig,
    *,
    signals: ReviewSignals | None = None,
    local_median_reviews: float | None = None,
) -> Lead:
    ctx = _Context(business, signals, local_median_reviews, config)
    reasons = [reason for rule in _RULES if (reason := rule(ctx)) is not None and reason.points > 0]
    reasons.sort(key=lambda reason: reason.points, reverse=True)
    score = min(100, sum(reason.points for reason in reasons))
    return Lead(
        business=business,
        score=score,
        tier=tier_for(score, config),
        reasons=reasons,
        review_signals=signals,
    )


def score_all(
    businesses: list[Business],
    config: ScoringConfig,
    signals: Mapping[str, ReviewSignals] | None = None,
) -> list[Lead]:
    """Score every business and return leads best-first.

    Ties are broken by review count (more reviews = more inbound calls to miss), then name,
    so the order is deterministic.
    """
    signals = signals or {}
    medians = local_medians(businesses, config)
    leads = [
        score_business(
            business,
            config,
            signals=signals.get(business.dedupe_key),
            local_median_reviews=medians.get(business.search_location),
        )
        for business in businesses
    ]
    leads.sort(
        key=lambda lead: (-lead.score, -(lead.business.review_count or 0), lead.business.name)
    )
    return leads
