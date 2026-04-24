"""Community health metric computations for the assessment pipeline.

All functions accept GeoDataFrames and a study_area GeoDataFrame.
They handle CRS mismatches by reprojecting inputs to match study_area.
They return 0.0 / zero-dict when layers are empty.
"""
from __future__ import annotations

import geopandas as gpd


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
