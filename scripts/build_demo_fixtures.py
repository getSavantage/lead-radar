"""Generate the synthetic SerpApi fixtures in ``src/lead_radar/demo_data/``.

The businesses, phone numbers (555-01xx), websites (.example) and reviews are fictional. The
*shape* of every file follows the documented response format:

* https://serpapi.com/maps-local-results       (``local_results``, ``serpapi_pagination``)
* https://serpapi.com/google-maps-reviews-api  (``place_info``, ``reviews``)
* https://serpapi.com/account-api
* https://serpapi.com/api-status-and-error-codes (empty-results response)

The output is deterministic (seeded), so re-running this script produces no diff:

    python scripts/build_demo_fixtures.py
"""

from __future__ import annotations

import json
import random
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from lead_radar.demo import (
    DEMO_DATA_DIR,
    DEMO_NOW,
    DEMO_REQUEST,
    maps_fixture_name,
    reviews_fixture_name,
    slug,
)
from lead_radar.locations import pick

rng = random.Random(20261101)

FIRST = [
    "Suncoast", "Bayshore", "Gulf Breeze", "Ybor", "Hillsborough", "Palm River", "Egret",
    "Manatee", "Tarpon", "Heron", "Sandhill", "Mangrove", "Pelican", "Osprey", "Cypress",
    "Seagrape", "Driftwood", "Sawgrass", "Coquina", "Skyway", "Pinellas Point", "Gandy",
    "Old Northeast", "Kenwood", "Snell Isle", "Tyrone", "Lealman", "Coral Ridge", "Harborview",
    "Citrus Park", "Westshore", "Seminole Heights", "Davis Island", "Channelside", "Riverview",
    "Bay Pines", "Gulfport", "Pass-a-Grille",
]  # fmt: skip
SECOND = [
    "Roofing", "Roofing & Sheet Metal", "Roof Repair", "Roofing Co.", "Roofers",
    "Roofing Solutions", "Roof Pros", "Exteriors & Roofing", "Family Roofing", "Roofing Group",
]  # fmt: skip
STREETS = [
    "N Dale Mabry Hwy",
    "W Kennedy Blvd",
    "E Hillsborough Ave",
    "S MacDill Ave",
    "Central Ave",
    "4th St N",
    "34th St S",
    "Gandy Blvd",
    "W Waters Ave",
    "N Florida Ave",
]
CITIES = {
    "Tampa, FL": ("Tampa", "FL 336", 27.9506, -82.4572, "813"),
    "St. Petersburg, FL": ("St. Petersburg", "FL 337", 27.7676, -82.6403, "727"),
}

# (profile, website kind, review-count range, rating range)
#   website kind: site | none | social
#   profile: how the owner handles calls and reviews
ARCHETYPES: list[tuple[str, str, tuple[int, int], tuple[float, float]]] = [
    ("responsive", "site", (180, 900), (4.6, 4.9)),
    ("silent", "none", (4, 30), (4.0, 4.8)),
    ("missed_calls", "site", (60, 240), (3.6, 4.3)),
    ("responsive", "site", (90, 400), (4.5, 4.9)),
    ("silent", "social", (10, 60), (4.2, 4.9)),
    ("declining", "site", (120, 350), (4.4, 4.7)),
    ("missed_calls", "none", (15, 70), (3.4, 4.1)),
    ("responsive", "site", (200, 650), (4.7, 5.0)),
    ("silent", "site", (40, 160), (4.3, 4.8)),
    ("missed_calls", "social", (25, 90), (3.8, 4.4)),
]

POSITIVE = [
    "Crew showed up on time and replaced our shingle roof in two days. Clean job site.",
    "Fair quote, no pressure, and the new roof passed inspection first try.",
    "They found the leak around the vent pipe that two other roofers missed.",
    "Great communication from estimate to final walkthrough. Highly recommend.",
    "Handled the insurance paperwork after the storm and kept us updated the whole time.",
    "Quick repair of a few lifted tiles. Reasonable price and friendly crew.",
    "Professional from start to finish. The office staff were lovely to deal with.",
]
NEUTRAL = [
    "Work was fine but it took a week longer than quoted.",
    "Decent repair, a bit pricey for what it was.",
    "Roof looks good. Cleanup could have been better, found nails in the driveway.",
]
MISSED_CALL = [
    "Called three times for an estimate and it went straight to voicemail every time.",
    "Left two messages about a leak and they never called back. Went with someone else.",
    "No answer during business hours. Hard to give them work if nobody picks up.",
    "Good work last year, but this time I couldn't reach anyone to schedule a repair.",
    "Filled in the form and phoned twice. Never heard back from them.",
    "They quoted the job then didn't return my calls for two weeks.",
]
BAD_WORK = [
    "Roof started leaking again after the first heavy rain. Not impressed.",
    "The crew damaged our gutters and left without saying anything.",
    "Price went up twice after we signed. Finish quality was poor.",
]
OWNER_REPLY = "Thank you for taking the time to leave a review. We appreciate your business!"


