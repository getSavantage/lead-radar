from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lead_radar.config import ScoringConfig, default_config_text, load_config
from lead_radar.errors import ConfigError
from lead_radar.models import Business, Complaint, ReviewSignals, Tier
from lead_radar.scoring import local_medians, score_all, score_business
from tests.conftest import business


def rules_fired(
    target: Business,
    *,
    signals: ReviewSignals | None = None,
    local_median_reviews: float | None = None,
) -> dict[str, int]:
    lead = score_business(
        target, ScoringConfig(), signals=signals, local_median_reviews=local_median_reviews
    )
    return {reason.rule: reason.points for reason in lead.reasons}


def complaints(count: int) -> list[Complaint]:
    return [Complaint(matched="voicemail", excerpt="went to voicemail")] * count


# --- individual rules --------------------------------------------------------------------


def test_healthy_listing_scores_zero(config: ScoringConfig) -> None:
    lead = score_business(business(), config, local_median_reviews=150)
    assert (lead.score, lead.tier, lead.reasons) == (0, Tier.COLD, [])


def test_no_website() -> None:
    assert rules_fired(business(website=None)) == {"no_website": 20}


@pytest.mark.parametrize(
    "url",
    ["https://www.facebook.com/acmeroofing", "https://m.facebook.com/acme", "https://linktr.ee/a"],
)
def test_social_profile_is_not_a_real_website(url: str) -> None:
    assert rules_fired(business(website=url)) == {"social_only_website": 12}


def test_lookalike_domain_is_a_real_website() -> None:
    assert rules_fired(business(website="https://notfacebook.com/")) == {}


def test_no_online_booking() -> None:
    assert rules_fired(business(booking_signals=[])) == {"no_online_booking": 10}
    with_link = business(booking_signals=[], booking_link="https://book.example/acme")
    assert rules_fired(with_link) == {}


def test_low_review_count_is_relative_to_local_median() -> None:
    few = business(review_count=40)
    assert rules_fired(few, local_median_reviews=100) == {"low_review_count": 15}
    assert rules_fired(few, local_median_reviews=80) == {}, "40 is exactly half: not below"
    assert rules_fired(few, local_median_reviews=None) == {}, "no median, no judgement"


def test_missing_review_count_counts_as_zero() -> None:
    fired = rules_fired(business(review_count=None, rating=None), local_median_reviews=50)
    assert fired == {"low_review_count": 15}


def test_unclaimed_low_rating_and_no_hours() -> None:
    assert rules_fired(business(unclaimed_listing=True)) == {"unclaimed_listing": 10}
    assert rules_fired(business(rating=3.9)) == {"low_rating": 5}
    assert rules_fired(business(rating=4.0)) == {}
    assert rules_fired(business(hours=None)) == {"no_hours_listed": 5}


def test_missed_call_complaints_scale_then_cap() -> None:
    one = ReviewSignals(recent_reviews=3, complaints=complaints(1))
    many = ReviewSignals(recent_reviews=9, complaints=complaints(5))
    assert rules_fired(business(), signals=one) == {"missed_call_complaints": 10}
    assert rules_fired(business(), signals=many) == {"missed_call_complaints": 20}


def test_unanswered_reviews_needs_enough_recent_reviews() -> None:
    silent = ReviewSignals(recent_reviews=8, owner_response_rate=0.1, recent_avg_rating=4.8)
    too_few = ReviewSignals(recent_reviews=3, owner_response_rate=0.0, recent_avg_rating=4.8)
    engaged = ReviewSignals(recent_reviews=8, owner_response_rate=0.9, recent_avg_rating=4.8)
    assert rules_fired(business(), signals=silent) == {"unanswered_reviews": 10}
    assert rules_fired(business(), signals=too_few) == {}
    assert rules_fired(business(), signals=engaged) == {}


def test_rating_dropping_compares_recent_average_to_overall() -> None:
    dropping = ReviewSignals(recent_reviews=8, owner_response_rate=1.0, recent_avg_rating=4.2)
    steady = ReviewSignals(recent_reviews=8, owner_response_rate=1.0, recent_avg_rating=4.6)
    assert rules_fired(business(rating=4.8), signals=dropping) == {"rating_dropping": 5}
    assert rules_fired(business(rating=4.8), signals=steady) == {}


