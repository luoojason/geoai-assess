"""The step-7 map drew every Nth feature and never said so.

Delaware County 2026-08-12: 26,393 buildings fetched and measured, 3,300 drawn
(stride 8), and the legend showed only "not measured" / "not selected" — neither
of which applied. The metrics panel printed "Building Coverage: 47.2%" beside a
map showing 12.5% of the buildings, so the correct reading of that screen was
"the fetch broke". It had not.
"""
from __future__ import annotations

import geopandas as gpd
from shapely.geometry import Polygon

from backend import hcas


def _grid_gdf(n: int, size: float = 50.0) -> gpd.GeoDataFrame:
    """n squares in a row, in metres (EPSG:5070, as the app uses)."""
    polys = [Polygon([(x * size * 2, 0), (x * size * 2 + size, 0),
                      (x * size * 2 + size, size), (x * size * 2, size)]) for x in range(n)]
    return gpd.GeoDataFrame(geometry=polys, crs="EPSG:5070")


def test_slim_reports_how_many_it_kept():
    slim, info = hcas._overview_geoms(_grid_gdf(26393))
    assert info["total"] == 26393
    assert info["stride"] == 8
    assert info["shown"] == len(slim)
    assert info["shown"] < info["total"]


def test_slim_reports_no_sampling_when_under_the_cap():
    slim, info = hcas._overview_geoms(_grid_gdf(100))
    assert info == {"shown": 100, "total": 100, "stride": 1}
    assert len(slim) == 100


def test_legend_states_the_sample():
    from backend.assess.renderer import _sample_legend_line

    line = _sample_legend_line("buildings", {"shown": 3300, "total": 26393, "stride": 8})
    assert "3,300" in line
    assert "26,393" in line
    assert "1 in 8" in line
    # The promise the map cannot yet keep must not appear.
    assert "zoom in" not in line.lower()


def test_no_legend_line_when_nothing_was_dropped():
    from backend.assess.renderer import _sample_legend_line

    assert _sample_legend_line("buildings", {"shown": 100, "total": 100, "stride": 1}) == ""


def test_legend_promises_zoom_only_when_detail_is_embedded():
    from backend.assess.renderer import _sample_legend_line

    info = {"shown": 3300, "total": 26393, "stride": 8}
    assert "zoom in" not in _sample_legend_line("buildings", info).lower()
    assert "zoom in" in _sample_legend_line("buildings", info, has_detail=True).lower()


def test_rendered_map_embeds_the_viewport_module_and_no_external_urls():
    import geopandas as gpd
    from shapely.geometry import Polygon
    from backend.assess.renderer import render_assessment_map
    from backend.assess.map_payload import build_payload

    sa = gpd.GeoDataFrame(
        geometry=[Polygon([(-83.1, 40.0), (-82.9, 40.0), (-82.9, 40.2), (-83.1, 40.2)])],
        crs="EPSG:4326",
    ).to_crs("EPSG:5070")
    b = gpd.GeoDataFrame(
        geometry=[Polygon([(-83.0, 40.1), (-82.99, 40.1), (-82.99, 40.11), (-83.0, 40.11)])],
        crs="EPSG:4326",
    ).to_crs("EPSG:5070")
    empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")

    html = render_assessment_map(
        sa, b, empty, empty, {"building_coverage_ratio": 0.1},
        detail_payloads={"buildings": build_payload(b)},
    )
    assert "HCASViewportLayer" in html
    # The page must not reach the network for its own feature data.
    assert "fetch(" not in html
    assert "XMLHttpRequest" not in html
    # The payload itself is inline, not referenced.
    assert "cell_deg" in html


def test_render_includes_the_sampled_layer_sentence_end_to_end():
    """Task 1 unit-tested _sample_legend_line in isolation; nothing asserted
    the legend loop in render_assessment_map actually puts that sentence into
    the HTML it returns. This closes that gap.
    """
    import geopandas as gpd
    from shapely.geometry import Polygon
    from backend.assess.renderer import render_assessment_map

    sa = gpd.GeoDataFrame(
        geometry=[Polygon([(-83.1, 40.0), (-82.9, 40.0), (-82.9, 40.2), (-83.1, 40.2)])],
        crs="EPSG:4326",
    ).to_crs("EPSG:5070")
    b = gpd.GeoDataFrame(
        geometry=[Polygon([(-83.0, 40.1), (-82.99, 40.1), (-82.99, 40.11), (-83.0, 40.11)])],
        crs="EPSG:4326",
    ).to_crs("EPSG:5070")
    empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")

    sample_info = {"buildings": {"shown": 3300, "total": 26393, "stride": 8}}
    html = render_assessment_map(
        sa, b, empty, empty, {"building_coverage_ratio": 0.1},
        sample_info=sample_info,
    )
    assert "showing 3,300 of 26,393" in html
    assert "1 in 8" in html
    # No detail_payloads were passed, so the map cannot yet keep the "zoom in"
    # promise — the end-to-end render must not overstate itself either.
    assert "zoom in" not in html.lower()
