"""Project areas smaller than a county, built from Census units.

Prof. Shi and Prof. Wang, 2026-09-16 (#1, "Define the community"):

  Currently, the spatial scope is fixed at the county level. Wished improvement:
  allow the user to flexibly define the spatial scope —
  - In the REPORTING UNIT, add an option "Customized (community, neighborhood, etc.)".
  - Define the project area that matches the user-specified area:
    - a unit with a defined Census boundary (county, town, ZIP Code, tract, block
      group) uses that boundary directly;
    - a community or neighborhood name asks AI to recommend block groups, so block
      groups are the building blocks of the project area.
  - Allow the user to interactively add or remove block groups.

So a project area is always a set of Census units of ONE kind, identified by
GEOID, inside (or, for a ZIP code, overlapping) one county. The union of their
polygons is the study area every later step measures. Nothing here decides a
boundary on its own: the AI proposes, the analyst's selection is what is stored.
"""
from __future__ import annotations

import logging
import math

import requests

logger = logging.getLogger(__name__)

_TIGER_2020 = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "tigerWMS_Census2020/MapServer"
)
# 2020 Census layers carrying POP100 (the decennial count). The geometry itself
# comes from the Current service via profile.fetch_areal_unit_geometries, whose
# tract and block-group GEOIDs are the 2020 ones until the 2030 census.
_POP_LAYERS = {"tract": 6, "block_group": 8, "county_subdivision": 20, "zcta": 84}

# The unit a "Customized" area is assembled from.
CUSTOM_BUILDING_BLOCK = "block_group"

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_USER_AGENT = "HCAS/1.0 (Healthy Community Assessment System; +https://github.com/luoojason)"


def geometry_unit(unit: str) -> str:
    """The Census unit whose polygons make up an area of this reporting unit."""
    return CUSTOM_BUILDING_BLOCK if unit == "custom" else unit


def unit_populations(state_fips: str, county_fips: str, unit: str,
                     geoids: list[str] | None = None) -> dict[str, int]:
    """{GEOID: 2020 Census population} for a county's units. Empty on failure —
    a missing population is reported as unknown, never as zero."""
    unit = geometry_unit(unit)
    layer = _POP_LAYERS.get(unit)
    if layer is None:
        return {}
    if unit == "zcta":
        if not geoids:
            return {}
        where = "GEOID IN (" + ",".join(f"'{g}'" for g in geoids if g.isdigit()) + ")"
    else:
        where = f"GEOID LIKE '{state_fips}{county_fips}%'"
    try:
        r = requests.get(
            f"{_TIGER_2020}/{layer}/query",
            params={"where": where, "outFields": "GEOID,POP100", "returnGeometry": "false",
                    "f": "json", "resultRecordCount": 10000},
            timeout=40,
        )
        r.raise_for_status()
        out = {}
        for f in r.json().get("features") or []:
            a = f.get("attributes") or {}
            if a.get("GEOID") and a.get("POP100") is not None:
                out[str(a["GEOID"])] = int(a["POP100"])
        return out
    except Exception as exc:
        logger.warning(f"unit_populations({unit}) failed: {exc!r}")
        return {}


def select(units, geoids):
    """The chosen units, in the order given. Raises ValueError naming any GEOID
    that is not a unit of this county — a stale or mistyped selection must never
    quietly shrink the area the analyst believes they chose."""
    wanted = [str(g) for g in geoids]
    have = set(units["GEOID"].astype(str))
    missing = [g for g in wanted if g not in have]
    if missing:
        raise ValueError(
            f"{len(missing)} of the selected areas are not in this county "
            f"({', '.join(missing[:5])}{'…' if len(missing) > 5 else ''}). "
            "Re-open the project definition and select them again."
        )
    sub = units[units["GEOID"].astype(str).isin(wanted)].copy()
    order = {g: i for i, g in enumerate(wanted)}
    return sub.sort_values(by="GEOID", key=lambda s: s.astype(str).map(order))


def union_area(units):
    """One-row GeoDataFrame (EPSG:5070) holding the union of `units`."""
    import geopandas as gpd

    proj = units.to_crs("EPSG:5070")
    geom = proj.geometry.union_all()
    return gpd.GeoDataFrame(geometry=[geom], crs="EPSG:5070")


def describe_selection(units, populations: dict[str, int], unit_label: str) -> dict:
    """Area, population and count for a selection — the figures step 1 shows."""
    area_km2 = round(float(units.to_crs("EPSG:5070").geometry.area.sum()) / 1e6, 2)
    ids = [str(g) for g in units["GEOID"]]
    known = [populations[g] for g in ids if g in populations]
    out = {"count": len(ids), "area_km2": area_km2, "unit_label": unit_label}
    if ids and len(known) == len(ids):
        out["population"] = int(sum(known))
        out["population_source"] = "2020 Census (decennial count)"
    elif known:
        out["population_partial"] = int(sum(known))
        out["population_note"] = (
            f"2020 Census population found for {len(known)} of {len(ids)} areas; "
            "the total is therefore a lower bound."
        )
    return out


# --- AI-recommended block groups ------------------------------------------------

