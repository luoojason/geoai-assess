"""The basemap provider started demanding an API key — measured 2026-09-03.

Both maps were built with folium's ``tiles="CartoDB positron"`` shorthand, which
resolves to ``basemaps.cartocdn.com``. CARTO now answers those tile requests with
HTTP 200 and a PNG stamped diagonally **"API KEY REQUIRED - carto.com/basemaps/apikey"**,
so nothing errors and no log line appears: the map simply renders with the
watermark repeated across every tile, under the study-area boundary.

That is the locator map on step 1 and the assessment map on step 7 — the
deliverable a reviewer actually looks at. Caught in a browser pass, not by the
suite, because a watermarked tile is a successful HTTP response.

OpenStreetMap's tile server needs no key, and the app already credits OSM for
its vector data.
"""
import geopandas as gpd
import pytest
from shapely.geometry import box


@pytest.fixture
def study_area():
    return gpd.GeoDataFrame(
        {"name": ["Adams County"]},
        geometry=[box(-83.70, 38.59, -83.26, 39.05)],
        crs="EPSG:4326",
    )


def _tile_sources(html: str) -> str:
    return html.lower()


class TestNoKeyRequiringBasemap:
    def test_locator_map_does_not_use_a_key_requiring_tile_host(self, study_area):
        from backend.assess.renderer import render_base_map

        html = _tile_sources(render_base_map(study_area, "Adams County, Ohio, USA"))
        assert "cartocdn" not in html, (
            "CARTO tiles are watermarked 'API KEY REQUIRED' without a key — the map "
            "renders successfully and looks broken to the reviewer"
        )

    def test_locator_map_uses_a_keyless_tile_source(self, study_area):
        from backend.assess.renderer import render_base_map

        html = _tile_sources(render_base_map(study_area, "Adams County, Ohio, USA"))
        assert "tile.openstreetmap.org" in html

    def test_assessment_map_does_not_use_a_key_requiring_tile_host(self, study_area):
        """Step 7's map is the deliverable; it carried the same shorthand."""
        import inspect

        from backend.assess import renderer

        src = inspect.getsource(renderer)
        assert "CartoDB positron" not in src, (
            "a second folium map still requests CARTO tiles; both must be keyless"
        )
