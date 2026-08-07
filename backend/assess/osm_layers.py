"""OSM data fetchers for the assessment pipeline.

Each function accepts a bbox as (west, south, east, north) in EPSG:4326
(lon_min, lat_min, lon_max, lat_max) and returns a GeoDataFrame in the
requested projected CRS. Returns an empty GeoDataFrame (never raises) when
no features are found for the area.
"""
from __future__ import annotations

import logging
import math
import os
import threading

import geopandas as gpd
import osmnx as ox
from osmnx._errors import InsufficientResponseError

from backend.assess.netdiag import record_fetch_error

logger = logging.getLogger(__name__)

# Prof. Shi's field test (2026-07-20): overpass-api.de was unreachable from his
# network while other hosts worked. On the first network failure we switch the
# whole process to the mirror and retry — sticky, so later fetchers start there.
_OVERPASS_PRIMARY = "https://overpass-api.de/api"
_OVERPASS_MIRROR = "https://overpass.kumi.systems/api"
_failover_lock = threading.Lock()
_failed_over = False

# --- request budget ---------------------------------------------------------
# osmnx defaults to a 180 s socket timeout and osmnx>=2 retries, so ONE hung
# layer could hold a step for many minutes with nothing the user could do. Since
# 2026-07-27 the study area is the whole county (Prof. Shi's request), which made
# every Overpass query 10-60x larger than the old 5 km disc and turned that
# latent risk into his 2026-08-07 report: "after the data are fetched, the
# process cannot move on". Bound it here, and again per layer in hcas.py.
OSM_TIMEOUT_S = int(os.environ.get("OSM_TIMEOUT_S", "90"))

# overpass.kumi.systems answers a generic client with HTTP 429 and the body
# "Please include a meaningful User-Agent string with your requests to avoid
# rate-limiting" (verified 2026-08-07), so the failover mirror was rejecting us
# on arrival. Identify the app.
_USER_AGENT = "HCAS/1.0 (Healthy Community Assessment System; +https://github.com/luoojason)"

# osmnx interpolates requests_timeout into the Overpass `[timeout:…]` header too
# (_overpass._make_overpass_settings), so one setting bounds both the socket and
# the server-side query.
ox.settings.requests_timeout = OSM_TIMEOUT_S
ox.settings.http_user_agent = _USER_AGENT


# 12 s, not 6: measured on a flaky link 2026-08-07, overpass-api.de answered a
# one-node probe in 8.0 s while the mirror timed out at 10.4 s — and ten minutes
# earlier the two had swapped places. A probe tight enough to misjudge a working
# endpoint is worse than no probe, since it would send every query to a mirror
# that is not necessarily better.
_PROBE_TIMEOUT_S = float(os.environ.get("OSM_PROBE_TIMEOUT_S", "12"))
_probed = False


def _probe_overpass_once() -> None:
    """Pick a reachable Overpass endpoint before the first real query.

    Failing over on the real query costs a full `requests_timeout` first — 90 s
    of connect timeout on a network that cannot reach overpass-api.de at all
    (measured on a Windows client 2026-08-07), paid before a single byte of data
    is requested. A 6 s probe answers the same question.
    """
    global _probed, _failed_over
    with _failover_lock:
        if _probed:
            return
        _probed = True
    if _failed_over:
        return
    import requests

    try:
        # A tiny REAL query against /interpreter, not /status: on the machine
        # this was measured on, /status answered inside the probe window while
        # /interpreter still connect-timed out at 90 s, so probing the cheap
        # endpoint proved nothing about the one that matters.
        r = requests.post(
            f"{ox.settings.overpass_url}/interpreter",
            data={"data": "[out:json][timeout:5];node(0,0,0.001,0.001);out 1;"},
            headers={"User-Agent": _USER_AGENT},
            timeout=_PROBE_TIMEOUT_S,
        )
        if r.status_code < 500:
            return
        reason = f"HTTP {r.status_code}"
    except Exception as exc:
        reason = repr(exc)
    logger.warning(
        f"Overpass primary unreachable in {_PROBE_TIMEOUT_S:g}s ({reason}) — "
        f"starting on {_OVERPASS_MIRROR}"
    )
    with _failover_lock:
        ox.settings.overpass_url = _OVERPASS_MIRROR
        _failed_over = True


