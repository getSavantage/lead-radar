from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lead_radar import pipeline
from lead_radar.client import SerpApiGateway
from lead_radar.config import ScoringConfig
from lead_radar.demo import DEMO_NOW, DEMO_REQUEST, FixtureTransport
from lead_radar.export import (
    CSV_COLUMNS,
    csv_safe,
    output_basename,
    render_csv,
    render_html,
    write_reports,
)
from lead_radar.models import Business, RunResult, SearchRequest
from lead_radar.scoring import score_all
from tests.conftest import load_fixture


@pytest.fixture(scope="module")
def demo_result() -> RunResult:
    return pipeline.run(
        DEMO_REQUEST, SerpApiGateway(FixtureTransport()), ScoringConfig(), now=DEMO_NOW
    )


@pytest.fixture(scope="module")
def hostile_result() -> RunResult:
    """Built from the fixture whose third business has a formula/HTML-injection title."""
    request = SearchRequest(query="hvac <contractor>", locations=["Austin, TX"])
    businesses = [
        Business.from_local_result(raw, query=request.query, location="Austin, TX")
        for raw in load_fixture("maps_docs_example.json")["local_results"]
    ]
    return RunResult(
        request=request,
        leads=score_all(businesses, ScoringConfig()),
        generated_at=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
    )


def rows(result: RunResult) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(render_csv(result))))


# --- CSV ---------------------------------------------------------------------------------


def test_csv_has_one_row_per_lead_in_score_order(demo_result: RunResult) -> None:
    parsed = rows(demo_result)
    assert list(parsed[0]) == CSV_COLUMNS
    assert len(parsed) == len(demo_result.leads) == 38
    assert [int(row["score"]) for row in parsed] == [lead.score for lead in demo_result.leads]


def test_csv_row_content(demo_result: RunResult) -> None:
    top, lead = rows(demo_result)[0], demo_result.leads[0]
    assert top["name"] == lead.business.name
    assert top["tier"] == "hot"
    assert top["phone"] == lead.business.phone
    assert top["why"].split(" | ")[0] == "No website on the listing (+20)"
    assert top["why"].count(" | ") == len(lead.reasons) - 1
    assert top["maps_url"].startswith("https://www.google.com/maps/search/?api=1&query=")
    assert top["place_id"] == lead.business.place_id
    assert top["missed_call_complaints"] == str(len(lead.review_signals.complaints))  # type: ignore[union-attr]


def test_csv_leaves_review_columns_blank_without_review_data(hostile_result: RunResult) -> None:
    assert {row["missed_call_complaints"] for row in rows(hostile_result)} == {""}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('=HYPERLINK("http://evil.example")', '\'=HYPERLINK("http://evil.example")'),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("+cmd|' /C calc'!A0", "'+cmd|' /C calc'!A0"),
        ("-2+3", "'-2+3"),
        ("+1 813-555-0100", "+1 813-555-0100"),  # phone numbers stay usable
        ("(813) 555-0100", "(813) 555-0100"),
        ("Acme Roofing", "Acme Roofing"),
        (42, 42),
        ("", ""),
    ],
)
def test_csv_formula_injection_is_neutralised(value: object, expected: object) -> None:
    assert csv_safe(value) == expected


def test_csv_escapes_hostile_business_names(hostile_result: RunResult) -> None:
    names = [row["name"] for row in rows(hostile_result)]
    assert any(name.startswith("'=HYPERLINK") for name in names)
    assert not any(name.startswith("=") for name in names)


# --- HTML --------------------------------------------------------------------------------


def test_html_lists_every_lead_with_badge_reasons_and_links(demo_result: RunResult) -> None:
    html = render_html(demo_result)
    assert html.count('<tr data-tier="') == 38
    top = demo_result.leads[0]
    assert top.business.name in html
    assert f'<span class="badge hot"><b>{top.score}</b>' in html
    assert top.reasons[0].message in html
    assert 'href="https://www.google.com/maps/search/?api=1&amp;query=' in html
    assert 'href="tel:' in html
    assert "roofing contractor in Tampa, FL · St. Petersburg, FL" in html


def test_html_is_self_contained(demo_result: RunResult) -> None:
    html = render_html(demo_result)
    assert not re.search(r"<(script|link|img|iframe)[^>]+(src|href)=", html), "no external assets"
    assert "<style>" in html and "<script>" in html
    assert "@import" not in html and "url(" not in html


def test_html_is_sortable_and_filterable(demo_result: RunResult) -> None:
    html = render_html(demo_result)
    assert html.count('data-sort="num"') == 3 and 'data-sort="text"' in html
    assert 'aria-sort="descending"' in html
    assert 'id="q"' in html and 'data-tier="hot" aria-pressed="false"' in html


def test_html_escapes_untrusted_text(hostile_result: RunResult) -> None:
    html = render_html(hostile_result)
    assert "<b>Stand</b>" not in html
    assert "Unclaimed &lt;b&gt;Stand&lt;/b&gt;" in html
    assert "hvac &lt;contractor&gt;" in html
    assert "javascript:" not in html
    assert "No website" in html


def test_html_flags_partial_runs_and_missing_review_data(hostile_result: RunResult) -> None:
    partial = hostile_result.model_copy(
        update={
            "stop_reason": "Interrupted with Ctrl+C.",
            "warnings": ["Reviews unavailable for X"],
        }
    )
    html = render_html(partial)
    assert "Partial results." in html and "Interrupted with Ctrl+C." in html
    assert "Reviews unavailable for X" in html
    assert "Re-run with <code>--reviews</code>" in html


def test_html_for_empty_result() -> None:
    empty = RunResult(
        request=SearchRequest(query="unicorn groomer", locations=["Nowhere"]),
        generated_at=DEMO_NOW,
    )
    html = render_html(empty)
    assert "No businesses were found" in html and "<table" not in html


# --- files -------------------------------------------------------------------------------


def test_write_reports_creates_both_files(demo_result: RunResult, tmp_path: Path) -> None:
    csv_path, html_path = write_reports(demo_result, tmp_path / "nested" / "out")

    assert csv_path.name == "leads-roofing-contractor-tampa-fl-and-1-more-20261101-1200.csv"
    assert html_path.with_suffix("").name == output_basename(demo_result)
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf"), "BOM so Excel reads UTF-8"
    assert html_path.read_text("utf-8").startswith("<!doctype html>")


def test_committed_example_is_up_to_date(demo_result: RunResult) -> None:
    """examples/ is generated by `lead-radar demo --out examples`; fail if it drifts."""
    examples = Path(__file__).parent.parent / "examples"
    assert (examples / "demo-report.html").read_text("utf-8") == render_html(demo_result)
    assert (examples / "demo-report.csv").read_text("utf-8-sig") == render_csv(demo_result)
