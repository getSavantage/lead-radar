"""Background runs for the web app.

A run can take minutes (one SerpApi request per page and per review lookup), so the HTTP
request that starts it returns at once and the browser polls the job for progress. Only one
job runs at a time: two concurrent runs would make the credit estimate meaningless.
"""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from lead_radar.errors import LeadRadarError
from lead_radar.pipeline import Progress

logger = logging.getLogger(__name__)

JobStatus = Literal["running", "done", "failed"]


class JobAlreadyRunningError(RuntimeError):
    pass


@dataclass
class Job:
    id: str
    query: str = ""
    locations: list[str] = field(default_factory=list)
    with_reviews: bool = False
    status: JobStatus = "running"
    stage: str = "starting"
    message: str = "Starting…"
    current: int = 0
    total: int | None = None
    run_id: str | None = None
    error: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    def update(self, event: Progress) -> None:
        self.stage, self.message = event.stage, event.message
        self.current, self.total = event.current, event.total

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "query": self.query,
            "locations": self.locations,
            "with_reviews": self.with_reviews,
            "status": self.status,
            "stage": self.stage,
            "message": self.message,
            "current": self.current,
            "total": self.total,
            "run_id": self.run_id,
            "error": self.error,
            "cancelling": self.cancel.is_set() and self.status == "running",
        }


Work = Callable[[Job], str]
"""Does the run, reporting progress through ``job.update``; returns the saved run id."""


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def active(self) -> Job | None:
        with self._lock:
            return next((job for job in self._jobs.values() if job.status == "running"), None)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def start(
        self,
        work: Work,
        *,
        query: str = "",
        locations: list[str] | None = None,
        with_reviews: bool = False,
    ) -> Job:
        with self._lock:
            if any(job.status == "running" for job in self._jobs.values()):
                raise JobAlreadyRunningError("Another search is still running.")
            job = Job(
                id=secrets.token_hex(6),
                query=query,
                locations=list(locations or []),
                with_reviews=with_reviews,
            )
            self._jobs[job.id] = job
        threading.Thread(target=self._execute, args=(job, work), daemon=True).start()
        return job

    @staticmethod
    def _execute(job: Job, work: Work) -> None:
        try:
            job.run_id = work(job)
            job.status = "done"
        except LeadRadarError as exc:
            job.error = str(exc)
            job.status = "failed"
        except Exception:
            # Never put arbitrary exception text in front of the user: it may contain a URL
            # with the API key. The traceback goes to the server log only.
            logger.exception("Run %s failed", job.id)
            job.error = "Unexpected error. See the terminal running `lead-radar web` for details."
            job.status = "failed"