def _with_overpass_failover(call):
    """Run an osmnx call; on a network failure, fail over to the Overpass
    mirror once and retry. InsufficientResponseError (a genuinely-empty query
    result) passes straight through — that is not a network problem.

    Retries are decided by which endpoint THIS call used, not by whether some
    other thread has already switched. Step 3 fetches layers concurrently, so
    the old check ("has anyone failed over yet?") let the first failure switch
    the URL and every sibling already in flight against the primary then
    re-raised without ever trying the mirror — three layers died on the primary
    in the same millisecond in the 2026-08-07 reproduction.
    """
    global _failed_over
    used = ox.settings.overpass_url
    try:
        return call()
    except InsufficientResponseError:
        raise
    except Exception as exc:
        if used == _OVERPASS_MIRROR:
            # The mirror failed. Point back at the primary so the NEXT tile tries
            # it rather than hammering an endpoint we now have evidence against —
            # neither host is reliably the good one. Measured 2026-08-07 within
            # ten minutes on the same link: primary 8.0 s / mirror timeout, then
            # primary timeout / mirror 3.1 s, then primary 22 s / mirror timeout.
            # A one-way switch strands the whole run on whichever host happened
            # to be bad second. This call still fails; retrying it here would
            # just repeat the wait we already paid.
            with _failover_lock:
                if _failed_over:
                    logger.warning(
                        f"Overpass mirror failed too ({exc!r}) — next request goes "
                        f"back to {_OVERPASS_PRIMARY}"
                    )
                    ox.settings.overpass_url = _OVERPASS_PRIMARY
                    _failed_over = False
            raise
        with _failover_lock:
            if not _failed_over:
                logger.warning(
                    f"Overpass primary failed ({exc!r}) — failing over to {_OVERPASS_MIRROR}"
                )
                _failed_over = True
            ox.settings.overpass_url = _OVERPASS_MIRROR
        return call()


# --- adaptive tiling -------------------------------------------------------
# A whole-county Overpass query is often too big to answer at all. Measured
# 2026-08-07 against overpass.kumi.systems for Grand Isle County VT (one of the
# smallest counties in the country, 212 km2 of land):
#
#   whole county bbox, buildings : FAILED after 103 s (truncated response)
#   same area as 4 quadrants     : 2.0 s + 1.8 s + 5.6 s + 6.9 s -> 6,086 buildings
#
# So the query is not slow, it is too large; splitting it makes the same data
# arrive in a sixth of the time we used to spend failing. Prof. Shi's county-wide
# study area (his 2026-07-27 request) is what pushed every layer over that edge,
# and no timeout tuning can fix it — a bounded wait just fails faster.
#
# Split adaptively rather than on a fixed grid: a rural county answers in one
# request, and only the tiles that actually fail are subdivided, so a dense urban
# county pays for its density and nothing else does.
OSM_TILE_MAX_DEPTH = int(os.environ.get("OSM_TILE_MAX_DEPTH", "3"))  # <= 64 tiles per start tile

# Discovering "too big" by waiting for a timeout costs a full OSM_TIMEOUT_S per
# layer before any splitting starts, and a county is reliably too big — so cut
# the area to start tiles up front and only *adaptively* split from there. 0.2°
# is ~16 x 22 km at 44°N; the quadrants that answered in 2-7 s during the
# measurement above were 0.10 x 0.22°, so this is the same order.
OSM_TILE_MAX_DEG = float(os.environ.get("OSM_TILE_MAX_DEG", "0.2"))


