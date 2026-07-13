"""Minimal county boundary fetcher for GeoAssess."""
from __future__ import annotations

import logging

import geopandas as gpd
import requests

logger = logging.getLogger(__name__)

_TIGER_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "tigerWMS_Current/MapServer/82/query"
)


def collect_county_boundaries(state_fips: str) -> gpd.GeoDataFrame:
    """Fetch county boundaries for a state from Census TIGERweb.

    Returns a GeoDataFrame with GEOID and NAME columns in EPSG:4326.
    Returns an empty GeoDataFrame if the request fails.
    """
    params = {
        "where": f"STATE='{state_fips}'",
        "outFields": "STATE,COUNTY,NAME,GEOID",
        "returnGeometry": "true",
        "f": "geojson",
    }
    try:
        r = requests.get(_TIGER_URL, params=params, timeout=15)
        r.raise_for_status()
        gdf = gpd.read_file(r.text)
        if "GEOID" not in gdf.columns:
            gdf["GEOID"] = gdf["STATE"].astype(str) + gdf["COUNTY"].astype(str).str.zfill(3)
        return gdf.to_crs("EPSG:4326")
    except Exception as exc:
        logger.warning(f"collect_county_boundaries failed for state={state_fips}: {exc}")
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
