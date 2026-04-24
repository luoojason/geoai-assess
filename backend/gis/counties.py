"""
US counties reference dataset loader (Goal: geographic expansion v0.7.0).

Reads `backend/data/us_counties.json` — a flat list of
{state_fips, state_name, county_fips, county_name} entries covering all 3,143
US counties / county-equivalents. Provides:

  lookup_county(state_fips, county_fips) -> dict | None
  list_states() -> list[dict]
  list_counties(state_fips) -> list[dict]
  build_place_name(state_fips, county_fips) -> str   # "Franklin County, Ohio, USA"

Data is loaded once at import time into an in-memory index.
"""

import json
import logging
import requests
from functools import lru_cache
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)

_DATA_FILE = Path(__file__).parent.parent / "data" / "us_counties.json"


class CountyRecord(TypedDict):
    state_fips: str
    state_name: str
    county_fips: str
    county_name: str


@lru_cache(maxsize=1)
def _load() -> list[CountyRecord]:
    if not _DATA_FILE.exists():
        logger.warning(f"us_counties.json not found at {_DATA_FILE}")
        return []
    with open(_DATA_FILE) as f:
        return json.load(f)


@lru_cache(maxsize=1)
def _index_by_fips() -> dict[tuple[str, str], CountyRecord]:
    return {(c["state_fips"], c["county_fips"]): c for c in _load()}


@lru_cache(maxsize=1)
def _states() -> list[dict]:
    """Unique list of states, sorted by state_fips."""
    seen: dict[str, str] = {}
    for c in _load():
        seen.setdefault(c["state_fips"], c["state_name"])
    return [
        {"state_fips": sf, "state_name": sn}
        for sf, sn in sorted(seen.items())
    ]


def lookup_county(state_fips: str, county_fips: str) -> CountyRecord | None:
    """Return the CountyRecord for given FIPS codes, or None if not found."""
    return _index_by_fips().get((state_fips, county_fips))


def list_states() -> list[dict]:
    """Sorted list of {state_fips, state_name} for all US states/territories."""
    return _states()


def list_counties(state_fips: str) -> list[CountyRecord]:
    """Sorted list of counties for a given state_fips, sorted by county_name."""
    return sorted(
        (c for c in _load() if c["state_fips"] == state_fips),
        key=lambda c: c["county_name"],
    )


def build_place_name(state_fips: str, county_fips: str) -> str:
    """
    Construct an OSMnx-compatible place_name for the given county FIPS.
    Raises ValueError if the FIPS codes don't match a known county.
    """
    rec = lookup_county(state_fips, county_fips)
    if rec is None:
        raise ValueError(
            f"Unknown county FIPS: state={state_fips}, county={county_fips}. "
            "Check backend/data/us_counties.json."
        )
    return f"{rec['county_name']}, {rec['state_name']}, USA"


@lru_cache(maxsize=256)
def centroid_of_county(state_fips: str, county_fips: str) -> tuple[float, float]:
    """
    Return a (lat, lon) centroid for the given county FIPS pair.

    Implementation choice: look up at call time against the Census TIGERweb
    county-level layer (MapServer/84) with a small in-memory LRU cache (256
    entries, far more than any single demo session needs).  This avoids adding
    bbox data to us_counties.json while staying offline-independent for the
    rehearsal counties after the first warm-up call.

    Fallback cascade:
      1. TIGERweb county geometry → centroid of the returned polygon extent.
      2. If the network call fails (timeout, HTTP error, empty result), fall
         back to a hardcoded table of the three rehearsal counties so demoing
         Wayne MI (26163) and Cuyahoga OH (39035) never degrades to Columbus.
      3. If the county is not in the rehearsal table either, return Columbus
         (39.9612, -82.9988) — same as the old hardcoded value — and WARN so
         the caller knows.

    Rationale: the render path is called after the pipeline has already made
    several Census/TIGER network requests, so a single extra call is acceptable.
    The lru_cache makes subsequent same-county requests free.
    """
    # Hardcoded fallback table for critical rehearsal counties (lat, lon).
    # Derived from county seat / geographic centroid (WGS-84).
    _REHEARSAL_FALLBACK: dict[tuple[str, str], tuple[float, float]] = {
        ("39", "049"): (39.9612, -82.9988),   # Franklin, OH (Columbus)
        ("39", "035"): (41.4993, -81.6944),   # Cuyahoga, OH (Cleveland)
        ("26", "163"): (42.3223, -83.1763),   # Wayne, MI (Detroit area)
    }

    tiger_url = (
        "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
        "tigerWMS_Current/MapServer/84/query"
        f"?where=STATE='{state_fips}'+AND+COUNTY='{county_fips}'"
        "&outFields=STATE,COUNTY"
        "&returnGeometry=true"
        "&returnExtentOnly=true"
        "&f=json"
    )
    try:
        r = requests.get(tiger_url, timeout=10)
        r.raise_for_status()
        payload = r.json()
        ext = payload.get("extent") or {}
        if ext and all(k in ext for k in ("xmin", "ymin", "xmax", "ymax")):
            lat = (ext["ymin"] + ext["ymax"]) / 2.0
            lon = (ext["xmin"] + ext["xmax"]) / 2.0
            logger.debug(
                f"centroid_of_county({state_fips}, {county_fips}): "
                f"({lat:.4f}, {lon:.4f}) from TIGERweb"
            )
            return (lat, lon)
        logger.warning(
            f"centroid_of_county: TIGERweb returned no extent for "
            f"state={state_fips} county={county_fips}; using fallback table"
        )
    except Exception as exc:
        logger.warning(
            f"centroid_of_county: TIGERweb request failed "
            f"({exc!r}); using fallback table"
        )

    key = (state_fips, county_fips)
    if key in _REHEARSAL_FALLBACK:
        return _REHEARSAL_FALLBACK[key]

    logger.warning(
        f"centroid_of_county: no fallback for state={state_fips} "
        f"county={county_fips}; returning Columbus default"
    )
    return (39.9612, -82.9988)
