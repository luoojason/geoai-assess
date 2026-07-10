"""OSM data fetchers for the assessment pipeline.

Each function accepts a bbox as (west, south, east, north) in EPSG:4326
(lon_min, lat_min, lon_max, lat_max) and returns a GeoDataFrame in the
requested projected CRS. Returns an empty GeoDataFrame (never raises) when
no features are found for the area.
"""
from __future__ import annotations

import logging

import geopandas as gpd
import osmnx as ox

logger = logging.getLogger(__name__)

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
        gdf = ox.features_from_bbox(bbox=bbox, tags=_BUILDING_TAGS)
        gdf = gdf[gdf.geometry.geom_type.isin(_POLYGON_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf.to_crs(crs)
    except Exception as exc:
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
        gdf = ox.features_from_bbox(bbox=bbox, tags=_GREENSPACE_TAGS)
        gdf = gdf[gdf.geometry.geom_type.isin(_POLYGON_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf.to_crs(crs)
    except Exception as exc:
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
        graph = ox.graph_from_bbox(bbox=bbox, network_type=network_type)
        _, edges = ox.graph_to_gdfs(graph)
        if edges.empty:
            return _empty_gdf(crs)
        return edges.to_crs(crs)
    except Exception as exc:
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
        gdf = ox.features_from_bbox(bbox=bbox, tags=_PARKING_TAGS)
        gdf = gdf[gdf.geometry.geom_type.isin(_POLYGON_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf.to_crs(crs)
    except Exception as exc:
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
        gdf = ox.features_from_bbox(bbox=bbox, tags=_SIDEWALK_TAGS)
        gdf = gdf[gdf.geometry.geom_type.isin(_LINE_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf.to_crs(crs)
    except Exception as exc:
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
        gdf = ox.features_from_bbox(bbox=bbox, tags=_TRANSIT_TAGS)
        gdf = gdf[gdf.geometry.geom_type.isin(_POINT_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf.to_crs(crs)
    except Exception as exc:
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
        gdf = ox.features_from_bbox(bbox=bbox, tags=_LANDUSE_TAGS)
        gdf = gdf[gdf.geometry.geom_type.isin(_POLYGON_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf.to_crs(crs)
    except Exception as exc:
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
        gdf = ox.features_from_bbox(bbox=bbox, tags=_FACILITY_TAGS)
        if gdf.empty:
            return _empty_gdf(crs)
        gdf = gdf.to_crs(crs)
        poly = gdf.geometry.geom_type.isin(_POLYGON_GEOM_TYPES)
        gdf.loc[poly, "geometry"] = gdf.loc[poly].geometry.centroid
        gdf = gdf[gdf.geometry.geom_type.isin(_POINT_GEOM_TYPES)].copy()
        if gdf.empty:
            return _empty_gdf(crs)
        return gdf
    except Exception as exc:
        logger.warning(f"fetch_facilities failed: {exc} — returning empty GeoDataFrame")
        return _empty_gdf(crs)


def _empty_gdf(crs: str) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(geometry=[], crs=crs)