AREA_SUGGEST_SYSTEM = (
    "You help define the extent of a named community inside one US county, so it can "
    "be assembled from Census block groups. Given the community's name or description "
    "and the county, return: `place`, the community's usual name; `geocode_query`, a "
    "query an OpenStreetMap geocoder will match (name, city, state); and `bbox`, your "
    "best estimate of the community's extent as WGS84 degrees. Keep the box to the "
    "community itself, not the whole city or county, and for a corridor or district "
    "keep it tight around the core rather than including the neighborhoods beside it: "
    "the analyst can add block groups on a map far more easily than find the ones that "
    "do not belong. If the description names a "
    "landmark or institution rather than a community, give the surrounding "
    "neighborhood a resident would mean. Say in `caveat` when the extent is uncertain "
    "or the community has no agreed boundary."
)

AREA_SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "place": {"type": "string"},
        "geocode_query": {"type": "string"},
        "bbox": {
            "type": "object",
            "properties": {
                "west": {"type": "number"}, "south": {"type": "number"},
                "east": {"type": "number"}, "north": {"type": "number"},
            },
            "required": ["west", "south", "east", "north"],
            "additionalProperties": False,
        },
        "interpretation": {"type": "string"},
        "caveat": {"type": "string"},
    },
    "required": ["place", "geocode_query", "bbox", "interpretation", "caveat"],
    "additionalProperties": False,
}


def nominatim_search(query: str, viewbox) -> list[dict]:
    """OpenStreetMap's geocoder, limited to the county's box. Empty on failure."""
    west, south, east, north = viewbox
    try:
        r = requests.get(
            _NOMINATIM,
            params={"q": query, "format": "jsonv2", "polygon_geojson": 1, "limit": 5,
                    "viewbox": f"{west},{north},{east},{south}", "bounded": 1},
            headers={"User-Agent": _USER_AGENT},
            timeout=20,
        )
        r.raise_for_status()
        return r.json() or []
    except Exception as exc:
        logger.warning(f"nominatim search failed: {exc!r}")
        return []


def _valid_bbox(b: dict, county_bbox) -> tuple | None:
    try:
        w, s, e, n = (float(b["west"]), float(b["south"]), float(b["east"]), float(b["north"]))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (w, s, e, n)) or w >= e or s >= n:
        return None
    cw, cs, ce, cn = county_bbox
    # Clip to the county box; a box entirely outside it is no suggestion at all.
    w, s, e, n = max(w, cw), max(s, cs), min(e, ce), min(n, cn)
    return (w, s, e, n) if w < e and s < n else None


def suggest_block_groups(text: str, county_label: str, county_bbox, block_groups,
                         llm, geocode=nominatim_search) -> dict:
    """Recommend the block groups that make up a named community.

    Two opinions, in order of authority: an OpenStreetMap boundary for the place
    when one exists (German Village in Columbus is mapped as a polygon), else the
    AI's own estimate of its extent (the Short North is only a point in OSM).
    A block group is recommended when its interior point falls inside that
    shape; if none does — a community smaller than one block group — the block
    group containing the shape's centre is recommended alone.

    `llm(system, prompt, schema)` -> (dict, usage). `block_groups` is the county's
    block groups in any CRS with GEOID and geometry. Returns GEOIDs plus the
    reasoning the analyst needs to judge them; the selection itself is theirs.
    """
    import geopandas as gpd
    from shapely.geometry import box, shape

    text = (text or "").strip()
    if not text:
        raise ValueError("Type the community or neighborhood name first.")
    draft, _usage = llm(
        AREA_SUGGEST_SYSTEM,
        f"Community: {text}\nCounty: {county_label}\n"
        f"County extent (WGS84 west, south, east, north): "
        + ", ".join(f"{c:.4f}" for c in county_bbox),
        AREA_SUGGEST_SCHEMA,
    )
    ai_box = _valid_bbox(draft.get("bbox") or {}, county_bbox)

    shape_geom, method = None, None
    cw, cs, ce, cn = county_bbox
    county_area = (ce - cw) * (cn - cs)
    for hit in geocode(draft.get("geocode_query") or text, county_bbox):
        gj = hit.get("geojson") or {}
        if gj.get("type") in ("Polygon", "MultiPolygon"):
            g = shape(gj)
            # A polygon the size of the county is the county (or the city), not
            # the community the analyst named.
            minx, miny, maxx, maxy = g.bounds
            if (maxx - minx) * (maxy - miny) < 0.6 * county_area:
                shape_geom, method = g, "osm_boundary"
                break
    if shape_geom is None and ai_box is not None:
        shape_geom, method = box(*ai_box), "ai_extent"
    if shape_geom is None:
        return {
            "geoids": [], "place": draft.get("place") or text,
            "interpretation": draft.get("interpretation", ""),
            "caveat": (draft.get("caveat") or "")
                      + " The community could not be located inside this county, so no block "
                        "groups were recommended — select them on the map instead.",
            "method": None,
        }

    bgs = block_groups.to_crs("EPSG:4326")
    reps = bgs.geometry.representative_point()
    inside = bgs[reps.within(shape_geom)]
    if inside.empty:
        centre = shape_geom.centroid
        inside = bgs[bgs.geometry.contains(centre)]
    geoids = [str(g) for g in inside["GEOID"]]
    method_text = {
        "osm_boundary": "matched to the community's boundary in OpenStreetMap",
        "ai_extent": "matched to the AI's estimate of the community's extent "
                     "(OpenStreetMap has no boundary for it)",
    }[method]
    return {
        "geoids": geoids,
        "place": draft.get("place") or text,
        "interpretation": draft.get("interpretation", ""),
        "caveat": draft.get("caveat", ""),
        "method": method,
        "method_text": method_text,
        "outline": gpd.GeoSeries([shape_geom], crs="EPSG:4326").__geo_interface__["features"][0]["geometry"],
    }
