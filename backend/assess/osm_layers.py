"""OSM data fetchers for the assessment pipeline.

Each function accepts a bbox as (west, south, east, north) in EPSG:4326
(lon_min, lat_min, lon_max, lat_max) and returns a GeoDataFrame in the
requested projected CRS. Returns an empty GeoDataFrame (never raises) when
no features are found for the area.
"""
from __future__ import annotations

import logging
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
            raise  # the mirror itself failed; a second attempt buys nothing
        with _failover_lock:
            if not _failed_over:
                logger.warning(
                    f"Overpass primary failed ({exc!r}) — failing over to {_OVERPASS_MIRROR}"
                )
                _failed_over = True
            ox.settings.overpass_url = _OVERPASS_MIRROR
        return call()


def _features(bbox, tags):
    return _with_overpass_failover(lambda: ox.features_from_bbox(bbox=bbox, tags=tags))


def _graph(bbox, network_type):
    return _with_overpass_failover(
        lambda: ox.graph_from_bbox(bbox=bbox, network_type=network_type)
    )


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
        graph = _graph(bbox, network_type)
        _, edges = ox.graph_to_gdfs(graph)
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
