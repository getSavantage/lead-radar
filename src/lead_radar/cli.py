"""Command-line interface. Presentation only: all logic lives in ``lead_radar.pipeline``."""

from __future__ import annotations

import threading
import webbrowser
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Annotated

import typer
from dotenv import find_dotenv, load_dotenv
from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from lead_radar import __version__, pipeline
from lead_radar.cache import DEFAULT_CACHE_DIR, ResponseCache
from lead_radar.client import SerpApiGateway, live_gateway
from lead_radar.config import ScoringConfig, default_config_text, load_config
from lead_radar.demo import DEMO_NOW, DEMO_REQUEST, FixtureTransport
from lead_radar.errors import LeadRadarError, SerpApiRequestError
from lead_radar.export import write_reports
from lead_radar.models import AccountInfo, CreditEstimate, RunResult, SearchRequest, Tier
from lead_radar.store import DEFAULT_STORE_DIR, RunStore

app = typer.Typer(
    help="Find local home-service businesses with SerpApi and score who needs automation help.",
    no_args_is_help=True,
    add_completion=False,
)
cache_app = typer.Typer(help="Inspect or clear the local response cache.", no_args_is_help=True)
app.add_typer(cache_app, name="cache")

console = Console()
err_console = Console(stderr=True)

_TIER_STYLE = {Tier.HOT: "bold red", Tier.WARM: "yellow", Tier.COLD: "dim"}

CacheDirOption = Annotated[
    Path, typer.Option("--cache-dir", help="Where API responses are cached.", show_default=True)
]


def _fail(message: str, code: int = 1) -> typer.Exit:
    err_console.print(f"[bold red]Error:[/] {message}", highlight=False)
    return typer.Exit(code)


def _version(value: bool) -> None:
    if value:
        console.print(f"lead-radar {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show version.")
    ] = False,
) -> None:
    # usecwd: find the .env next to where the command is run, not next to the installed package.
    load_dotenv(find_dotenv(usecwd=True))


def _print_account(account: AccountInfo) -> None:
    table = Table.grid(padding=(0, 2))
    table.add_row("Plan", account.plan_name or "unknown")
    table.add_row("Status", account.account_status or "unknown")
    table.add_row("Searches left", _fmt(account.total_searches_left))
    table.add_row(
        "Used this month", f"{_fmt(account.this_month_usage)} of {_fmt(account.searches_per_month)}"
    )
    table.add_row(
        "This hour",
        f"{_fmt(account.this_hour_searches)} of "
        f"{_fmt(account.account_rate_limit_per_hour)} allowed",
    )
    console.print(Panel(table, title="SerpApi account", expand=False))


def _fmt(value: int | None) -> str:
    return "unknown" if value is None else f"{value:,}"


def _print_estimate(
    estimate: CreditEstimate, request: SearchRequest, account: AccountInfo | None
) -> None:
    bound = "exactly" if estimate.exact else "up to"
    table = Table.grid(padding=(0, 2))
    table.add_row(
        "Google Maps pages",
        f"{bound} {estimate.maps_calls}",
        f"[dim]{len(request.locations)} location(s) x {pipeline.max_pages(request.max_results)} "
        f"page(s) of {pipeline.PAGE_SIZE}[/]",
    )
    if request.with_reviews:
        table.add_row(
            "Review lookups", f"{bound} {estimate.review_calls}", "[dim]1 search per lead[/]"
        )
    table.add_row("Already cached", str(estimate.cached_calls), "[dim]free, served from disk[/]")
    table.add_row("[bold]Searches this run[/]", f"[bold]{bound} {estimate.total}[/]", "")
    if account is not None:
        table.add_row(
            "Searches left", _fmt(account.total_searches_left), f"[dim]{account.plan_name or ''}[/]"
        )
    console.print(Panel(table, title="Credit estimate", expand=False))
    left = account.total_searches_left if account else None
    if left is not None and estimate.total > left:
        console.print(
            f"[yellow]This run may need more searches than you have left ({left:,}). "
            "It will stop when credits run out and keep partial results.[/]"
        )


def _print_leads(result: RunResult, limit: int = 15) -> None:
    table = Table(title=f"Top {min(limit, len(result.leads))} of {len(result.leads)} leads")
    table.add_column("Score", justify="right")
    table.add_column("Business")
    table.add_column("Phone", no_wrap=True)
    table.add_column("Rating", justify="right", no_wrap=True)
    table.add_column("Why")
    for lead in result.leads[:limit]:
        business = lead.business
        rating = (
            "n/a"
            if business.rating is None
            else f"{business.rating:.1f} ({business.review_count or 0})"
        )
        table.add_row(
            f"[{_TIER_STYLE[lead.tier]}]{lead.score}[/]",
            business.name,
            business.phone or "-",
            rating,
            "\n".join(lead.why) or "[dim]no gaps detected[/]",
        )
    console.print(table)


