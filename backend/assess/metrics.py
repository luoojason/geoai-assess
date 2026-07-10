"""Community health metric computations for the assessment pipeline.

All functions accept GeoDataFrames and a study_area GeoDataFrame.
They handle CRS mismatches by reprojecting inputs to match study_area.
They return 0.0 / zero-dict when layers are empty.
"""
from __future__ import annotations

import geopandas as gpd
import pandas as pd


def compute_building_coverage(
    buildings: gpd.GeoDataFrame,
    study_area: gpd.GeoDataFrame,
) -> float:
    """Return building footprint area / study_area area (0.0–1.0).

    Both inputs must be in a projected CRS (metres). If buildings is in a
    different CRS it is reprojected to match study_area.
    """
    if buildings.empty:
        return 0.0
    if buildings.crs != study_area.crs:
        buildings = buildings.to_crs(study_area.crs)
    area_total = study_area.geometry.area.sum()
    if area_total == 0:
        return 0.0
    building_area = buildings.geometry.area.sum()
    return min(float(building_area / area_total), 1.0)


def compute_greenspace_coverage(
    greenspace: gpd.GeoDataFrame,
    study_area: gpd.GeoDataFrame,
) -> dict[str, float]:
    """Return {'area_m2': float, 'ratio': float}.

    ratio = greenspace area / study_area area (0.0–1.0).
    """
    if greenspace.empty:
        return {"area_m2": 0.0, "ratio": 0.0}
    if greenspace.crs != study_area.crs:
        greenspace = greenspace.to_crs(study_area.crs)
    area_total = study_area.geometry.area.sum()
    green_area = float(greenspace.geometry.area.sum())
    ratio = min(green_area / area_total, 1.0) if area_total > 0 else 0.0
    return {"area_m2": green_area, "ratio": float(ratio)}


def compute_road_density(
    roads: gpd.GeoDataFrame,
    study_area: gpd.GeoDataFrame,
) -> float:
    """Return total road length in km per study_area area in km²."""
    if roads.empty:
        return 0.0
    if roads.crs != study_area.crs:
        roads = roads.to_crs(study_area.crs)
    area_km2 = study_area.geometry.area.sum() / 1_000_000
    if area_km2 == 0:
        return 0.0
    length_km = roads.geometry.length.sum() / 1_000
    return float(length_km / area_km2)


def compute_far(
    buildings: gpd.GeoDataFrame,
    study_area: gpd.GeoDataFrame,
) -> float:
    """Return Floor Area Ratio = sum(footprint * stories) / study_area area.

    Uses 'building:levels' column when present; defaults to 1.0 per building
    when the column is missing or NaN. Returns 0.0 when buildings is empty.
    """
    if buildings.empty:
        return 0.0
    if buildings.crs != study_area.crs:
        buildings = buildings.to_crs(study_area.crs)
    area_total = study_area.geometry.area.sum()
    if area_total == 0:
        return 0.0
    if "building:levels" in buildings.columns:
        levels = pd.to_numeric(buildings["building:levels"], errors="coerce").fillna(1.0).clip(lower=1.0)
    else:
        levels = pd.Series(1.0, index=buildings.index)
    floor_area = (buildings.geometry.area * levels).sum()
    return float(floor_area / area_total)


def compute_parking_coverage(
    parking: gpd.GeoDataFrame,
    study_area: gpd.GeoDataFrame,
) -> float:
    """Return parking area / study_area area (0.0–1.0).

    Returns 0.0 when parking is empty. Handles CRS mismatch.
    """
    if parking.empty:
        return 0.0
    if parking.crs != study_area.crs:
        parking = parking.to_crs(study_area.crs)
    area_total = study_area.geometry.area.sum()
    if area_total == 0:
        return 0.0
    return min(float(parking.geometry.area.sum() / area_total), 1.0)


def compute_pedestrian_density(
    sidewalks: gpd.GeoDataFrame,
    study_area: gpd.GeoDataFrame,
) -> float:
    """Return total sidewalk length in km per study_area area in km².

    Returns 0.0 when sidewalks is empty. Handles CRS mismatch.
    """
    if sidewalks.empty:
        return 0.0
    if sidewalks.crs != study_area.crs:
        sidewalks = sidewalks.to_crs(study_area.crs)
    area_km2 = study_area.geometry.area.sum() / 1_000_000
    if area_km2 == 0:
        return 0.0
    return float(sidewalks.geometry.length.sum() / 1_000 / area_km2)


def compute_transit_access(
    transit_stops: gpd.GeoDataFrame,
    study_area: gpd.GeoDataFrame,
) -> dict[str, float | int]:
    """Return {'stop_count': int, 'stop_density_per_km2': float}.

    Returns zeros when transit_stops is empty. Handles CRS mismatch.
    """
    if transit_stops.empty:
        return {"stop_count": 0, "stop_density_per_km2": 0.0}
    if transit_stops.crs != study_area.crs:
        transit_stops = transit_stops.to_crs(study_area.crs)
    area_km2 = study_area.geometry.area.sum() / 1_000_000
    count = len(transit_stops)
    density = float(count / area_km2) if area_km2 > 0 else 0.0
    return {"stop_count": count, "stop_density_per_km2": density}


def compute_facility_access(
    facilities: gpd.GeoDataFrame,
    study_area: gpd.GeoDataFrame,
) -> dict[str, float | int]:
    """Return {'facility_count': int, 'facility_density_per_km2': float}.

    Returns zeros when facilities is empty. Handles CRS mismatch.
    """
    if facilities.empty:
        return {"facility_count": 0, "facility_density_per_km2": 0.0}
    if facilities.crs != study_area.crs:
        facilities = facilities.to_crs(study_area.crs)
    area_km2 = study_area.geometry.area.sum() / 1_000_000
    count = len(facilities)
    density = float(count / area_km2) if area_km2 > 0 else 0.0
    return {"facility_count": count, "facility_density_per_km2": density}