def fake_id(length: int, alphabet: str) -> str:
    return "".join(rng.choice(alphabet) for _ in range(length))


def make_business(index: int, location: str) -> tuple[dict[str, Any], str]:
    city, zip_prefix, lat, lng, area_code = CITIES[location]
    profile, site, review_range, rating_range = ARCHETYPES[index % len(ARCHETYPES)]
    name = f"{FIRST[index % len(FIRST)]} {SECOND[(index * 3) % len(SECOND)]}"
    data_id = f"0x88c2{fake_id(12, '0123456789abcdef')}:0x{fake_id(16, '0123456789abcdef')}"
    place_id = "ChIJ" + fake_id(
        23, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    )
    latitude = round(lat + rng.uniform(-0.09, 0.09), 7)
    longitude = round(lng + rng.uniform(-0.09, 0.09), 7)
    data_param = quote(f"!4m5!3m4!1s{data_id}!8m2!3d{latitude}!4d{longitude}", safe="")

    result: dict[str, Any] = {
        "position": 0,  # set when the business is placed on a page
        "title": name,
        "place_id": place_id,
        "data_id": data_id,
        "data_cid": str(rng.randrange(10**18, 10**19)),
        "reviews_link": (
            "https://serpapi.com/search.json?"
            f"data_id={quote(data_id, safe='')}&engine=google_maps_reviews&hl=en"
        ),
        "photos_link": (
            "https://serpapi.com/search.json?"
            f"data_id={quote(data_id, safe='')}&engine=google_maps_photos&hl=en"
        ),
        "gps_coordinates": {"latitude": latitude, "longitude": longitude},
        "place_id_search": (
            "https://serpapi.com/search.json?"
            f"data={data_param}&engine=google_maps&google_domain=google.com&hl=en&type=place"
        ),
        "provider_id": "/g/" + fake_id(9, "0123456789abcdefghijklmnopqrstuvwxyz_"),
    }

    brand_new = index == 17  # one listing with no rating and no reviews yet
    if not brand_new:
        result["rating"] = round(rng.uniform(*rating_range), 1)
        result["reviews"] = rng.randint(*review_range)
    result["type"] = "Roofing contractor"
    result["types"] = ["Roofing contractor"] + (["Gutter service"] if index % 4 == 0 else [])
    result["type_id"] = "roofing_contractor"
    result["type_ids"] = ["roofing_contractor"] + (["gutter_service"] if index % 4 == 0 else [])
    result["address"] = (
        f"{rng.randrange(100, 9900)} {rng.choice(STREETS)}, {city}, "
        f"{zip_prefix}{rng.randrange(10, 99)}"
    )

    if index % 9 != 5:  # most listings publish hours
        result["open_state"] = "Open ⋅ Closes 5 PM"
        result["hours"] = "Open ⋅ Closes 5 PM"
        weekday = "7 AM–5 PM" if index % 2 else "8 AM–5 PM"
        result["operating_hours"] = {
            "monday": weekday,
            "tuesday": weekday,
            "wednesday": weekday,
            "thursday": weekday,
            "friday": weekday,
            "saturday": "8 AM–12 PM" if index % 3 == 0 else "Closed",
            "sunday": "Closed",
        }
    result["phone"] = f"({area_code}) 555-01{index:02d}"
    if site == "site":
        result["website"] = f"https://www.{slug(name)}.example/"
    elif site == "social":
        result["website"] = f"https://www.facebook.com/{slug(name).replace('-', '')}"

    options = ["Onsite services"]
    service_options: dict[str, bool] = {"onsite_services": True}
    if profile == "responsive":
        options.append("Online estimates")
        service_options["online_estimates"] = True
    if profile == "responsive" and index % 3 == 0:
        options.append("Online appointments")
        service_options["online_appointments"] = True
    result["extensions"] = [{"service_options": options}]
    result["service_options"] = service_options
    if index % 11 == 6:
        result["unclaimed_listing"] = True
    return result, "none" if brand_new else profile


