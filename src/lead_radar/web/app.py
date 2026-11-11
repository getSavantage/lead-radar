"""Local web app: a JSON API over ``pipeline`` and ``store``, plus a static single-page UI.

There is no search, scoring or rendering logic here; every endpoint delegates to the same
functions the CLI uses. The API key stays on the server and is never sent to the browser.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from lead_radar import __version__, pipeline
from lead_radar.cache import DEFAULT_CACHE_DIR, ResponseCache
from lead_radar.client import SerpApiGateway, api_key_from_env, live_gateway
from lead_radar.config import ScoringConfig
from lead_radar.demo import DEMO_NOW, DEMO_REQUEST, FixtureTransport
from lead_radar.errors import InvalidApiKeyError, LeadRadarError, MissingApiKeyError
from lead_radar.export import render_html
from lead_radar.models import SearchRequest
from lead_radar.store import ReportKind, RunStore
from lead_radar.web.jobs import Job, JobAlreadyRunningError, JobManager

STATIC_DIR = Path(__file__).parent / "static"
CACHE_TTL = timedelta(hours=168)

GatewayFactory = Callable[[bool], SerpApiGateway]
"""Builds a live gateway; the argument says whether to use the disk cache."""


class SearchForm(BaseModel):
    query: str = ""
    locations: list[str] = Field(default_factory=list)
    max_results: int = 40
    with_reviews: bool = False
    review_limit: int | None = None
    use_cache: bool = True

    def to_request(self) -> SearchRequest:
        try:
            return SearchRequest(
                query=self.query,
                locations=self.locations,
                max_results=self.max_results,
                with_reviews=self.with_reviews,
                review_limit=self.review_limit if self.with_reviews else None,
            )
        except ValidationError as exc:
            raise HTTPException(422, _describe(exc)) from None


class StartJob(SearchForm):
    demo: bool = False
    max_searches: int | None = None
    """The cost the user confirmed. The server re-estimates and refuses anything dearer."""


_FIELD_NAMES = {"query": "Niche", "locations": "Locations", "max_results": "Results per location"}


def _describe(exc: ValidationError) -> str:
    problems = []
    for error in exc.errors():
        field = str(error["loc"][0]) if error["loc"] else "input"
        problems.append(f"{_FIELD_NAMES.get(field, field)}: {error['msg']}")
    return "; ".join(problems)


def _has_key_from_env() -> bool:
    try:
        api_key_from_env()
    except MissingApiKeyError:
        return False
    return True


def create_app(
    *,
    store: RunStore,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    config: ScoringConfig | None = None,
    gateway_factory: GatewayFactory | None = None,
    has_key: Callable[[], bool] = _has_key_from_env,
) -> FastAPI:
    config = config or ScoringConfig()
    jobs = JobManager()

    def make_gateway(use_cache: bool) -> SerpApiGateway:
        if gateway_factory is not None:
            return gateway_factory(use_cache)
        return live_gateway(cache=ResponseCache(cache_dir, CACHE_TTL) if use_cache else None)

    app = FastAPI(title="Lead Radar", version=__version__, docs_url="/api/docs", redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.exception_handler(LeadRadarError)
    async def lead_radar_error(_: Request, exc: LeadRadarError) -> JSONResponse:
        status = 401 if isinstance(exc, InvalidApiKeyError) else 400
        return JSONResponse({"detail": str(exc)}, status_code=status)

    def run_or_404(run_id: str) -> None:
        try:
            store.summary(run_id)
        except (KeyError, ValueError):
            raise HTTPException(404, "Report not found.") from None

    # --- pages -----------------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    # --- status and account ----------------------------------------------------------------

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        active = jobs.active()
        return {
            "version": __version__,
            "has_key": has_key(),
            "active_job": active.id if active else None,
            "demo": {"query": DEMO_REQUEST.query, "locations": DEMO_REQUEST.locations},
        }

    @app.get("/api/account")
    def account() -> dict[str, Any]:
        return make_gateway(False).account().model_dump()

    # --- estimate and runs -----------------------------------------------------------------

    @app.post("/api/estimate")
    def estimate(form: SearchForm) -> dict[str, Any]:
        request = form.to_request()
        gateway = make_gateway(form.use_cache)
        result = pipeline.estimate(request, gateway, config)
        account_info: dict[str, Any] | None = None
        account_error: str | None = None
        try:
            account_info = gateway.account().model_dump()
        except InvalidApiKeyError:
            raise
        except LeadRadarError as exc:
            account_error = str(exc)
        return {
            "estimate": {**result.model_dump(), "total": result.total},
            "account": account_info,
            "account_error": account_error,
        }

    @app.post("/api/jobs", status_code=202)
    def start_job(body: StartJob) -> dict[str, str]:
        if body.demo:

            def work(job: Job) -> str:
                gateway = SerpApiGateway(FixtureTransport())
                result = pipeline.run(
                    DEMO_REQUEST,
                    gateway,
                    config,
                    now=DEMO_NOW,
                    on_progress=job.update,
                    should_stop=job.cancel.is_set,
                )
                return store.save(result, demo=True).id

        else:
            request = body.to_request()
            gateway = make_gateway(body.use_cache)
            cost = pipeline.estimate(request, gateway, config).total
            if cost > 0 and (body.max_searches is None or cost > body.max_searches):
                raise HTTPException(
                    409,
                    f"This search can use up to {cost} searches, more than you confirmed. "
                    "Estimate it again.",
                )

            def work(job: Job) -> str:
                result = pipeline.run(
                    request,
                    gateway,
                    config,
                    on_progress=job.update,
                    should_stop=job.cancel.is_set,
                )
                return store.save(result).id

        try:
            job = jobs.start(work)
        except JobAlreadyRunningError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"job_id": job.id}

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job not found.")
        return job.snapshot()

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Job not found.")
        job.cancel.set()
        return job.snapshot()

    # --- saved reports ---------------------------------------------------------------------

    @app.get("/api/runs")
    def list_runs() -> list[dict[str, Any]]:
        return [summary.model_dump(mode="json") for summary in store.list()]

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        run_or_404(run_id)
        result = store.load(run_id)
        return {
            **store.summary(run_id).model_dump(mode="json"),
            "stop_reason": result.stop_reason,
            "warnings": result.warnings,
            "duplicates_removed": result.duplicates_removed,
        }

    @app.get("/api/runs/{run_id}/view", response_class=HTMLResponse)
    def view_run(run_id: str) -> HTMLResponse:
        """The report, rendered for embedding in the app (no page title or footer)."""
        run_or_404(run_id)
        return HTMLResponse(render_html(store.load(run_id), embedded=True))

    @app.get("/api/runs/{run_id}/report.{kind}")
    def download_report(run_id: str, kind: str, download: bool = False) -> Response:
        run_or_404(run_id)
        if kind not in ("csv", "html"):
            raise HTTPException(404, "Unknown report format.")
        report_kind: ReportKind = "csv" if kind == "csv" else "html"
        path = store.report_path(run_id, report_kind)
        media_type = "text/csv" if kind == "csv" else "text/html"
        disposition = "attachment" if download or kind == "csv" else "inline"
        return FileResponse(
            path, media_type=media_type, filename=path.name, content_disposition_type=disposition
        )

    @app.delete("/api/runs/{run_id}", status_code=204)
    def delete_run(run_id: str) -> Response:
        run_or_404(run_id)
        store.delete(run_id)
        return Response(status_code=204)

    return app
