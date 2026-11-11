"""Smoke test of the Streamlit app in demo mode, using Streamlit's headless test runner."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest

UI_SCRIPT = Path(__file__).parent.parent / "src" / "lead_radar" / "ui.py"


def test_demo_mode_runs_end_to_end_without_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)

    app = AppTest.from_file(str(UI_SCRIPT), default_timeout=30).run()
    assert not app.exception
    assert app.title[0].value == "Lead Radar"
    assert app.toggle[0].value is True, "without a key the app falls back to demo mode"

    app.button[0].click().run()

    assert not app.exception and not app.error
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Businesses"] == "38"
    assert metrics["Searches used"] == "41"
    assert len(app.dataframe) == 1
    assert len(app.expander) == 10
