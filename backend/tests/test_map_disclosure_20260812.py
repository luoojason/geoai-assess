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


def test_legend_only_promises_zoom_for_layers_that_actually_have_detail():
    """hcas.py builds each layer's detail payload inside its OWN try/except
    (backend/hcas.py, _run_step_7): one layer's `_detail_payload()` call can
    raise, be caught and logged, and never make it into the `detail` dict,
    while the other layers' calls succeed — leaving `detail_payloads` a
    non-empty dict that is nonetheless missing that one layer's key.

    A single `has_detail=bool(detail_payloads)` applied to every layer would
    have the failed layer's legend line claim "zoom in and every one in view
    is drawn" even though no HCASViewportLayer was ever registered for it —
    the exact "legend promises what the map cannot keep" defect this whole
    plan exists to eliminate. Reproduces that shape directly: buildings has a
    detail payload, roads does not, and asserts each layer's own sentence in
    the rendered HTML matches its own reality.
    """
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

    # Different totals so each layer's rendered sentence is unambiguous to
    # locate in the HTML.
    sample_info = {
        "buildings": {"shown": 3300, "total": 26393, "stride": 8},
        "roads": {"shown": 3200, "total": 28865, "stride": 9},
    }
    html = render_assessment_map(
        sa, b, empty, empty, {"building_coverage_ratio": 0.1},
        sample_info=sample_info,
        # Only buildings' detail payload "succeeded" — roads' key is absent,
        # as it would be if hcas.py's try/except for roads had caught
        # something. detail_payloads is still a non-empty dict.
        detail_payloads={"buildings": build_payload(b)},
    )

    bldg_anchor = html.index("showing 3,300 of 26,393")
    roads_anchor = html.index("showing 3,200 of 28,865")
    bldg_sentence = html[bldg_anchor:bldg_anchor + 200].lower()
    roads_sentence = html[roads_anchor:roads_anchor + 200].lower()

    assert "zoom in" in bldg_sentence
    assert "zoom in" not in roads_sentence


def test_viewport_layer_shape_matches_geometry_type():
    """Buildings are polygons — L.polygon(rings) renders their holes as
    voids. Roads and sidewalks are LineStrings and MUST get shape:'polyline',
    or every road gets a spurious closing segment joining its last point back
    to its first. Nothing previously asserted the shape landed correctly per
    layer in the rendered HTML — only that SOME shape option existed.
    """
    import html as html_module
    import json
    import re

    import geopandas as gpd
    from shapely.geometry import LineString, Polygon
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
    r = gpd.GeoDataFrame(
        geometry=[LineString([(-83.0, 40.1), (-82.99, 40.11)])],
        crs="EPSG:4326",
    ).to_crs("EPSG:5070")
    sw = gpd.GeoDataFrame(
        geometry=[LineString([(-83.0, 40.12), (-82.99, 40.13)])],
        crs="EPSG:4326",
    ).to_crs("EPSG:5070")
    empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")

    html = render_assessment_map(
        sa, b, empty, empty, {"building_coverage_ratio": 0.1},
        sidewalks=sw,
        detail_payloads={
            "buildings": build_payload(b),
            "roads": build_payload(r),
            "sidewalks": build_payload(sw),
        },
    )
    # render_assessment_map returns m._repr_html_(), which HTML-entity-
    # escapes the whole document to embed it in an iframe srcdoc attribute
    # (folium's own Figure._repr_html_, unrelated to this task). Unescape to
    # get back real quote characters before parsing JSON out of it.
    raw = html_module.unescape(html)

    decoder = json.JSONDecoder()
    opts_list = []
    pos = 0
    while True:
        idx = raw.find("new HCASViewportLayer(", pos)
        if idx == -1:
            break
        opts_start = re.search(r",p,", raw[idx:]).end() + idx
        opts, _ = decoder.raw_decode(raw, opts_start)
        opts_list.append(opts)
        pos = opts_start

    assert len(opts_list) == 3
    shape_by_color = {opts["style"].get("color"): opts["shape"] for opts in opts_list}
    assert shape_by_color["#333333"] == "polygon"    # buildings
    assert shape_by_color["#3a7abf"] == "polyline"   # roads
    assert shape_by_color["#7b4fa6"] == "polyline"   # sidewalks


def test_viewport_init_is_deferred_to_window_load():
    """Task 8's real-browser check found the map hangs indefinitely: folium
    emits its OWN `L.map(...)` call into the same script section our
    injected code lands in, positioned by folium's own template — a plain
    top-level child added via `script.add_child(...)` can land BEFORE that
    call in execution order. The map variable is then undefined when our
    `new HCASViewportLayer(...)` runs: an uncaught TypeError, and every
    viewport layer silently never attaches. This was invisible to every
    prior unit test (including this file's own ordering-focused tests) and
    to two code reviews; it took a real Leaflet browser run to surface.

    Text POSITION relative to `L.map(` is not a reliable regression guard
    here — folium's own template still places `L.map(...)` after our
    injected script child textually, and re-ordering placement was
    explicitly rejected as "the third guess". The actual fix is deferral:
    the instantiation runs inside a `window.addEventListener("load", ...)`
    callback, which fires only once every inline script — wherever it
    landed — has already executed. So this test asserts the real invariant
    directly: the instantiation call sits INSIDE that deferred callback's
    body, not at top level.
    """
    import html as html_module

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
    raw = html_module.unescape(html)

    listener_start = raw.index('window.addEventListener("load"')
    # Only one addEventListener("load", ...) is ever emitted by this module,
    # so its closing "});" is the next one after the opening — a direct
    # stand-in for "still inside this callback" without a full JS parser.
    listener_close = raw.index("\n});", listener_start)
    inst_idx = raw.index("new HCASViewportLayer(", listener_start)

    assert listener_start < inst_idx < listener_close
