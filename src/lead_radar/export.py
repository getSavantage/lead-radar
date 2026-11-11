"""CSV and self-contained HTML exports of a run."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from jinja2 import Environment, PackageLoader

from lead_radar import __version__
from lead_radar.models import Lead, RunResult, Tier

CSV_COLUMNS = [
    "score",
    "tier",
    "name",
    "category",
    "phone",
    "website",
    "address",
    "rating",
    "review_count",
    "online_booking",
    "booking_link",
    "hours",
    "unclaimed_listing",
    "missed_call_complaints",
    "owner_response_rate",
    "recent_avg_rating",
    "why",
    "maps_url",
    "place_id",
    "data_id",
    "search_location",
]

_PHONE_OR_NUMBER = re.compile(r"^[+-]?[\d\s().-]+$")


def csv_safe(value: object) -> object:
    """Neutralise spreadsheet formula injection.

    Business names and review text come from the public web. A cell that starts with
    ``=``, ``@``, ``+`` or ``-`` can be executed as a formula by Excel or Google Sheets, so
    such cells get a leading apostrophe. Plain numbers and phone numbers ("+1 813-555-0100")
    are left alone.
    """
    if not isinstance(value, str) or not value:
        return value
    if value[0] in "=@\t\r" or (value[0] in "+-" and not _PHONE_OR_NUMBER.match(value)):
        return "'" + value
    return value


def lead_row(lead: Lead) -> dict[str, object]:
    business = lead.business
    signals = lead.review_signals
    return {
        "score": lead.score,
        "tier": lead.tier.value,
        "name": business.name,
        "category": business.category or "",
        "phone": business.phone or "",
        "website": business.website or "",
        "address": business.address or "",
        "rating": "" if business.rating is None else business.rating,
        "review_count": "" if business.review_count is None else business.review_count,
        "online_booking": "yes" if business.has_online_booking else "no",
        "booking_link": business.booking_link or "",
        "hours": business.hours or "",
        "unclaimed_listing": "yes" if business.unclaimed_listing else "no",
        "missed_call_complaints": "" if signals is None else len(signals.complaints),
        "owner_response_rate": (
            "" if signals is None or signals.owner_response_rate is None
            else signals.owner_response_rate
        ),
        "recent_avg_rating": (
            "" if signals is None or signals.recent_avg_rating is None
            else signals.recent_avg_rating
        ),
        # Points go last: a cell starting with "+20" would be parsed as a formula.
        "why": " | ".join(f"{reason.message} (+{reason.points})" for reason in lead.reasons),
        "maps_url": business.maps_url,
        "place_id": business.place_id or "",
        "data_id": business.data_id or "",
        "search_location": business.search_location,
    }  # fmt: skip


def render_csv(result: RunResult) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for lead in result.leads:
        writer.writerow({key: csv_safe(value) for key, value in lead_row(lead).items()})
    return buffer.getvalue()


_env = Environment(
    loader=PackageLoader("lead_radar", "templates"),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_html(result: RunResult, *, embedded: bool = False) -> str:
    """Render the report as one HTML file with inline CSS and JS and no external requests.

    ``embedded`` drops the page title and footer, for showing the report inside the web app.
    """
    counts = result.tier_counts()
    return _env.get_template("report.html.j2").render(
        result=result,
        request=result.request,
        leads=result.leads,
        hot=counts[Tier.HOT],
        warm=counts[Tier.WARM],
        cold=counts[Tier.COLD],
        with_reviews=result.request.with_reviews,
        version=__version__,
        embedded=embedded,
    )


def output_basename(result: RunResult) -> str:
    """e.g. ``leads-roofing-contractor-tampa-fl-20261101-1200``."""

    def slug(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")

    first = slug(result.request.locations[0])[:30]
    more = (
        f"-and-{len(result.request.locations) - 1}-more"
        if len(result.request.locations) > 1
        else ""
    )
    stamp = result.generated_at.strftime("%Y%m%d-%H%M")
    return f"leads-{slug(result.request.query)[:40]}-{first}{more}-{stamp}"


def write_reports(
    result: RunResult, directory: Path, basename: str | None = None
) -> tuple[Path, Path]:
    """Write ``<basename>.csv`` and ``<basename>.html`` into ``directory``; return both paths."""
    directory.mkdir(parents=True, exist_ok=True)
    basename = basename or output_basename(result)
    csv_path = directory / f"{basename}.csv"
    html_path = directory / f"{basename}.html"
    # utf-8-sig: Excel only detects UTF-8 in CSV files when the BOM is present.
    csv_path.write_text(render_csv(result), encoding="utf-8-sig", newline="")
    html_path.write_text(render_html(result), encoding="utf-8")
    return csv_path, html_path
