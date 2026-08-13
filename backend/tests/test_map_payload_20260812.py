"""Grid bucketing for the viewport-rendered map.

The test that matters is test_every_feature_is_reachable: nothing in the suite
before 2026-08-12 asserted that the map contains what was measured, which is why
87.5% of Delaware's buildings could go missing silently for two weeks.
"""
from __future__ import annotations

import geopandas as gpd
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    Point,
    Polygon,
)

from backend import hcas
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
    # geoms[feature][ring][point] — ring 0 is the exterior.
    lon, lat = p["geoms"][0][0][0]
    assert -84.0 < lon < -82.0
    assert 39.0 < lat < 41.0


def test_polygon_with_hole_keeps_the_hole_as_a_second_ring():
    # A square with a smaller square hole cut out of it.
    square_with_hole = Polygon(
        shell=[(-83.0, 40.0), (-82.9, 40.0), (-82.9, 40.1), (-83.0, 40.1)],
        holes=[[(-82.98, 40.02), (-82.92, 40.02), (-82.92, 40.08), (-82.98, 40.08)]],
    )
    gdf = gpd.GeoDataFrame(geometry=[square_with_hole], crs="EPSG:4326")
    p = build_payload(gdf, cell_deg=0.01)
    entry = p["geoms"][0]
    assert len(entry) == 2, "expected exterior ring + one hole ring"
    hole_ring = entry[1]
    assert [-82.98, 40.02] in hole_ring
    assert [-82.92, 40.08] in hole_ring


def _metre_squares(n: int, size: float = 12.0) -> gpd.GeoDataFrame:
    polys = [Polygon([(x * size * 2, 0), (x * size * 2 + size, 0),
                      (x * size * 2 + size, size), (x * size * 2, size)]) for x in range(n)]
    return gpd.GeoDataFrame(geometry=polys, crs="EPSG:5070")


def _many_vertex_blobs(n: int, spacing: float = 100.0, radius: float = 20.0) -> gpd.GeoDataFrame:
    """n ~64-vertex circles (buffered points), in metres (EPSG:5070).

    Unlike a 4-corner square, shapely's preserve_topology=True simplification
    can actually remove vertices from this fixture, so the 12 m overview
    tolerance and the 1.5 m detail tolerance produce visibly different point
    counts.
    """
    blobs = [Point(x * spacing, 0).buffer(radius, quad_segs=16) for x in range(n)]
    return gpd.GeoDataFrame(geometry=blobs, crs="EPSG:5070")


def test_detail_payload_keeps_every_feature():
    gdf = _metre_squares(20000)
    p = hcas._detail_payload(gdf)
    assert p["total"] >= 20000, "the detail tier must not sample"


def test_detail_geometry_is_finer_than_overview():
    gdf = _many_vertex_blobs(50)
    overview, _info = hcas._overview_geoms(gdf)
    detail = hcas._detail_payload(gdf)
    overview_pts = sum(len(g.exterior.coords) for g in overview.geometry)
    detail_pts = sum(len(r) for entry in detail["geoms"] for r in entry)
    assert detail_pts > overview_pts, "12 m simplification on a 12 m house is a blob"


def test_overview_still_samples_and_reports():
    gdf = _metre_squares(26393)
    _slim, info = hcas._overview_geoms(gdf)
    assert info["stride"] == 8
    assert info["total"] == 26393


def test_geometry_collection_parts_are_all_reachable():
    """`_clip_to_study_area`'s `.intersection()` against a county polygon can
    return a GeometryCollection — a LineString clipped exactly along a
    shared boundary segment, for instance, produces mixed-dimension pieces.
    fetch_buildings filters to polygons and fetch_sidewalks to lines before
    this point today, so nothing had ever handed build_payload a
    GeometryCollection until the Important-1 clip-before-detail fix made
    that a real, reachable input. `_parts` claims to recurse through
    Multi*/GeometryCollection; this is the first test that runs it.
    """
    gc = GeometryCollection([
        Polygon([(-83.0, 40.0), (-82.99, 40.0), (-82.99, 40.01), (-83.0, 40.01)]),
        LineString([(-82.98, 40.02), (-82.97, 40.03)]),
        Point(-82.96, 40.04),
    ])
    gdf = gpd.GeoDataFrame(geometry=[gc], crs="EPSG:4326")
    p = build_payload(gdf, cell_deg=0.01)
    assert p["total"] == 3, "one geoms entry per member of the collection"
    reachable = set()
    for idxs in p["cells"].values():
        reachable.update(idxs)
    assert reachable == {0, 1, 2}, f"not every collection member reachable: {reachable}"
    # Ring 0 (the only ring each of these members has) keeps its own point
    # count rather than being merged or dropped: a closed 4-vertex polygon
    # ring (5 points, shapely repeats the first to close it), a 2-point
    # line, and a 1-point "ring" for the bare Point.
    lens = sorted(len(p["geoms"][i][0]) for i in range(3))
    assert lens == [1, 2, 5], f"member point counts not preserved distinctly: {lens}"


def test_multilinestring_parts_are_all_reachable():
    """`_clip_to_study_area`'s `.intersection()` can also turn a single
    LineString into a MultiLineString — a sidewalk crossing the county
    boundary in/out/in comes back as two or more disjoint parts. Untested
    until the Important-1 clip-before-detail fix made this a real input to
    build_payload (fetch_sidewalks only ever handed it plain LineStrings).
    """
    mls = MultiLineString([
        [(-83.0, 40.0), (-82.99, 40.0)],
        [(-82.5, 41.0), (-82.49, 41.0), (-82.48, 41.0)],
    ])
    gdf = gpd.GeoDataFrame(geometry=[mls], crs="EPSG:4326")
    p = build_payload(gdf, cell_deg=0.01)
    assert p["total"] == 2, "one geoms entry per line part of the MultiLineString"
    reachable = set()
    for idxs in p["cells"].values():
        reachable.update(idxs)
    assert reachable == {0, 1}, f"not every MultiLineString part reachable: {reachable}"
    lens = sorted(len(p["geoms"][i][0]) for i in range(2))
    assert lens == [2, 3], f"each part should keep its own point count, not be merged: {lens}"