def make_reviews(business: dict[str, Any], profile: str) -> dict[str, Any]:
    reviews: list[dict[str, Any]] = []
    count = 0 if profile == "none" else rng.randint(9, 16)
    when = DEMO_NOW - timedelta(days=rng.randint(1, 9))
    for position in range(1, count + 1):
        roll = rng.random()
        if profile == "missed_calls" and position <= 6 and roll < 0.5:
            text, rating = rng.choice(MISSED_CALL), rng.choice([1, 1, 2, 3])
        elif profile == "declining" and position <= 7:
            text, rating = rng.choice(BAD_WORK + NEUTRAL), rng.choice([1, 2, 2, 3])
        elif roll > 0.9:
            text, rating = rng.choice(NEUTRAL), 3
        else:
            text, rating = rng.choice(POSITIVE), rng.choice([4, 5, 5, 5])
        review_id = "Ch" + fake_id(
            34, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        )
        iso = when.strftime("%Y-%m-%dT%H:%M:%SZ")
        days_ago = (DEMO_NOW - when).days
        contributor_id = str(rng.randrange(10**20, 10**21))
        review: dict[str, Any] = {
            "position": position,
            "link": f"https://www.google.com/maps/reviews/data=!4m8!14m7!1m6!2m5!1s{review_id}?hl=en",
            "rating": float(rating),
            "date": "a week ago" if days_ago < 14 else f"{max(1, days_ago // 30)} months ago",
            "iso_date": iso,
            "iso_date_of_last_edit": iso,
            "source": "Google",
            "review_id": review_id,
            "user": {
                "name": f"Demo Reviewer {rng.randrange(100, 999)}",
                "link": f"https://www.google.com/maps/contrib/{contributor_id}?hl=en",
                "contributor_id": contributor_id,
                "local_guide": rng.random() < 0.3,
                "reviews": rng.randint(1, 80),
                "photos": rng.randint(0, 40),
            },
            "snippet": text,
            "extracted_snippet": {"original": text},
            "likes": rng.randint(0, 3),
        }
        replies = (profile == "responsive" and rng.random() < 0.85) or (
            profile in {"missed_calls", "declining"} and rng.random() < 0.15
        )
        if replies:
            review["response"] = {
                "date": review["date"],
                "snippet": OWNER_REPLY,
                "extracted_snippet": {"original": OWNER_REPLY},
            }
        reviews.append(review)
        when -= timedelta(days=rng.randint(3, 40), hours=rng.randint(0, 23))

    payload: dict[str, Any] = {
        "search_metadata": {"id": "demo-" + slug(business["title"]), "status": "Success"},
        "search_parameters": {
            "engine": "google_maps_reviews",
            "data_id": business["data_id"],
            "hl": "en",
            "sort_by": "newestFirst",
        },
        "place_info": {
            "title": business["title"],
            "address": business["address"],
            "rating": business.get("rating"),
            "reviews": business.get("reviews", 0),
            "type": business["type"],
        },
        "reviews": reviews,
    }
    if not reviews:
        payload["error"] = "Google hasn't returned any results for this query."
    return payload


def maps_page(
    location: str, start: int, results: list[dict[str, Any]], *, more: bool
) -> dict[str, Any]:
    query = DEMO_REQUEST.query
    page: dict[str, Any] = {
        "search_metadata": {"id": f"demo-{slug(location)}-{start}", "status": "Success"},
        "search_parameters": {"engine": "google_maps", "type": "search", "q": query, "hl": "en"},
        "search_information": {
            "local_results_state": "Results for exact spelling",
            "query_displayed": query,
        },
        "local_results": [dict(r, position=i) for i, r in enumerate(results, start=1)],
    }
    if more:
        page["serpapi_pagination"] = {
            "next": (
                "https://serpapi.com/search.json?engine=google_maps&hl=en"
                f"&location={quote(location)}&q={quote(query)}&start={start + 20}&type=search&z=12"
            )
        }
    return page


