"""Turn a place name such as "Tampa, FL" into precise map coordinates.

Why this exists: given ``location=Tampa, FL``, the Google Maps engine matched the
"Tampa-St Petersburg (Sarasota)" media market and centred the map near Clearwater, so most
"Tampa" results were across the bay. The free Locations API
(https://serpapi.com/locations-api) returns typed candidates (City, Neighborhood, DMA Region,
...) with GPS coordinates. Lead Radar picks the best city and sends an exact
``ll=@lat,lng,zoom`` instead.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}  # fmt: skip

# Most specific first: a city is what someone typing "Tampa, FL" means.
_TYPE_PREFERENCE = ("City", "Municipality", "Neighborhood", "County", "DMA Region")


class ResolvedLocation(BaseModel):
    text: str
    """What the user typed; stays the label everywhere in the reports."""
    canonical_name: str
    target_type: str
    latitude: float
    longitude: float

    def ll(self, zoom: int) -> str:
        return f"@{self.latitude},{self.longitude},{zoom}z"


def split_query(text: str) -> tuple[str, list[str]]:
    """``"St. Petersburg, FL"`` -> ``("St. Petersburg", ["florida"])``.

    The first part is the search term for the Locations API; the rest are qualifiers that a
    candidate's ``canonical_name`` must contain. US state codes are expanded, because
    canonical names spell states out ("Tampa,Florida,United States").
    """
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts:
        return "", []
    qualifiers = [US_STATES.get(part.upper(), part).lower() for part in parts[1:]]
    return parts[0], qualifiers


def pick(text: str, candidates: list[dict[str, Any]]) -> ResolvedLocation | None:
    """Choose the best Locations API candidate for ``text``, or ``None`` if nothing fits.

    A candidate must have the same name as the first part of ``text`` and contain every
    qualifier in its canonical name. Among those, the most specific type wins, then the
    largest ``reach``.
    """
    name, qualifiers = split_query(text)
    if not name:
        return None
    matches: list[tuple[int, int, ResolvedLocation]] = []
    for candidate in candidates:
        gps = candidate.get("gps")
        canonical = str(candidate.get("canonical_name") or "")
        target_type = str(candidate.get("target_type") or "")
        if not (isinstance(gps, list) and len(gps) == 2):
            continue
        if str(candidate.get("name") or "").lower() != name.lower():
            continue
        parts = [part.lower() for part in canonical.split(",")]
        if not all(qualifier in parts for qualifier in qualifiers):
            continue
        rank = (
            _TYPE_PREFERENCE.index(target_type)
            if target_type in _TYPE_PREFERENCE
            else len(_TYPE_PREFERENCE)
        )
        reach = candidate.get("reach")
        resolved = ResolvedLocation(
            text=text,
            canonical_name=canonical,
            target_type=target_type,
            longitude=float(gps[0]),  # the Locations API returns [longitude, latitude]
            latitude=float(gps[1]),
        )
        matches.append((rank, -(reach if isinstance(reach, int) else 0), resolved))
    if not matches:
        return None
    matches.sort(key=lambda match: (match[0], match[1]))
    return matches[0][2]
