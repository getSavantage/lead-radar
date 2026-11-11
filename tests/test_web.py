"""Web API tests. Runs go through the real pipeline with offline transports."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lead_radar.cache import ResponseCache
from lead_radar.client import SerpApiGateway
from lead_radar.demo import FixtureTransport
from lead_radar.errors import InvalidApiKeyError
from lead_radar.store import RunStore
from lead_radar.web.app import create_app
from tests.conftest import ScriptedTransport

SEARCH = {
    "query": "roofing contractor",
    "locations": ["Tampa, FL", "St. Petersburg, FL"],
    "max_results": 40,
}


class Harness:
    def __init__(self, tmp_path: Path, *, has_key: bool = True) -> None:
        self.store = RunStore(tmp_path / "runs")
        self.cache_dir = tmp_path / "cache"
        self.transport: Any = FixtureTransport()
        self.client = TestClient(
            create_app(
                store=self.store,
                cache_dir=self.cache_dir,
                gateway_factory=self.gateway,
                has_key=lambda: has_key,
            )
        )

    def gateway(self, use_cache: bool) -> SerpApiGateway:
        cache = ResponseCache(self.cache_dir) if use_cache else None
        return SerpApiGateway(self.transport, cache=cache, sleep=lambda _: None)

    def wait(self, job_id: str, timeout: float = 10.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job: dict[str, Any] = self.client.get(f"/api/jobs/{job_id}").json()
            if job["status"] != "running":
                return job
            time.sleep(0.02)
        raise AssertionError("job did not finish")

    def run(self, **body: Any) -> dict[str, Any]:
        response = self.client.post("/api/jobs", json=body)
        assert response.status_code == 202, response.text
        return self.wait(response.json()["job_id"])


@pytest.fixture
def web(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_index_and_static_assets_are_served(web: Harness) -> None:
    page = web.client.get("/")
    assert page.status_code == 200 and "<title>Lead Radar</title>" in page.text
    for asset in ("/static/app.js", "/static/styles.css"):
        assert web.client.get(asset).status_code == 200


def test_status_never_exposes_the_key(web: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERPAPI_API_KEY", "sk-should-not-leak")
    status = web.client.get("/api/status").json()
    assert status["has_key"] is True and status["active_job"] is None
    assert "sk-should-not-leak" not in web.client.get("/api/status").text
    assert "SECRET_API_KEY" not in web.client.get("/api/account").text


def test_account(web: Harness) -> None:
    account = web.client.get("/api/account").json()
    assert account["total_searches_left"] == 962 and account["plan_name"] == "Demo Plan"


def test_estimate_returns_cost_and_balance_without_spending(web: Harness) -> None:
    data = web.client.post("/api/estimate", json=SEARCH).json()
    assert data["estimate"]["total"] == 4 and data["estimate"]["exact"] is False
    assert data["account"]["total_searches_left"] == 962
    assert web.transport.calls == []


def test_estimate_validates_input(web: Harness) -> None:
    response = web.client.post("/api/estimate", json={"query": "plumber", "locations": []})
    assert response.status_code == 422
    assert response.json()["detail"].startswith("Locations:")


def test_invalid_key_is_a_401_with_a_clear_message(web: Harness) -> None:
    web.transport = ScriptedTransport()
    web.transport.account_response = InvalidApiKeyError("SerpApi rejected the API key.")
    response = web.client.post("/api/estimate", json=SEARCH)
    assert response.status_code == 401 and "rejected the API key" in response.json()["detail"]


def test_run_requires_a_confirmed_cost(web: Harness) -> None:
    unconfirmed = web.client.post("/api/jobs", json=SEARCH)
    too_low = web.client.post("/api/jobs", json={**SEARCH, "max_searches": 3})
    assert unconfirmed.status_code == too_low.status_code == 409
    assert "Estimate it again" in too_low.json()["detail"]
    assert web.transport.calls == []


def test_full_flow_run_list_view_download_delete(web: Harness) -> None:
    job = web.run(**SEARCH, max_searches=4)
    assert job["status"] == "done", job
    run_id = job["run_id"]

    runs = web.client.get("/api/runs").json()
    assert [run["id"] for run in runs] == [run_id]
    assert runs[0]["leads"] == 38 and runs[0]["api_calls"] == 3

    detail = web.client.get(f"/api/runs/{run_id}").json()
    assert detail["duplicates_removed"] == 3 and detail["stop_reason"] is None

    view = web.client.get(f"/api/runs/{run_id}/view")
    assert view.status_code == 200 and 'class="embedded"' in view.text
    assert "<h1>" not in view.text

    csv = web.client.get(f"/api/runs/{run_id}/report.csv")
    assert csv.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv.headers["content-disposition"]
    assert "leads-roofing-contractor-tampa-fl-and-1-more" in csv.headers["content-disposition"]
    assert csv.content.decode("utf-8-sig").startswith("score,tier,name")

    html = web.client.get(f"/api/runs/{run_id}/report.html")
    assert "inline" in html.headers["content-disposition"] and "<h1>" in html.text
    attachment = web.client.get(f"/api/runs/{run_id}/report.html?download=1")
    assert "attachment" in attachment.headers["content-disposition"]

    assert web.client.delete(f"/api/runs/{run_id}").status_code == 204
    assert web.client.get("/api/runs").json() == []
    assert web.client.get(f"/api/runs/{run_id}").status_code == 404


def test_cached_rerun_is_free_and_needs_no_confirmation(web: Harness) -> None:
    web.run(**SEARCH, max_searches=4)
    web.transport = ScriptedTransport()  # any API call would raise

    assert web.client.post("/api/estimate", json=SEARCH).json()["estimate"]["total"] == 0
    job = web.run(**SEARCH)
    assert job["status"] == "done"
    assert web.client.get(f"/api/runs/{job['run_id']}").json()["api_calls"] == 0


def test_demo_runs_without_a_key(tmp_path: Path) -> None:
    web = Harness(tmp_path, has_key=False)
    assert web.client.get("/api/status").json()["has_key"] is False

    job = web.run(demo=True)
    run = web.client.get(f"/api/runs/{job['run_id']}").json()
    assert run["demo"] is True and run["leads"] == 38 and run["with_reviews"] is True


def test_failed_run_reports_the_error(web: Harness) -> None:
    web.transport = ScriptedTransport(InvalidApiKeyError("SerpApi rejected the API key."))
    job = web.run(**SEARCH, max_searches=4, use_cache=False)
    assert job["status"] == "failed" and "rejected the API key" in job["error"]
    assert web.client.get("/api/runs").json() == []


def test_cancel_keeps_partial_results(web: Harness) -> None:
    class SlowTransport(FixtureTransport):
        def search(self, params: dict[str, Any]) -> dict[str, Any]:
            time.sleep(0.3)
            return super().search(params)

    web.transport = SlowTransport()
    job_id = web.client.post("/api/jobs", json={**SEARCH, "max_searches": 4}).json()["job_id"]
    time.sleep(0.05)
    assert web.client.post(f"/api/jobs/{job_id}/cancel").json()["cancelling"] is True

    job = web.wait(job_id)
    assert job["status"] == "done"
    run = web.client.get(f"/api/runs/{job['run_id']}").json()
    assert run["partial"] and run["stop_reason"].startswith("Cancelled")
    assert run["leads"] == 20, "the page in flight finishes; nothing after it is requested"


def test_only_one_run_at_a_time(web: Harness) -> None:
    class SlowTransport(FixtureTransport):
        def search(self, params: dict[str, Any]) -> dict[str, Any]:
            time.sleep(0.2)
            return super().search(params)

    web.transport = SlowTransport()
    first = web.client.post("/api/jobs", json={**SEARCH, "max_searches": 4}).json()["job_id"]
    second = web.client.post("/api/jobs", json={"demo": True})
    assert second.status_code == 409
    assert web.client.get("/api/status").json()["active_job"] == first
    web.wait(first)


@pytest.mark.parametrize(
    "path",
    [
        "/api/runs/nope",
        "/api/runs/..%2F..%2Fetc/view",
        "/api/runs/20261101-120000-abcdef/report.csv",
        "/api/jobs/nope",
    ],
)
def test_unknown_ids_are_404(web: Harness, path: str) -> None:
    assert web.client.get(path).status_code == 404


def test_unknown_report_format_is_404(web: Harness) -> None:
    run_id = web.run(demo=True)["run_id"]
    assert web.client.get(f"/api/runs/{run_id}/report.json").status_code == 404
