from __future__ import annotations

from pathlib import Path

import pytest

from lead_radar import pipeline
from lead_radar.client import SerpApiGateway
from lead_radar.config import ScoringConfig
from lead_radar.demo import DEMO_NOW, DEMO_REQUEST, FixtureTransport
from lead_radar.export import render_csv
from lead_radar.models import RunResult, Tier
from lead_radar.store import RunStore


@pytest.fixture(scope="module")
def result() -> RunResult:
    return pipeline.run(
        DEMO_REQUEST, SerpApiGateway(FixtureTransport()), ScoringConfig(), now=DEMO_NOW
    )


def test_save_writes_reports_and_a_summary(tmp_path: Path, result: RunResult) -> None:
    store = RunStore(tmp_path)
    summary = store.save(result, demo=True)

    assert RunStore.is_valid_id(summary.id)
    assert (summary.leads, summary.hot, summary.api_calls) == (
        38,
        result.tier_counts()[Tier.HOT],
        41,
    )
    assert summary.demo and summary.with_reviews and not summary.partial
    files = sorted(path.name for path in (tmp_path / summary.id).iterdir())
    assert files == sorted(
        ["run.json", "summary.json", f"{summary.basename}.csv", f"{summary.basename}.html"]
    )


def test_load_round_trips_the_full_result(tmp_path: Path, result: RunResult) -> None:
    store = RunStore(tmp_path)
    loaded = store.load(store.save(result).id)
    assert loaded == result
    assert render_csv(loaded) == render_csv(result), "a saved run re-renders identically"


def test_list_is_newest_first_and_skips_junk(tmp_path: Path, result: RunResult) -> None:
    store = RunStore(tmp_path)
    first = store.save(result)
    second = store.save(result.model_copy(update={"stop_reason": "Cancelled."}))
    (tmp_path / "not-a-run").mkdir()
    (tmp_path / "20990101-000000-abcdef").mkdir()  # valid name, no summary

    assert [summary.id for summary in store.list()] == sorted([first.id, second.id], reverse=True)
    assert store.summary(second.id).partial


def test_report_paths_and_delete(tmp_path: Path, result: RunResult) -> None:
    store = RunStore(tmp_path)
    run_id = store.save(result).id
    assert store.report_path(run_id, "csv").suffix == ".csv"
    assert store.report_path(run_id, "html").read_text("utf-8").startswith("<!doctype html>")

    store.delete(run_id)
    assert store.list() == []
    with pytest.raises(KeyError):
        store.summary(run_id)


@pytest.mark.parametrize("bad", ["../../etc", "..", "20261101-120000-abcdeg", "", "x/../y"])
def test_ids_from_urls_never_reach_the_filesystem(tmp_path: Path, bad: str) -> None:
    store = RunStore(tmp_path)
    for call in (store.summary, store.load, store.delete):
        with pytest.raises(KeyError):
            call(bad)


def test_empty_store(tmp_path: Path) -> None:
    assert RunStore(tmp_path / "missing").list() == []
