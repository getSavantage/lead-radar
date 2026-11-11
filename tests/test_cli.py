from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from lead_radar import cli
from lead_radar.cache import ResponseCache
from lead_radar.client import SerpApiGateway
from lead_radar.demo import FixtureTransport
from lead_radar.errors import InvalidApiKeyError, OutOfCreditsError
from lead_radar.store import RunStore
from tests.conftest import ScriptedTransport, maps_page

runner = CliRunner()
SECRET = "sk-test-0123456789abcdef"


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every CLI test in an empty directory with a fake key and a wide terminal."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SERPAPI_API_KEY", SECRET)
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setattr(cli.console, "width", 200)


def use_transport(monkeypatch: pytest.MonkeyPatch, transport: Any) -> None:
    """Swap the live transport for an offline one while keeping the real cache wiring."""

    def fake_live_gateway(*, cache: ResponseCache | None, on_retry: Any = None) -> SerpApiGateway:
        return SerpApiGateway(transport, cache=cache, on_retry=on_retry, sleep=lambda _: None)

    monkeypatch.setattr(cli, "live_gateway", fake_live_gateway)


def test_demo_writes_reports_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SERPAPI_API_KEY")
    result = runner.invoke(cli.app, ["demo", "--out", "reports"])

    assert result.exit_code == 0, result.output
    assert "38 businesses" in result.output and "3 duplicates removed" in result.output
    assert Path("reports/demo-report.csv").is_file()
    assert Path("reports/demo-report.html").is_file()


def test_search_without_key_explains_how_to_set_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SERPAPI_API_KEY")
    result = runner.invoke(cli.app, ["search", "roofing contractor", "-l", "Tampa, FL"])

    assert result.exit_code == 1
    assert "SERPAPI_API_KEY is not set" in result.output


def test_search_shows_estimate_and_balance_then_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FixtureTransport()
    use_transport(monkeypatch, transport)

    result = runner.invoke(
        cli.app, ["search", "roofing contractor", "-l", "Tampa, FL", "-n", "40"], input="n\n"
    )

    assert result.exit_code == 0, result.output
    assert "Credit estimate" in result.output
    assert "up to 2" in result.output  # 40 results = 2 pages
    assert "962" in result.output  # balance from the (free) Account API
    assert "Cancelled. No searches were used." in result.output
    assert transport.calls == [], "declining must not spend a single search"


def test_search_yes_runs_and_writes_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    use_transport(monkeypatch, FixtureTransport())
    args = ["search", "roofing contractor", "-l", "Tampa, FL", "-l", "St. Petersburg, FL", "--yes"]

    result = runner.invoke(cli.app, args)

    assert result.exit_code == 0, result.output
    assert "38 businesses" in result.output
    assert "SerpApi searches used: 3" in result.output
    assert len(list(Path("out/runs").glob("*/leads-roofing-contractor-*.csv"))) == 1
    assert len(list(Path("out/runs").glob("*/leads-roofing-contractor-*.html"))) == 1
    assert len(RunStore(Path("out/runs")).list()) == 1, "CLI runs show up in the web app"
    assert SECRET not in result.output


def test_second_run_is_free_and_needs_no_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    args = ["search", "roofing contractor", "-l", "Tampa, FL", "--reviews", "--review-limit", "3"]
    use_transport(monkeypatch, FixtureTransport())
    assert runner.invoke(cli.app, [*args, "--yes"]).exit_code == 0

    use_transport(monkeypatch, ScriptedTransport())  # fails on any search
    result = runner.invoke(cli.app, args)  # no --yes, no input

    assert result.exit_code == 0, result.output
    assert "this run costs 0 searches" in result.output
    assert "SerpApi searches used: 0 (5 more served from cache)" in result.output


def test_no_cache_flag_bypasses_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    args = ["search", "roofing contractor", "-l", "St. Petersburg, FL", "--yes"]
    use_transport(monkeypatch, FixtureTransport())
    runner.invoke(cli.app, args)

    transport = FixtureTransport()
    use_transport(monkeypatch, transport)
    result = runner.invoke(cli.app, [*args, "--no-cache"])

    assert result.exit_code == 0, result.output
    assert len(transport.calls) == 1
    assert "SerpApi searches used: 1 (0 more served from cache)" in result.output