def _start_tiles(bbox):
    """Split a bbox into pieces no larger than OSM_TILE_MAX_DEG on either axis."""
    west, south, east, north = bbox
    nx = max(1, math.ceil((east - west) / OSM_TILE_MAX_DEG))
    ny = max(1, math.ceil((north - south) / OSM_TILE_MAX_DEG))
    if nx == 1 and ny == 1:
        return [tuple(bbox)]
    dx, dy = (east - west) / nx, (north - south) / ny
    return [
        (west + i * dx, south + j * dy, west + (i + 1) * dx, south + (j + 1) * dy)
        for i in range(nx)
        for j in range(ny)
    ]


def _tiled(bbox, call_one, depth: int = 0):
    """Run `call_one(bbox)` and, if it fails, quarter the bbox and retry each
    part. Returns a list of results (never raises unless depth is exhausted).

    InsufficientResponseError means the tile is genuinely empty — that is an
    answer, not a failure, so it is not split further.
    """
    if depth == 0:
        _probe_overpass_once()
        tiles = _start_tiles(bbox)
        if len(tiles) > 1:
            logger.info(f"Overpass: {bbox} is large — starting at {len(tiles)} tiles")
            out, failures = [], []
            for t in tiles:
                try:
                    out.extend(_tiled(t, call_one, depth))
                except Exception as exc:
                    failures.append(exc)
            if failures and not out:
                raise failures[0]
            if failures:
                logger.warning(
                    f"{len(failures)} of {len(tiles)} start tiles failed; keeping the rest"
                )
            return out
    west, south, east, north = bbox
    try:
        return [call_one(bbox)]
    except InsufficientResponseError:
        return []
    except Exception:
        if depth >= OSM_TILE_MAX_DEPTH:
            raise
        mid_x, mid_y = (west + east) / 2, (south + north) / 2
        quads = [
            (west, south, mid_x, mid_y), (mid_x, south, east, mid_y),
            (west, mid_y, mid_x, north), (mid_x, mid_y, east, north),
        ]
        logger.info(
            f"Overpass query too large for {bbox} — splitting into 4 (depth {depth + 1})"
        )
        out = []
        failures = []
        for q in quads:
            try:
                out.extend(_tiled(q, call_one, depth + 1))
            except Exception as exc:  # keep the tiles that did answer
                failures.append(exc)
        if failures and not out:
            raise failures[0]
        if failures:
            logger.warning(
                f"{len(failures)} of 4 sub-tiles failed under {bbox}; keeping the rest"
            )
        return out


def _combine(parts, crs_hint=None):
    """Concatenate tile results and drop features seen in more than one tile
    (anything crossing a tile edge is returned by both)."""
    parts = [p for p in parts if p is not None and len(p)]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    import pandas as pd

    combined = pd.concat(parts)
    combined = combined[~combined.index.duplicated(keep="first")]
    return gpd.GeoDataFrame(combined, geometry="geometry", crs=parts[0].crs or crs_hint)


def _features(bbox, tags):
    parts = _tiled(
        bbox,
        lambda b: _with_overpass_failover(lambda: ox.features_from_bbox(bbox=b, tags=tags)),
    )
    out = _combine(parts)
    return out if out is not None else gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")


def _graph(bbox, network_type):
    """Edges only — the callers all want `graph_to_gdfs(...)[1]`, and edges are
    what can be merged across tiles (a graph cannot be stitched meaningfully at a
    tile boundary, but its edge geometries can)."""
    def one(b):
        graph = _with_overpass_failover(
            lambda: ox.graph_from_bbox(bbox=b, network_type=network_type)
        )
        return ox.graph_to_gdfs(graph)[1]

    out = _combine(_tiled(bbox, one))
    return out if out is not None else gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")


_BUILDING_TAGS = {"building": True}

_GREENSPACE_TAGS = {
    "leisure": ["park", "garden", "nature_reserve", "recreation_ground"],
    "landuse": ["grass", "park", "recreation_ground", "forest", "meadow", "village_green"],
}

_POLYGON_GEOM_TYPES = {"Polygon", "MultiPolygon"}


