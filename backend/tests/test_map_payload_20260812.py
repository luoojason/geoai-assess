"""Grid bucketing for the viewport-rendered map.

The test that matters is test_every_feature_is_reachable: nothing in the suite
before 2026-08-12 asserted that the map contains what was measured, which is why
87.5% of Delaware's buildings could go missing silently for two weeks.
"""
from __future__ import annotations

import geopandas as gpd
from shapely.geometry import Polygon

from backend.assess.map_payload import build_payload


def _squares(n: int, size: float = 0.001) -> gpd.GeoDataFrame:
    """n small squares in a row, in EPSG:4326 degrees."""
    polys = []
    for i in range(n):
        x = -83.0 + i * size * 2
        polys.append(Polygon([(x, 40.0), (x + size, 40.0), (x + size, 40.0 + size), (x, 40.0 + size)]))
    return gpd.GeoDataFrame(geometry=polys, crs="EPSG:4326")


def test_every_feature_is_reachable():
    gdf = _squares(500)
    p = build_payload(gdf, cell_deg=0.01)
    reachable = set()
    for idxs in p["cells"].values():
        reachable.update(idxs)
    assert reachable == set(range(500))
    assert p["total"] == 500


def test_geometry_is_stored_once_per_feature():
    gdf = _squares(500)
    p = build_payload(gdf, cell_deg=0.01)
    assert len(p["geoms"]) == 500


def test_feature_spanning_a_boundary_is_indexed_in_every_cell_it_touches():
    # A square straddling the x=-83.0 cell boundary with cell_deg=0.01.
    straddler = Polygon([(-83.005, 40.0), (-82.995, 40.0), (-82.995, 40.005), (-83.005, 40.005)])
    gdf = gpd.GeoDataFrame(geometry=[straddler], crs="EPSG:4326")
    p = build_payload(gdf, cell_deg=0.01)
    holding = [k for k, idxs in p["cells"].items() if 0 in idxs]
    assert len(holding) >= 2, f"straddling feature filed into only {holding}"


def test_empty_input_is_an_empty_payload_not_a_crash():
    gdf = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    p = build_payload(gdf)
    assert p["total"] == 0
    assert p["geoms"] == []
    assert p["cells"] == {}


def test_reprojects_to_4326():
    gdf = _squares(3).to_crs("EPSG:5070")
    p = build_payload(gdf, cell_deg=0.01)
    lon, lat = p["geoms"][0][0]
    assert -84.0 < lon < -82.0
    assert 39.0 < lat < 41.0
