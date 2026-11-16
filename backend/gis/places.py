"""Cities and towns as study areas (Prof. Shi 2026-11-13 #17).

"Gary, Indiana" is a Census incorporated place, not a county subdivision, a ZIP
code or a neighborhood, so Find with AI could not delineate it. The place
boundary comes from Census TIGERweb (Places layer) and the block groups that lie
mostly inside it become the project area — the same building block a
neighborhood uses. ``units_mostly_inside`` is shared with the uploaded study-area
polygon (#12).
"""
from __future__ import annotations

import logging
import re

import requests

logger = logging.getLogger(__name__)

_PLACES = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "Places_CouSub_ConCity_SubMCD/MapServer"
)
# Census 2020 layers: incorporated places first, then census designated places.
_PLACE_LAYERS = ((25, "incorporated place"), (26, "census designated place"))

_SUFFIX = re.compile(r"\b(city|town|village|borough|township|cdp|municipality)\b", re.I)


def base_name(text: str) -> str:
    """'Gary city' / 'City of Gary' -> 'Gary'."""
    t = re.sub(r"\b(city|town|village) of\b", "", text or "", flags=re.I)
    t = _SUFFIX.sub("", t)
    return re.sub(r"\s+", " ", t).strip(" ,.")


def fetch_place(state_fips: str, name: str):
    """The Census place called `name` in the state, as a GeoDataFrame (EPSG:4326) with
    NAME and kind columns, or None when there is no such place."""
    import geopandas as gpd

    base = base_name(name)
    if not base:
        return None
    safe = base.replace("'", "''")
    for layer, kind in _PLACE_LAYERS:
        params = {
            "where": f"STATE='{state_fips}' AND UPPER(BASENAME)=UPPER('{safe}')",
            "outFields": "GEOID,NAME,BASENAME",
            "returnGeometry": "true", "outSR": "4326", "f": "geojson",
        }
        try:
            r = requests.get(f"{_PLACES}/{layer}/query", params=params, timeout=60)
            r.raise_for_status()
            gdf = gpd.read_file(r.text)
        except Exception as exc:   # network or service trouble: a miss, said plainly by the caller
            logger.warning("place lookup %s/%s failed: %r", state_fips, name, exc)
            continue
        if not gdf.empty:
            gdf = gdf[["GEOID", "NAME", "geometry"]].copy()
            gdf["kind"] = kind
            return gdf.to_crs("EPSG:4326")
    return None


def units_mostly_inside(units, geometry, min_fraction: float = 0.5) -> list[str]:
    """GEOIDs of the units with more than `min_fraction` of their own area inside
    `geometry` (a shapely geometry or GeoSeries in EPSG:4326)."""
    import geopandas as gpd

    if hasattr(geometry, "union_all"):
        poly = geometry.union_all()
    elif hasattr(geometry, "unary_union"):
        poly = geometry.unary_union
    else:
        poly = geometry
    u = units.to_crs("EPSG:5070")
    p = gpd.GeoSeries([poly], crs="EPSG:4326").to_crs("EPSG:5070").iloc[0]
    out = []
    for geoid, geom in zip(u["GEOID"], u.geometry):
        if geom is None or geom.is_empty or not geom.intersects(p):
            continue
        area = geom.area
        if area > 0 and geom.intersection(p).area / area > min_fraction:
            out.append(str(geoid))
    return out