def fetch_buildings(
    bbox: tuple[float, float, float, float],
    crs: str = "EPSG:5070",
) -> gpd.GeoDataFrame:
    """Fetch building footprints within bbox and reproject to crs.

    bbox: (west, south, east, north) in EPSG:4326.
    """
    try:
        gdf = _features(bbox, _BUILDING_TAGS)
        gdf = gdf[gdf.geometry.geom_type.isin(_POLYGON_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf.to_crs(crs)
    except InsufficientResponseError:
        return _empty_gdf(crs)  # empty query result, not a network problem
    except Exception as exc:
        record_fetch_error("fetch_buildings", exc)
        logger.warning(f"fetch_buildings failed: {exc} — returning empty GeoDataFrame")
        return _empty_gdf(crs)


def fetch_greenspace(
    bbox: tuple[float, float, float, float],
    crs: str = "EPSG:5070",
) -> gpd.GeoDataFrame:
    """Fetch parks and green areas within bbox and reproject to crs.

    bbox: (west, south, east, north) in EPSG:4326.
    Only polygon geometries are retained (excludes point amenity tags).
    """
    try:
        gdf = _features(bbox, _GREENSPACE_TAGS)
        gdf = gdf[gdf.geometry.geom_type.isin(_POLYGON_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf.to_crs(crs)
    except InsufficientResponseError:
        return _empty_gdf(crs)  # empty query result, not a network problem
    except Exception as exc:
        record_fetch_error("fetch_greenspace", exc)
        logger.warning(f"fetch_greenspace failed: {exc} — returning empty GeoDataFrame")
        return _empty_gdf(crs)


def fetch_road_network(
    bbox: tuple[float, float, float, float],
    crs: str = "EPSG:5070",
    network_type: str = "drive",
) -> gpd.GeoDataFrame:
    """Fetch road network edges within bbox and reproject to crs.

    bbox: (west, south, east, north) in EPSG:4326.
    Returns the edges GeoDataFrame from the osmnx graph.
    """
    try:
        edges = _graph(bbox, network_type)  # already the edges GeoDataFrame
        if edges.empty:
            return _empty_gdf(crs)
        return edges.to_crs(crs)
    except InsufficientResponseError:
        return _empty_gdf(crs)  # empty query result, not a network problem
    except Exception as exc:
        record_fetch_error("fetch_road_network", exc)
        logger.warning(f"fetch_road_network failed: {exc} — returning empty GeoDataFrame")
        return _empty_gdf(crs)


_PARKING_TAGS = {"amenity": "parking", "parking": True}

_SIDEWALK_TAGS = {
    "highway": ["footway", "path", "pedestrian", "sidewalk"],
}

_TRANSIT_TAGS = {
    "public_transport": ["stop_position", "platform"],
    "highway": "bus_stop",
    "railway": ["station", "halt", "tram_stop"],
}

_LINE_GEOM_TYPES = {"LineString", "MultiLineString"}
_POINT_GEOM_TYPES = {"Point", "MultiPoint"}


def fetch_parking(
    bbox: tuple[float, float, float, float],
    crs: str = "EPSG:5070",
) -> gpd.GeoDataFrame:
    """Fetch parking areas within bbox and reproject to crs.

    bbox: (west, south, east, north) in EPSG:4326.
    Only polygon geometries are retained.
    """
    try:
        gdf = _features(bbox, _PARKING_TAGS)
        gdf = gdf[gdf.geometry.geom_type.isin(_POLYGON_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf.to_crs(crs)
    except InsufficientResponseError:
        return _empty_gdf(crs)  # empty query result, not a network problem
    except Exception as exc:
        record_fetch_error("fetch_parking", exc)
        logger.warning(f"fetch_parking failed: {exc} — returning empty GeoDataFrame")
        return _empty_gdf(crs)


def fetch_sidewalks(
    bbox: tuple[float, float, float, float],
    crs: str = "EPSG:5070",
) -> gpd.GeoDataFrame:
    """Fetch sidewalk and pedestrian path edges within bbox and reproject to crs.

    bbox: (west, south, east, north) in EPSG:4326.
    Only LineString geometries are retained.
    """
    try:
        gdf = _features(bbox, _SIDEWALK_TAGS)
        gdf = gdf[gdf.geometry.geom_type.isin(_LINE_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf.to_crs(crs)
    except InsufficientResponseError:
        return _empty_gdf(crs)  # empty query result, not a network problem
    except Exception as exc:
        record_fetch_error("fetch_sidewalks", exc)
        logger.warning(f"fetch_sidewalks failed: {exc} — returning empty GeoDataFrame")
        return _empty_gdf(crs)


def fetch_transit_stops(
    bbox: tuple[float, float, float, float],
    crs: str = "EPSG:5070",
) -> gpd.GeoDataFrame:
    """Fetch transit stops within bbox and reproject to crs.

    bbox: (west, south, east, north) in EPSG:4326.
    Only Point geometries are retained.
    """
    try:
        gdf = _features(bbox, _TRANSIT_TAGS)
        gdf = gdf[gdf.geometry.geom_type.isin(_POINT_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf.to_crs(crs)
    except InsufficientResponseError:
        return _empty_gdf(crs)  # empty query result, not a network problem
    except Exception as exc:
        record_fetch_error("fetch_transit_stops", exc)
        logger.warning(f"fetch_transit_stops failed: {exc} — returning empty GeoDataFrame")
        return _empty_gdf(crs)


_LANDUSE_TAGS = {"landuse": True}


def fetch_landuse(
    bbox: tuple[float, float, float, float],
    crs: str = "EPSG:5070",
) -> gpd.GeoDataFrame:
    """Fetch all landuse polygons within bbox and reproject to crs.

    bbox: (west, south, east, north) in EPSG:4326.
    Only polygon geometries are retained; the 'landuse' tag column is kept.
    """
    try:
        gdf = _features(bbox, _LANDUSE_TAGS)
        gdf = gdf[gdf.geometry.geom_type.isin(_POLYGON_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf.to_crs(crs)
    except InsufficientResponseError:
        return _empty_gdf(crs)  # empty query result, not a network problem
    except Exception as exc:
        record_fetch_error("fetch_landuse", exc)
        logger.warning(f"fetch_landuse failed: {exc} — returning empty GeoDataFrame")
        return _empty_gdf(crs)


_FACILITY_TAGS = {
    "amenity": [
        "hospital", "clinic", "doctors", "pharmacy",
        "school", "kindergarten", "library", "community_centre",
    ],
    "shop": ["supermarket", "greengrocer", "convenience"],
}


def fetch_facilities(
    bbox: tuple[float, float, float, float],
    crs: str = "EPSG:5070",
) -> gpd.GeoDataFrame:
    """Fetch community facilities (health, education, food, civic) within bbox.

    bbox: (west, south, east, north) in EPSG:4326.
    Polygon footprints are reduced to centroids so the layer is uniformly
    point-typed. (ponytail: centroid ~ entrance at neighborhood scale)
    """
    try:
        gdf = _features(bbox, _FACILITY_TAGS)
        if gdf.empty:
            return _empty_gdf(crs)
        gdf = gdf.to_crs(crs)
        poly = gdf.geometry.geom_type.isin(_POLYGON_GEOM_TYPES)
        gdf.loc[poly, "geometry"] = gdf.loc[poly].geometry.centroid
        gdf = gdf[gdf.geometry.geom_type.isin(_POINT_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf
    except InsufficientResponseError:
        return _empty_gdf(crs)  # empty query result, not a network problem
    except Exception as exc:
        record_fetch_error("fetch_facilities", exc)
        logger.warning(f"fetch_facilities failed: {exc} — returning empty GeoDataFrame")
        return _empty_gdf(crs)


def _empty_gdf(crs: str) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(geometry=[], crs=crs)
