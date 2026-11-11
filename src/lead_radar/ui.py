"""Streamlit UI. A thin layer over ``lead_radar.pipeline``: no search or scoring logic here.

Run with ``lead-radar ui`` (or ``streamlit run src/lead_radar/ui.py``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import streamlit as st
from dotenv import find_dotenv, load_dotenv
from pydantic import ValidationError

from lead_radar import pipeline
from lead_radar.cache import DEFAULT_CACHE_DIR, ResponseCache
from lead_radar.client import SerpApiGateway, api_key_from_env, live_gateway
from lead_radar.config import ScoringConfig
from lead_radar.demo import DEMO_NOW, DEMO_REQUEST, FixtureTransport
from lead_radar.errors import LeadRadarError, MissingApiKeyError
from lead_radar.export import lead_row, output_basename, render_csv, render_html
from lead_radar.models import AccountInfo, CreditEstimate, RunResult, SearchRequest, Tier

CACHE_TTL = timedelta(hours=168)


def _has_api_key() -> bool:
    try:
        api_key_from_env()
    except MissingApiKeyError:
        return False
    return True


def _sidebar(has_key: bool) -> tuple[bool, SearchRequest | None, bool]:
    """Render the inputs. Returns (demo mode, request or None if invalid, use cache)."""
    with st.sidebar:
        st.header("Search")
        demo = st.toggle(
            "Demo mode (offline sample data)",
            value=not has_key,
            disabled=not has_key,
            help="Replays bundled SerpApi-shaped fixtures. No API key, no credits.",
        )
        if not has_key:
            st.caption("Set `SERPAPI_API_KEY` in your environment or `.env` to run live searches.")
        if demo:
            st.caption(
                f"Demo search: **{DEMO_REQUEST.query}** in {', '.join(DEMO_REQUEST.locations)}"
            )
            return True, DEMO_REQUEST, False

        query = st.text_input("Niche", value="roofing contractor")
        locations = st.text_area(
            "Locations (one per line)",
            value="Tampa, FL",
            help='A city such as "Tampa, FL", or SerpApi ll coordinates such as '
            '"@27.9506,-82.4572,12z".',
        )
        max_results = st.slider("Max businesses per location", 20, 120, 40, step=20)
        with_reviews = st.checkbox(
            "Analyse latest reviews",
            help="Finds missed-call complaints and unanswered reviews. Costs 1 search per lead.",
        )
        review_limit = None
        if with_reviews:
            review_limit = st.number_input(
                "Only for the N best leads", min_value=1, max_value=500, value=20
            )
        use_cache = st.checkbox(
            "Use local cache", value=True, help="Cached responses are free. Untick to refetch."
        )
        try:
            request = SearchRequest(
                query=query,
                locations=locations.splitlines(),
                max_results=max_results,
                with_reviews=with_reviews,
                review_limit=int(review_limit) if review_limit else None,
            )
        except ValidationError:
            st.warning("Enter a niche and at least one location.")
            return False, None, use_cache
        return False, request, use_cache


def _gateway(use_cache: bool) -> SerpApiGateway:
    cache = ResponseCache(DEFAULT_CACHE_DIR, CACHE_TTL) if use_cache else None
    return live_gateway(cache=cache)


def _show_estimate(estimate: CreditEstimate, account: AccountInfo) -> None:
    bound = "" if estimate.exact else "up to "
    columns = st.columns(4)
    columns[0].metric("Searches this run", f"{bound}{estimate.total}")
    columns[1].metric("Maps pages", estimate.maps_calls)
    columns[2].metric("Review lookups", estimate.review_calls)
    left = account.total_searches_left
    columns[3].metric("Searches left", "unknown" if left is None else f"{left:,}")
    if estimate.cached_calls:
        st.caption(
            f"{estimate.cached_calls} request(s) are already in the local cache and are free."
        )
    if left is not None and estimate.total > left:
        st.warning(
            "This run may need more searches than you have left. "
            "It will stop early and keep partial results."
        )


def _run(request: SearchRequest, gateway: SerpApiGateway, *, demo: bool) -> RunResult:
    bar = st.progress(0.0, text="Starting...")

    def on_progress(event: pipeline.Progress) -> None:
        fraction = event.current / event.total if event.total else 0.0
        bar.progress(min(fraction, 1.0), text=event.message)

    try:
        return pipeline.run(
            request,
            gateway,
            ScoringConfig(),
            now=DEMO_NOW if demo else None,
            on_progress=on_progress,
        )
    finally:
        bar.empty()


def _show_result(result: RunResult) -> None:
    counts = result.tier_counts()
    columns = st.columns(5)
    columns[0].metric("Businesses", len(result.leads))
    columns[1].metric("Hot", counts[Tier.HOT])
    columns[2].metric("Warm", counts[Tier.WARM])
    columns[3].metric("Searches used", result.stats.api_calls)
    columns[4].metric("From cache", result.stats.cache_hits)
    if result.stop_reason:
        st.warning(result.stop_reason)
    for warning in result.warnings:
        st.warning(warning)
    if not result.leads:
        st.info("No businesses found. Try a broader niche or check the location spelling.")
        return

    tiers = st.multiselect(
        "Tiers", [tier.value for tier in Tier], default=[Tier.HOT.value, Tier.WARM.value]
    )
    leads = [lead for lead in result.leads if lead.tier.value in tiers]
    rows: list[dict[str, Any]] = [lead_row(lead) for lead in leads]
    st.dataframe(
        rows,
        hide_index=True,
        use_container_width=True,
        column_order=[
            "score",
            "name",
            "phone",
            "website",
            "rating",
            "review_count",
            "online_booking",
            "why",
            "maps_url",
            "address",
        ],
        column_config={
            "score": st.column_config.ProgressColumn(
                "Score", min_value=0, max_value=100, format="%d"
            ),
            "name": "Business",
            "phone": "Phone",
            "website": st.column_config.LinkColumn("Website"),
            "rating": "Rating",
            "review_count": "Reviews",
            "online_booking": "Books online",
            "why": st.column_config.TextColumn("Why", width="large"),
            "maps_url": st.column_config.LinkColumn("Maps", display_text="Open"),
            "address": "Address",
        },
    )

    basename = output_basename(result)
    left, right = st.columns(2)
    left.download_button(
        "Download CSV", render_csv(result), file_name=f"{basename}.csv", mime="text/csv"
    )
    right.download_button(
        "Download HTML report",
        render_html(result),
        file_name=f"{basename}.html",
        mime="text/html",
    )

    st.subheader("Why the top leads scored high")
    for lead in leads[:10]:
        with st.expander(f"{lead.score} · {lead.business.name}"):
            for reason in lead.reasons:
                st.markdown(f"**+{reason.points}** {reason.message}")
            if lead.review_signals:
                for complaint in lead.review_signals.complaints:
                    st.caption(f"{complaint.date or 'recent'} · “{complaint.excerpt}”")


def main() -> None:
    st.set_page_config(page_title="Lead Radar", page_icon="📡", layout="wide")
    load_dotenv(find_dotenv(usecwd=True))
    st.title("Lead Radar")
    st.caption(
        "Find local home-service businesses with the SerpApi Google Maps API and rank who most "
        "needs help with missed calls, online booking and review follow-up."
    )

    demo, request, use_cache = _sidebar(_has_api_key())
    state = st.session_state
    if request is None:
        return
    signature = f"{demo}|{use_cache}|{request.model_dump_json()}"

    try:
        if demo:
            if st.button("Run demo", type="primary"):
                state["result"] = _run(request, SerpApiGateway(FixtureTransport()), demo=True)
        else:
            if st.button("1. Estimate credits"):
                gateway = _gateway(use_cache)
                state["estimate"] = (
                    signature,
                    pipeline.estimate(request, gateway, ScoringConfig()),
                    gateway.account(),
                )
            estimated = state.get("estimate")
            # An estimate only authorises the exact search it was computed for.
            ready = estimated is not None and estimated[0] == signature
            if ready:
                _show_estimate(estimated[1], estimated[2])
            label = (
                f"2. Run search (up to {estimated[1].total} searches)" if ready else "2. Run search"
            )
            if st.button(label, type="primary", disabled=not ready):
                state["result"] = _run(request, _gateway(use_cache), demo=False)
                state.pop("estimate", None)
    except LeadRadarError as exc:
        st.error(str(exc))
        return

    result = state.get("result")
    if isinstance(result, RunResult):
        _show_result(result)


main()