SaveReports = Callable[[RunResult], tuple[Path, Path]]
"""Writes the reports somewhere and returns the (csv, html) paths."""


def _finish(result: RunResult, save: SaveReports, open_report: bool) -> None:
    """Shared tail of ``search`` and ``demo``: summary, files, exit code."""
    for typed, place in result.resolved_locations.items():
        console.print(f"[dim]{typed} -> map centred on {place}[/]", highlight=False)
    if result.stop_reason:
        console.print(f"[yellow]{result.stop_reason}[/]")
    for warning in result.warnings:
        console.print(f"[yellow]Warning:[/] {warning}", highlight=False)

    if not result.leads:
        console.print(
            "[yellow]No businesses found.[/] Google Maps returned no results for "
            f"'{result.request.query}' in {', '.join(result.request.locations)}.\n"
            "Try a broader niche, check the location spelling, or zoom out with a lower --zoom."
        )
        raise typer.Exit(130 if result.interrupted else 0)

    _print_leads(result)
    counts = result.tier_counts()
    console.print(
        f"{len(result.leads)} businesses: [bold red]{counts[Tier.HOT]} hot[/], "
        f"[yellow]{counts[Tier.WARM]} warm[/], {counts[Tier.COLD]} cold. "
        f"{result.duplicates_removed} duplicates removed. "
        f"SerpApi searches used: {result.stats.api_calls} "
        f"({result.stats.cache_hits} more served from cache)."
    )
    csv_path, html_path = save(result)
    console.print(f"CSV:    {csv_path}", highlight=False)
    console.print(f"Report: {html_path}", highlight=False)
    if open_report:
        webbrowser.open(html_path.resolve().as_uri())
    if result.interrupted:
        raise typer.Exit(130)


@app.command()
def search(
    query: Annotated[str, typer.Argument(help='Niche to look for, e.g. "roofing contractor".')],
    location: Annotated[
        list[str],
        typer.Option(
            "--location",
            "-l",
            help='City ("Tampa, FL") or SerpApi ll coordinates ("@27.95,-82.45,12z"). Repeatable.',
        ),
    ],
    max_results: Annotated[
        int,
        typer.Option("--max-results", "-n", min=1, max=500, help="Max businesses per location."),
    ] = 40,
    reviews: Annotated[
        bool,
        typer.Option(
            "--reviews", help="Also fetch each lead's latest reviews (1 search per lead)."
        ),
    ] = False,
    review_limit: Annotated[
        int | None,
        typer.Option("--review-limit", min=1, help="Only fetch reviews for the N best leads."),
    ] = None,
    zoom: Annotated[
        int, typer.Option(min=3, max=30, help="Map zoom (z) for named locations.")
    ] = 12,
    hl: Annotated[str, typer.Option(help="Google interface language.")] = "en",
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Scoring config YAML (see init-config).")
    ] = None,
    out: Annotated[
        Path,
        typer.Option("--out", "-o", help="Where runs are saved (shared with `lead-radar web`)."),
    ] = DEFAULT_STORE_DIR,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the credit confirmation.")] = False,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Ignore the local cache and fetch everything again.")
    ] = False,
    cache_dir: CacheDirOption = DEFAULT_CACHE_DIR,
    cache_ttl_hours: Annotated[
        float, typer.Option(min=0, help="Treat cached responses older than this as missing.")
    ] = 168.0,
    open_report: Annotated[
        bool, typer.Option("--open", help="Open the HTML report when done.")
    ] = False,
) -> None:
    """Search Google Maps via SerpApi, score every business and write CSV + HTML reports."""
    try:
        scoring = load_config(config)
        request = SearchRequest(
            query=query,
            locations=location,
            max_results=max_results,
            zoom=zoom,
            hl=hl,
            with_reviews=reviews,
            review_limit=review_limit,
        )
        cache = None if no_cache else ResponseCache(cache_dir, timedelta(hours=cache_ttl_hours))
        gateway = live_gateway(
            cache=cache,
            on_retry=lambda attempt, delay, exc: console.print(
                f"[yellow]{exc} Retrying in {delay:.1f}s (attempt {attempt + 1})...[/]",
                highlight=False,
            ),
        )
        estimate = pipeline.estimate(request, gateway, scoring)
        account: AccountInfo | None
        try:
            account = gateway.account()
        except SerpApiRequestError as exc:
            if exc.status_code in {401, 403}:
                raise
            account = None
            console.print(f"[yellow]Could not read your SerpApi balance: {exc}[/]", highlight=False)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise _fail(f"Invalid search options: {problems}") from None
    except LeadRadarError as exc:
        raise _fail(str(exc)) from None

    _print_estimate(estimate, request, account)
    if estimate.total == 0:
        console.print("[green]Everything is cached: this run costs 0 searches.[/]")
    elif not yes and not typer.confirm(
        f"Use up to {estimate.total} SerpApi searches?", default=True
    ):
        console.print("Cancelled. No searches were used.")
        raise typer.Exit(0)

    result = _run_with_status(request, gateway, scoring)

    def save(result: RunResult) -> tuple[Path, Path]:
        summary = RunStore(out).save(result)
        directory = out / summary.id
        return directory / f"{summary.basename}.csv", directory / f"{summary.basename}.html"

    _finish(result, save, open_report)
    console.print("[dim]Browse all your reports with: lead-radar web[/]")


