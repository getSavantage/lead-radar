from __future__ import annotations

from lead_radar.config import Detection
from lead_radar.models import Business, Review, detect_booking
from tests.conftest import load_fixture


def parsed() -> list[Business]:
    page = load_fixture("maps_docs_example.json")
    return [
        Business.from_local_result(raw, query="hvac contractor", location="Austin, TX")
        for raw in page["local_results"]
    ]


def test_maps_documented_fields_onto_the_model() -> None:
    hvac = parsed()[0]
    assert hvac.name == "Example Industrial Refrigeration"
    assert hvac.category == "HVAC contractor"
    assert hvac.place_id == "ChIJkR0uHy7NRIYRal2P7SdVYPY"
    assert hvac.data_id == "0x8644cd2e1f2e1d91:0xf6605527ed8f5d6a"
    assert hvac.phone == "+1 512-555-0134"
    assert hvac.website == "http://www.example-industrial.example/"
    assert hvac.website_domain == "example-industrial.example"
    assert (hvac.rating, hvac.review_count) == (4.0, 4)
    assert hvac.operating_hours["saturday"] == "Closed"
    assert (hvac.latitude, hvac.longitude) == (30.4447577, -97.77864339999999)
    assert hvac.search_location == "Austin, TX"
    assert not hvac.unclaimed_listing


def test_booking_detected_from_service_options_and_extensions() -> None:
    hvac = parsed()[0]
    assert hvac.booking_link is None
    assert hvac.booking_signals == ["online_appointments", "Online appointments"]
    assert hvac.has_online_booking


def test_booking_detected_from_documented_link_fields() -> None:
    pizza = parsed()[1]
    assert pizza.booking_link is not None
    assert pizza.booking_link.startswith("https://www.google.com/maps/reserve/")
    assert pizza.booking_signals == ["reserve_a_table", "order_online"]


def test_booking_link_fields_are_configurable() -> None:
    raw = {"title": "X", "schedule_link": "https://book.example/x"}
    assert detect_booking(raw, Detection()) == (None, [])
    custom = Detection(booking_link_fields=["schedule_link"])
    assert detect_booking(raw, custom) == ("https://book.example/x", ["schedule_link"])


def test_undocumented_book_online_field_seen_in_real_responses_counts() -> None:
    raw = {"title": "X", "book_online": "https://book.servicetitan.com/abc?rwg_token=t"}
    assert detect_booking(raw, Detection()) == (raw["book_online"], ["book_online"])


def test_tolerates_sparse_and_messy_results() -> None:
    pizza, stand = parsed()[1], parsed()[2]
    assert pizza.data_cid == "4127536899324797711"  # integer in the payload
    assert pizza.website == "https://www.example-pizza.example/"  # protocol-relative URL
    assert stand.rating is None and stand.review_count is None and stand.phone is None
    assert stand.unclaimed_listing
    assert not stand.has_hours


def test_non_http_urls_never_become_links() -> None:
    stand = parsed()[2]
    assert stand.website is None  # was "javascript:alert(1)"


def test_dedupe_key_prefers_place_id_then_falls_back() -> None:
    assert Business(name="A", place_id="p", data_id="d").dedupe_key == "p"
    assert Business(name="A", data_id="d", data_cid="c").dedupe_key == "d"
    assert Business(name="A", data_cid="c").dedupe_key == "c"
    assert Business(name="Acme", address="1 Main St").dedupe_key == "acme|1 main st"


def test_maps_url_carries_the_place_id() -> None:
    url = Business(name="Acme Roofing & Co", place_id="ChIJ123").maps_url
    assert url == (
        "https://www.google.com/maps/search/?api=1&query=Acme+Roofing+%26+Co&query_place_id=ChIJ123"
    )


def test_review_prefers_full_text_and_detects_owner_response() -> None:
    raw = load_fixture("reviews_docs_example.json")["reviews"]
    first, second = Review.from_raw(raw[0]), Review.from_raw(raw[1])
    assert first.text.endswith("they never called me back.")  # snippet alone is truncated
    assert not first.has_owner_response
    assert second.has_owner_response
    assert first.iso_date is not None and first.iso_date.year == 2026
