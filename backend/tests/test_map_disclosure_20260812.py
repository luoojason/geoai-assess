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