def _run_with_status(
    request: SearchRequest, gateway: SerpApiGateway, scoring: ScoringConfig
) -> RunResult:
    try:
        with console.status("Starting...") as status:

            def on_progress(event: pipeline.Progress) -> None:
                if event.stage == "reviews" and event.total:
                    status.update(f"Reviews {event.current + 1}/{event.total}: {event.message}")
                else:
                    status.update(event.message)

            return pipeline.run(request, gateway, scoring, on_progress=on_progress)
    except LeadRadarError as exc:
        raise _fail(str(exc)) from None


@app.command()
def account() -> None:
    """Show your SerpApi plan and remaining searches (free: the Account API is not metered)."""
    try:
        _print_account(live_gateway(cache=None).account())
    except LeadRadarError as exc:
        raise _fail(str(exc)) from None


@app.command()
def demo(
    out: Annotated[Path, typer.Option("--out", "-o", help="Output directory.")] = Path("out/demo"),
    open_report: Annotated[
        bool, typer.Option("--open", help="Open the HTML report when done.")
    ] = False,
) -> None:
    """Run the full pipeline offline on bundled sample data. No API key, no credits."""
    console.print(
        "[dim]Demo mode: synthetic businesses replayed from bundled SerpApi-shaped fixtures.[/]"
    )
    gateway = SerpApiGateway(FixtureTransport())
    result = pipeline.run(DEMO_REQUEST, gateway, ScoringConfig(), now=DEMO_NOW)
    _finish(result, lambda r: write_reports(r, out, "demo-report"), open_report)


@app.command("init-config")
def init_config(
    path: Annotated[Path, typer.Argument(help="Where to write the YAML.")] = Path("scoring.yaml"),
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write the default scoring rules to a YAML file you can edit."""
    if path.exists() and not force:
        raise _fail(f"{path} already exists. Use --force to overwrite it.")
    path.write_text(default_config_text(), encoding="utf-8")
    console.print(f"Wrote {path}. Edit it, then run: lead-radar search ... --config {path}")


@app.command()
def web(
    host: Annotated[str, typer.Option(help="Interface to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8000,
    out: Annotated[
        Path, typer.Option("--out", "-o", help="Where runs are saved (shared with `search`).")
    ] = DEFAULT_STORE_DIR,
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Scoring config YAML (see init-config).")
    ] = None,
    cache_dir: CacheDirOption = DEFAULT_CACHE_DIR,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Do not open a browser tab.")
    ] = False,
) -> None:
    """Start the web app: run searches, browse past reports and download them."""
    import uvicorn

    from lead_radar.web.app import create_app

    try:
        scoring = load_config(config)
    except LeadRadarError as exc:
        raise _fail(str(exc)) from None
    application = create_app(store=RunStore(out), cache_dir=cache_dir, config=scoring)
    url = f"http://{'localhost' if host in {'127.0.0.1', '0.0.0.0'} else host}:{port}"  # noqa: S104
    if host not in {"127.0.0.1", "localhost"}:
        console.print(
            "[yellow]Listening beyond this computer: anyone who can reach it can spend your "
            "SerpApi searches.[/]"
        )
    console.print(f"Lead Radar is running at [bold]{url}[/]  (Ctrl+C to stop)")
    if not no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(application, host=host, port=port, log_level="warning")


@cache_app.command("info")
def cache_info(cache_dir: CacheDirOption = DEFAULT_CACHE_DIR) -> None:
    """Show how many responses are cached."""
    summary = ResponseCache(cache_dir).summary()
    if not summary:
        console.print(f"The cache at {cache_dir} is empty.")
        return
    table = Table(title=f"Cache at {cache_dir}")
    table.add_column("Engine")
    table.add_column("Responses", justify="right")
    table.add_column("Size", justify="right")
    for engine, (count, size) in sorted(summary.items()):
        table.add_row(engine, str(count), f"{size / 1024:.0f} kB")
    console.print(table)


@cache_app.command("clear")
def cache_clear(
    cache_dir: CacheDirOption = DEFAULT_CACHE_DIR,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """Delete all cached responses."""
    cache = ResponseCache(cache_dir)
    total = sum(count for count, _ in cache.summary().values())
    if total == 0:
        console.print(f"The cache at {cache_dir} is already empty.")
        return
    if not yes and not typer.confirm(f"Delete {total} cached responses from {cache_dir}?"):
        raise typer.Exit(0)
    console.print(f"Deleted {cache.clear()} cached responses.")
