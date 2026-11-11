"""Saved runs: one folder per run, shared by the CLI and the web app.

Layout::

    out/runs/<run id>/
        run.json        the full RunResult, so a report can be re-rendered at any time
        summary.json    the few fields the history list needs (cheap to read)
        <name>.csv      e.g. leads-roofing-contractor-tampa-fl-20261112-0130.csv
        <name>.html

Run ids sort chronologically (``20261112-013045-a1b2c3``), so newest-first is a reverse sort.
"""

from __future__ import annotations

import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from lead_radar.export import output_basename, write_reports
from lead_radar.models import RunResult, Tier

DEFAULT_STORE_DIR = Path("out/runs")

_RUN_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")

ReportKind = Literal["csv", "html"]


class RunSummary(BaseModel):
    id: str
    query: str
    locations: list[str]
    generated_at: datetime
    leads: int
    hot: int
    warm: int
    cold: int
    api_calls: int
    cache_hits: int
    with_reviews: bool
    partial: bool
    demo: bool
    basename: str


class RunStore:
    def __init__(self, root: Path = DEFAULT_STORE_DIR) -> None:
        self.root = root

    @staticmethod
    def is_valid_id(run_id: str) -> bool:
        """Ids come from URLs, so they are checked before they ever touch a path."""
        return bool(_RUN_ID.match(run_id))

    def _dir(self, run_id: str) -> Path:
        if not self.is_valid_id(run_id):
            raise KeyError(run_id)
        return self.root / run_id

    def save(self, result: RunResult, *, demo: bool = False) -> RunSummary:
        run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
        directory = self._dir(run_id)
        directory.mkdir(parents=True, exist_ok=False)
        basename = output_basename(result)
        write_reports(result, directory, basename)
        (directory / "run.json").write_text(result.model_dump_json(), "utf-8")

        counts = result.tier_counts()
        summary = RunSummary(
            id=run_id,
            query=result.request.query,
            locations=result.request.locations,
            generated_at=result.generated_at,
            leads=len(result.leads),
            hot=counts[Tier.HOT],
            warm=counts[Tier.WARM],
            cold=counts[Tier.COLD],
            api_calls=result.stats.api_calls,
            cache_hits=result.stats.cache_hits,
            with_reviews=result.request.with_reviews,
            partial=result.stop_reason is not None,
            demo=demo,
            basename=basename,
        )
        (directory / "summary.json").write_text(summary.model_dump_json(indent=2), "utf-8")
        return summary

    def list(self) -> list[RunSummary]:
        """Newest first. Folders that are not valid runs are skipped, never deleted."""
        if not self.root.is_dir():
            return []
        summaries: list[RunSummary] = []
        for directory in sorted(self.root.iterdir(), reverse=True):
            if not self.is_valid_id(directory.name):
                continue
            try:
                summaries.append(
                    RunSummary.model_validate_json((directory / "summary.json").read_text("utf-8"))
                )
            except (OSError, ValidationError):
                continue
        return summaries

    def summary(self, run_id: str) -> RunSummary:
        try:
            text = (self._dir(run_id) / "summary.json").read_text("utf-8")
        except OSError as exc:
            raise KeyError(run_id) from exc
        return RunSummary.model_validate_json(text)

    def load(self, run_id: str) -> RunResult:
        try:
            text = (self._dir(run_id) / "run.json").read_text("utf-8")
        except OSError as exc:
            raise KeyError(run_id) from exc
        return RunResult.model_validate_json(text)

    def report_path(self, run_id: str, kind: ReportKind) -> Path:
        path = self._dir(run_id) / f"{self.summary(run_id).basename}.{kind}"
        if not path.is_file():
            raise KeyError(run_id)
        return path

    def delete(self, run_id: str) -> None:
        directory = self._dir(run_id)
        if not directory.is_dir():
            raise KeyError(run_id)
        shutil.rmtree(directory)