# Recorded from https://serpapi.com/locations.json?q=Tampa and ?q=St.%20Petersburg (the
# Locations API is free and needs no key). The DMA Region entry is the one a plain
# `location=Tampa, FL` was matched to, which is why Lead Radar resolves cities itself.
LOCATIONS: list[dict[str, Any]] = [
    {"name": "Tampa", "canonical_name": "Tampa,Florida,United States", "country_code": "US",
     "target_type": "City", "reach": 3980000, "gps": [-82.4571776, 27.950575]},
    {"name": "New Tampa", "canonical_name": "New Tampa,Florida,United States",
     "country_code": "US", "target_type": "Neighborhood", "reach": 408000,
     "gps": [-82.3533659, 28.1437181]},
    {"name": "Tampa International Airport",
     "canonical_name": "Tampa International Airport,Florida,United States", "country_code": "US",
     "target_type": "Airport", "reach": 118000, "gps": [-82.5370781, 27.9834776]},
    {"name": "St. Petersburg", "canonical_name": "St. Petersburg,Florida,United States",
     "country_code": "US", "target_type": "City", "reach": 614000,
     "gps": [-82.6267345, 27.7518284]},
    {"name": "Tampa-St Petersburg (Sarasota), FL",
     "canonical_name": "Tampa-St Petersburg (Sarasota),FL,Florida,United States",
     "country_code": "US", "target_type": "DMA Region", "gps": [-82.8145258, 27.9334641]},
]  # fmt: skip


def maps_key(location: str) -> str:
    """The ``ll`` Lead Radar will send for a demo location, which names its fixture."""
    resolved = pick(location, LOCATIONS)
    assert resolved is not None, location
    return resolved.ll(DEMO_REQUEST.zoom)


def write(name: str, payload: Any) -> None:
    path = DEMO_DATA_DIR / name
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", "utf-8")


def main() -> None:
    DEMO_DATA_DIR.mkdir(parents=True, exist_ok=True)
    for stale in DEMO_DATA_DIR.glob("*.json"):
        stale.unlink()

    tampa, st_pete = DEMO_REQUEST.locations
    tampa_businesses = [make_business(i, tampa) for i in range(29)]
    st_pete_new = [make_business(i, st_pete) for i in range(29, 38)]
    # Three Tampa businesses also rank in St. Petersburg: the cross-location duplicates.
    overlap = [tampa_businesses[i] for i in (2, 6, 21)]
    st_pete_businesses = [st_pete_new[0], overlap[0], *st_pete_new[1:4], overlap[1],
                          *st_pete_new[4:8], overlap[2], st_pete_new[8]]  # fmt: skip

    write(
        maps_fixture_name(maps_key(tampa), 0),
        maps_page(tampa, 0, [b for b, _ in tampa_businesses[:20]], more=True),
    )
    write(
        maps_fixture_name(maps_key(tampa), 20),
        maps_page(tampa, 20, [b for b, _ in tampa_businesses[20:]], more=False),
    )
    write(
        maps_fixture_name(maps_key(st_pete), 0),
        maps_page(st_pete, 0, [b for b, _ in st_pete_businesses], more=False),
    )

    write("locations.json", LOCATIONS)
    for business, profile in [*tampa_businesses, *st_pete_new]:
        write(reviews_fixture_name(business["data_id"]), make_reviews(business, profile))

    write(
        "account.json",
        {
            "account_id": "5ac54d6adefb2f1dba1663f5",
            "api_key": "SECRET_API_KEY",
            "account_email": "demo@serpapi.com",
            "account_status": "Active",
            "plan_id": "demo",
            "plan_name": "Demo Plan",
            "plan_monthly_price": 0.0,
            "plan_renewal_date": None,
            "searches_per_month": 1000,
            "plan_searches_left": 962,
            "extra_credits": 0,
            "total_searches_left": 962,
            "this_month_usage": 38,
            "this_hour_searches": 0,
            "last_hour_searches": 3,
            "account_rate_limit_per_hour": 200,
        },
    )
    print(f"Wrote {len(list(DEMO_DATA_DIR.glob('*.json')))} fixtures to {DEMO_DATA_DIR}")


if __name__ == "__main__":
    main()
