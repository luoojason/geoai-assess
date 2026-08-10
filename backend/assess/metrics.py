"""Community health metric computations for the assessment pipeline.

All functions accept GeoDataFrames and a study_area GeoDataFrame.
They handle CRS mismatches by reprojecting inputs to match study_area.
They return 0.0 / zero-dict when layers are empty.
"""
from __future__ import annotations

import geopandas as gpd
import pandas as pd


# Which published metric keys are derived from which layer fetcher.
#
# A failed fetch returns an empty GeoDataFrame (osm_layers never raises), and
# every computation below then produces a perfectly plausible 0.0. Zero parks
# and "we could not reach the park data" are the same number, so a caller that
# knows a fetcher failed must mark these keys as not-measured instead of
# publishing the zero. Keyed by fetcher __name__, which is what
# netdiag.record_fetch_error records.
METRIC_KEYS_BY_FETCHER: dict[str, tuple[str, ...]] = {
    "fetch_buildings": ("building_coverage_ratio", "far", "levels_tagged_pct"),
    "fetch_greenspace": ("greenspace_coverage_ratio", "greenspace_area_m2"),
    "fetch_road_network": ("road_density_km_per_km2",),
    "fetch_parking": ("parking_coverage_ratio",),
    "fetch_sidewalks": ("pedestrian_density_km_per_km2",),
    "fetch_transit_stops": ("transit_stop_count", "transit_stop_density_per_km2"),
    "fetch_facilities": ("facility_count", "facility_density_per_km2"),
    "fetch_landuse": ("land_use_mix", "land_use_categories"),
}


def unmeasured_metric_keys(failed_fetchers) -> list[str]:
    """Metric keys that cannot be measurements because their source fetch failed."""
    out: set[str] = set()
    for fn in failed_fetchers:
        out.update(METRIC_KEYS_BY_FETCHER.get(fn, ()))
    return sorted(out)


def _clipped_area(geoms: gpd.GeoSeries, study_area: gpd.GeoDataFrame) -> gpd.GeoSeries:
    """Per-feature area (m²) clipped to the study-area polygon, index preserved.

    OSM layers are fetched by bounding box, so a single large feature (e.g. a
    rural park or forest) can extend far beyond the study disc. Summing raw
    areas then inflated the coverage ratio above 1.0 and clamped it to "100%"
    — worst for sparse rural counties. Clipping to the study area first makes
    the ratio a true fraction.
    """
    sa = study_area.geometry.union_all()
    return geoms.intersection(sa).area


def _clipped_length(geoms: gpd.GeoSeries, study_area: gpd.GeoDataFrame) -> gpd.GeoSeries:
    """Per-feature length (m) clipped to the study-area polygon.

    The line counterpart of _clipped_area, and it was missing. Every density
    below divides by the study area, so an unclipped length is a numerator from
    one region over a denominator from another. Measured 2026-08-09 on a 2 km
    road crossing a 2 km² county with 1 km of it running outside: the county
    reported 1.5 km/km² against a true 1.0, and — because per-unit tabulation
    hands each unit every feature that touches it, at full length — a 1 km²
    tract holding 1 km of that road reported 3.0 km/km², three times the truth,
    in every tract at once.
    """
    sa = study_area.geometry.union_all()
    return geoms.intersection(sa).length