def test_review_rules_stay_silent_without_review_data() -> None:
    worst = business(website=None, booking_signals=[], hours=None, rating=3.0)
    assert "missed_call_complaints" not in rules_fired(worst)


# --- aggregate behaviour -----------------------------------------------------------------


def test_reasons_explain_the_whole_score_best_first(config: ScoringConfig) -> None:
    target = business(website=None, booking_signals=[], unclaimed_listing=True, review_count=3)
    lead = score_business(target, config, local_median_reviews=120)

    assert lead.score == sum(reason.points for reason in lead.reasons) == 55
    assert [reason.points for reason in lead.reasons] == [20, 15, 10, 10]
    assert lead.tier is Tier.HOT
    assert lead.why[0] == "+20 No website on the listing"
    assert "3 reviews vs. a local median of 120 in Tampa, FL" in lead.why[1]


def test_default_weights_sum_to_exactly_100(config: ScoringConfig) -> None:
    worst = business(
        website=None, booking_signals=[], unclaimed_listing=True, review_count=1,
        rating=3.0, hours=None,
    )  # fmt: skip
    signals = ReviewSignals(
        recent_reviews=10, owner_response_rate=0.0, recent_avg_rating=1.5, complaints=complaints(4)
    )
    assert score_business(worst, config, signals=signals, local_median_reviews=100).score == 100


def test_score_is_capped_at_100() -> None:
    config = ScoringConfig.model_validate(
        {"rules": {"no_website": {"points": 90}, "no_online_booking": {"points": 90}}}
    )
    assert score_business(business(website=None, booking_signals=[]), config).score == 100


def test_tier_thresholds(config: ScoringConfig) -> None:
    assert score_business(business(website=None), config).tier is Tier.COLD  # 20
    assert score_business(business(website=None, booking_signals=[]), config).tier is Tier.WARM


def test_local_median_is_per_location_and_needs_enough_peers(config: ScoringConfig) -> None:
    tampa = [
        business(place_id=f"t{i}", review_count=count)
        for i, count in enumerate([10, 20, 30, 40, 500])
    ]
    miami = [business(place_id="m1", review_count=5, search_location="Miami, FL")]
    assert local_medians(tampa + miami, config) == {"Tampa, FL": 30.0}


def test_score_all_sorts_by_score_then_review_count(config: ScoringConfig) -> None:
    leads = score_all(
        [
            business(name="Healthy", place_id="a"),
            business(name="No site, small", place_id="b", website=None, review_count=150),
            business(name="No site, big", place_id="c", website=None, review_count=300),
        ],
        config,
    )
    assert [lead.business.name for lead in leads] == ["No site, big", "No site, small", "Healthy"]


def test_score_all_attaches_signals_by_dedupe_key(config: ScoringConfig) -> None:
    signals = {"place-acme": ReviewSignals(recent_reviews=4, complaints=complaints(2))}
    (lead,) = score_all([business()], config, signals)
    assert lead.score == 20 and lead.review_signals is signals["place-acme"]


# --- configuration -----------------------------------------------------------------------


def test_shipped_yaml_matches_code_defaults() -> None:
    assert ScoringConfig.model_validate(yaml.safe_load(default_config_text())) == ScoringConfig()


def test_partial_yaml_overrides_only_what_it_names(tmp_path: Path) -> None:
    path = tmp_path / "scoring.yaml"
    path.write_text(
        "rules:\n  no_website: {points: 40}\n  no_online_booking: {enabled: false, points: 10}\n"
        "tiers: {hot: 40}\n",
        "utf-8",
    )
    config = load_config(path)
    lead = score_business(business(website=None, booking_signals=[]), config)

    assert (lead.score, lead.tier) == (40, Tier.HOT)
    assert config.rules.low_review_count.points == 15  # untouched default


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("rules:\n  no_websiet: {points: 5}\n", "rules.no_websiet"),
        ("rules:\n  no_website: {points: 500}\n", "rules.no_website.points"),
        ("detection:\n  complaint_phrases: ['(unclosed']\n", "invalid regex"),
        ("- just\n- a list\n", "mapping"),
        ("rules: [unbalanced\n", "not valid YAML"),
    ],
)
def test_invalid_config_fails_with_a_pointed_message(
    tmp_path: Path, text: str, message: str
) -> None:
    path = tmp_path / "scoring.yaml"
    path.write_text(text, "utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")