def test_invalid_key_is_reported_before_any_search(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = ScriptedTransport()
    transport.account_response = InvalidApiKeyError(
        "SerpApi rejected the API key (HTTP 401).", status_code=401
    )
    use_transport(monkeypatch, transport)

    result = runner.invoke(cli.app, ["search", "roofing contractor", "-l", "Tampa, FL", "--yes"])

    assert result.exit_code == 1
    assert "rejected the API key" in result.output
    assert transport.calls == []


def test_out_of_credits_mid_run_still_writes_partial_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = ScriptedTransport(
        maps_page([f"a{i}" for i in range(20)], more=True),
        OutOfCreditsError("Your SerpApi account has run out of searches."),
    )
    use_transport(monkeypatch, transport)

    result = runner.invoke(cli.app, ["search", "roofing contractor", "-l", "Tampa, FL", "--yes"])

    assert result.exit_code == 0, result.output
    assert "results are partial" in result.output and "run out of searches" in result.output
    assert len(list(Path("out/runs").glob("*/*.html"))) == 1


def test_ctrl_c_saves_partial_results_and_exits_130(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = ScriptedTransport(
        maps_page([f"a{i}" for i in range(20)], more=True), KeyboardInterrupt()
    )
    use_transport(monkeypatch, transport)

    result = runner.invoke(cli.app, ["search", "roofing contractor", "-l", "Tampa, FL", "--yes"])

    assert result.exit_code == 130
    assert "Ctrl+C" in result.output and "20 businesses" in result.output
    assert len(list(Path("out/runs").glob("*/*.csv"))) == 1


def test_empty_results_give_a_helpful_message(monkeypatch: pytest.MonkeyPatch) -> None:
    use_transport(monkeypatch, FixtureTransport())
    result = runner.invoke(cli.app, ["search", "unicorn groomer", "-l", "Nowhere, ZZ", "--yes"])

    assert result.exit_code == 0
    assert "No businesses found" in result.output and "broader niche" in result.output
    assert not Path("out").exists()


def test_bad_scoring_config_fails_before_spending_credits(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = FixtureTransport()
    use_transport(monkeypatch, transport)
    Path("scoring.yaml").write_text("rules:\n  no_websiet: {points: 5}\n", "utf-8")

    result = runner.invoke(
        cli.app, ["search", "roofing contractor", "-l", "Tampa, FL", "-c", "scoring.yaml", "--yes"]
    )

    assert result.exit_code == 1
    assert "rules.no_websiet" in result.output
    assert transport.calls == []


def test_account_command(monkeypatch: pytest.MonkeyPatch) -> None:
    use_transport(monkeypatch, FixtureTransport())
    result = runner.invoke(cli.app, ["account"])

    assert result.exit_code == 0, result.output
    assert "Demo Plan" in result.output and "962" in result.output
    assert "SECRET_API_KEY" not in result.output


def test_init_config_writes_editable_yaml_and_refuses_to_overwrite() -> None:
    assert runner.invoke(cli.app, ["init-config"]).exit_code == 0
    assert "no_website:" in Path("scoring.yaml").read_text("utf-8")

    again = runner.invoke(cli.app, ["init-config"])
    assert again.exit_code == 1 and "already exists" in again.output
    assert runner.invoke(cli.app, ["init-config", "--force"]).exit_code == 0


def test_cache_info_and_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "is empty" in runner.invoke(cli.app, ["cache", "info"]).output

    use_transport(monkeypatch, FixtureTransport())
    runner.invoke(cli.app, ["search", "roofing contractor", "-l", "Tampa, FL", "--yes"])

    info = runner.invoke(cli.app, ["cache", "info"])
    assert "google_maps" in info.output and "locations" in info.output
    cleared = runner.invoke(cli.app, ["cache", "clear", "--yes"])
    assert "Deleted 3 cached responses" in cleared.output  # 2 Maps pages + 1 location lookup