def _inside(gdf: gpd.GeoDataFrame, study_area: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """The features that actually fall in the study area.

    Counts (transit stops, facilities) had no equivalent of the clipping the
    area metrics do, so they counted every point in the fetched bounding box.
    Per unit that was worse than approximate: each unit was handed every point
    near it and counted all of them, so a tract with one bus stop could report
    the county's four.
    """
    if gdf.empty:
        return gdf
    sa = study_area.geometry.union_all()
    return gdf[gdf.geometry.intersects(sa)]


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
    building_area = _clipped_area(buildings.geometry, study_area).sum()
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
    green_area = float(_clipped_area(greenspace.geometry, study_area).sum())
    ratio = min(green_area / area_total, 1.0) if area_total > 0 else 0.0
    return {"area_m2": green_area, "ratio": float(ratio)}


def compute_road_density(
    roads: gpd.GeoDataFrame,
    study_area: gpd.GeoDataFrame,
) -> float:
    """Return road length in km per study_area area in km².

    Length is clipped to the study area, so the numerator and the denominator
    describe the same ground (see _clipped_length)."""
    if roads.empty:
        return 0.0
    if roads.crs != study_area.crs:
        roads = roads.to_crs(study_area.crs)
    area_km2 = study_area.geometry.area.sum() / 1_000_000
    if area_km2 == 0:
        return 0.0
    length_km = _clipped_length(roads.geometry, study_area).sum() / 1_000
    return float(length_km / area_km2)


def compute_far_detail(
    buildings: gpd.GeoDataFrame,
    study_area: gpd.GeoDataFrame,
) -> dict[str, float | None]:
    """Return {'far': float, 'levels_tagged_pct': float | None}.

    far = sum(footprint * stories) / study_area area. Uses the 'building:levels'
    column when present; every building without a parseable value is *assumed*
    to be 1 level.

    That assumption is the whole reason for the second key. OSM tags
    building:levels on only a small minority of US footprints, so in most
    counties FAR collapses to roughly the building-coverage ratio and is an
    assumed lower bound rather than an independent measurement of vertical
    density. levels_tagged_pct is the share (0-100) of buildings that actually
    carried a usable tag, so a reader can see how much of FAR was measured.
    It is None when there are no buildings to tag (FAR is then a real 0.0, but
    the tagged share is undefined — do not report it as 0%).
    """
    if buildings.empty:
        return {"far": 0.0, "levels_tagged_pct": None}
    if buildings.crs != study_area.crs:
        buildings = buildings.to_crs(study_area.crs)
    area_total = study_area.geometry.area.sum()
    if "building:levels" in buildings.columns:
        parsed = pd.to_numeric(buildings["building:levels"], errors="coerce")
        levels = parsed.fillna(1.0).clip(lower=1.0)
        tagged_pct = round(float(parsed.notna().sum()) / len(buildings) * 100, 2)
    else:
        levels = pd.Series(1.0, index=buildings.index)
        tagged_pct = 0.0
    if area_total == 0:
        return {"far": 0.0, "levels_tagged_pct": tagged_pct}
    floor_area = (_clipped_area(buildings.geometry, study_area) * levels).sum()
    return {"far": float(floor_area / area_total), "levels_tagged_pct": tagged_pct}


def compute_far(
    buildings: gpd.GeoDataFrame,
    study_area: gpd.GeoDataFrame,
) -> float:
    """Return the Floor Area Ratio alone. See compute_far_detail — callers that
    publish FAR should use that instead, so the levels-tagged share travels
    with the number."""
    return float(compute_far_detail(buildings, study_area)["far"])


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
    return min(float(_clipped_area(parking.geometry, study_area).sum() / area_total), 1.0)


def compute_pedestrian_density(
    sidewalks: gpd.GeoDataFrame,
    study_area: gpd.GeoDataFrame,
) -> float:
    """Return sidewalk length in km per study_area area in km².

    Length is clipped to the study area (see _clipped_length). Returns 0.0 when
    sidewalks is empty. Handles CRS mismatch.
    """
    if sidewalks.empty:
        return 0.0
    if sidewalks.crs != study_area.crs:
        sidewalks = sidewalks.to_crs(study_area.crs)
    area_km2 = study_area.geometry.area.sum() / 1_000_000
    if area_km2 == 0:
        return 0.0
    return float(_clipped_length(sidewalks.geometry, study_area).sum() / 1_000 / area_km2)


def compute_transit_access(
    transit_stops: gpd.GeoDataFrame,
    study_area: gpd.GeoDataFrame,
) -> dict[str, float | int]:
    """Return {'stop_count': int, 'stop_density_per_km2': float}.

    Only stops inside the study area are counted (see _inside). Returns zeros
    when transit_stops is empty. Handles CRS mismatch.
    """
    if transit_stops.empty:
        return {"stop_count": 0, "stop_density_per_km2": 0.0}
    if transit_stops.crs != study_area.crs:
        transit_stops = transit_stops.to_crs(study_area.crs)
    area_km2 = study_area.geometry.area.sum() / 1_000_000
    count = len(_inside(transit_stops, study_area))
    density = float(count / area_km2) if area_km2 > 0 else 0.0
    return {"stop_count": count, "stop_density_per_km2": density}


def compute_land_use_mix(
    landuse: gpd.GeoDataFrame,
    study_area: gpd.GeoDataFrame,
) -> dict[str, float | int]:
    """Return {'land_use_mix': float 0-1, 'land_use_categories': int}.

    Normalized Shannon entropy of landuse polygon areas by 'landuse' tag:
    0.0 = single use, 1.0 = perfectly even mix. Zeros when landuse is empty
    or carries no 'landuse' column.
    """
    import numpy as np

    if landuse.empty or "landuse" not in landuse.columns:
        return {"land_use_mix": 0.0, "land_use_categories": 0}
    if landuse.crs != study_area.crs:
        landuse = landuse.to_crs(study_area.crs)
    # Clipped, like every other area here: the mix is a property of the study
    # area, and unclipped polygons weight it by land outside the boundary.
    areas = (landuse.assign(_a=_clipped_area(landuse.geometry, study_area))
                    .groupby("landuse")["_a"].sum())
    areas = areas[areas > 0]
    k = len(areas)
    if k <= 1:
        return {"land_use_mix": 0.0, "land_use_categories": int(k)}
    p = areas / areas.sum()
    entropy = float(-(p * np.log(p)).sum() / np.log(k))
    return {"land_use_mix": round(entropy, 6), "land_use_categories": int(k)}


def compute_facility_access(
    facilities: gpd.GeoDataFrame,
    study_area: gpd.GeoDataFrame,
) -> dict[str, float | int]:
    """Return {'facility_count': int, 'facility_density_per_km2': float}.

    Only facilities inside the study area are counted (see _inside). Returns
    zeros when facilities is empty. Handles CRS mismatch.
    """
    if facilities.empty:
        return {"facility_count": 0, "facility_density_per_km2": 0.0}
    if facilities.crs != study_area.crs:
        facilities = facilities.to_crs(study_area.crs)
    area_km2 = study_area.geometry.area.sum() / 1_000_000
    count = len(_inside(facilities, study_area))
    density = float(count / area_km2) if area_km2 > 0 else 0.0
    return {"facility_count": count, "facility_density_per_km2": density}
