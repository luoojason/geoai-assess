"""End-to-end map tests against real OpenStreetMap geometry, with no network.

Every other map test in this suite builds its own squares and circles. Those
catch logic errors and missed exactly one class of bug that mattered: the ones
that only appear when the geometry is real. Polygons with holes, MultiPolygons
produced by clipping, MultiLineStrings, Points, and a layer that is genuinely
empty are all in `fixtures/county_excerpt` — 2,783 features from Adams County,
Ohio, fetched through the app's own fetchers.

The bugs this file exists to keep dead, each of which shipped and each of which
was found by a human looking at a map rather than by a test:

  * the 3,500-feature cap, a fixed COUNT, so the fraction drawn collapsed as
    density rose — 1 in 58 on New York County, about one building per two city
    blocks, reported as "the map didn't load";
  * `landuse` fetched, inventoried and listed in the UI since the beginning,
    with no draw path at all — most of the green there is on a rural county,
    reported as "the green is missing";
  * point layers drawn with polygon/polyline constructors, which render
    nothing at all rather than failing loudly.
"""
from __future__ import annotations

import html as _html
import json
import re

import pytest

from backend import hcas
from backend.assess.map_payload import build_payload
from backend.assess.renderer import _LAYER_SPEC, _clip_to_study_area, render_assessment_map
from backend.tests.fixtures import LAYERS, load_county_excerpt


@pytest.fixture(scope="module")
def county():
    return load_county_excerpt()


def _payloads(county):
    sa = county["study_area"]
    out = {}
    for name in LAYERS:
        g = county[name]
        if g is None or g.empty:
            continue
        out[name] = hcas._detail_payload(_clip_to_study_area(g, sa))
    return out


def _rendered(county):
    """The real render path, unescaped out of folium's srcdoc iframe."""
    sa = county["study_area"]
    empty = hcas._empty_layer()
    slim = {n: hcas._overview_geoms(county[n])[0] for n in LAYERS}
    info = {n: hcas._overview_geoms(county[n])[1] for n in LAYERS}
    html = render_assessment_map(
        sa, slim.get("buildings", empty), slim.get("greenspace", empty),
        slim.get("roads", empty), {"building_coverage_ratio": 0.05},
        parking=slim.get("parking"), sidewalks=slim.get("sidewalks"),
        transit_stops=slim.get("transit_stops"), facilities=slim.get("facilities"),
        landuse=slim.get("landuse"), sample_info=info,
        detail_payloads=_payloads(county),
    )
    return _html.unescape(html)


def test_no_layer_is_sampled_at_any_density(county):
    """The cap is gone: every layer keeps every feature."""
    for name in LAYERS:
        g = county[name]
        if g.empty:
            continue
        _slim, info = hcas._overview_geoms(g)
        assert info["stride"] == 1, f"{name} was sampled"
        assert info["shown"] == info["total"] == len(g), f"{name} lost features"


def test_every_real_feature_survives_into_the_payload(county):
    """Real MultiPolygons and MultiLineStrings expand into several parts, so the
    part count is >= the feature count — never less, which would be loss."""
    for name, payload in _payloads(county).items():
        n_in = len(_clip_to_study_area(county[name], county["study_area"]))
        assert payload["total"] >= n_in, f"{name}: {payload['total']} parts from {n_in} features"
        reachable = set()
        for idxs in payload["cells"].values():
            reachable.update(idxs)
        assert reachable == set(range(payload["total"])), f"{name}: unreachable parts"


def test_landuse_is_actually_drawn(county):
    """Regression: landuse was fetched, counted and listed in the UI for weeks
    while the renderer had no parameter for it and hcas.py never passed one."""
    doc = _rendered(county)
    assert "Land use" in doc, "no Land use entry in the layer control"
    assert not county["landuse"].empty
    assert "landuse" in _payloads(county)


def test_every_non_empty_layer_reaches_the_map(county):
    doc = _rendered(county)
    for key, label, _show, _shape, _style in _LAYER_SPEC:
        if county[key].empty:
            continue
        assert label in doc, f"{key} ({label}) is missing from the rendered map"


def _emitted_options(doc) -> list:
    """The option object handed to each HCASViewportLayer, decoded.

    `raw_decode` from the opening brace, NOT a regex: the options nest a style
    object, so `\\{"gateZoom".*?\\}` stops at the STYLE's closing brace and
    yields invalid JSON that silently decodes to nothing. That mistake made an
    earlier version of this test pass an empty dict around and report a
    correctly-shaped layer as missing.
    """
    dec = json.JSONDecoder()
    out = []
    for m in re.finditer(r'\{"gateZoom"', doc):
        obj, _end = dec.raw_decode(doc[m.start():])
        out.append(obj)
    return out


def _shape_by_colour(doc) -> dict:
    by = {}
    for opts in _emitted_options(doc):
        style = opts.get("style", {})
        by[style.get("fillColor") or style.get("color")] = opts.get("shape")
    return by


def test_an_empty_layer_is_omitted_without_breaking_the_render(county):
    """transit_stops is genuinely empty in Adams. It must get no layer at all,
    and must not take the rest of the map down.

    Asserted against the EMITTED LAYERS, not against the whole document: the
    string "Transit Stops" legitimately appears in the metrics panel as
    "Transit Stops: not selected", which is a metric label rather than a layer.
    """
    assert county["transit_stops"].empty
    doc = _rendered(county)
    spec = {k: st for k, _l, _s, _sh, st in _LAYER_SPEC}
    transit_colour = spec["transit_stops"]["fillColor"]
    assert transit_colour not in _shape_by_colour(doc), "an empty layer was still drawn"
    non_empty = [k for k in LAYERS if not county[k].empty]
    assert len(_emitted_options(doc)) == len(non_empty)
    assert "Buildings" in doc and "Roads" in doc


def test_every_geometry_type_gets_its_own_constructor(county):
    """Facilities are Points, roads are LineStrings, buildings are Polygons.
    polygon/polyline on a one-coordinate ring draw nothing at all, so a wrong
    shape loses the layer silently rather than failing."""
    shapes = _shape_by_colour(_rendered(county))
    spec = {k: (sh, st) for k, _l, _s, sh, st in _LAYER_SPEC}
    for key in ("facilities", "roads", "buildings", "landuse", "sidewalks"):
        if county[key].empty:
            continue
        want_shape, style = spec[key]
        colour = style.get("fillColor") or style.get("color")
        assert shapes.get(colour) == want_shape, (
            f"{key} got shape {shapes.get(colour)}, expected {want_shape}")


def test_draw_order_follows_the_spec_not_the_payload_dict(county):
    """Regression: landuse used to paint OVER buildings.

    Each HCASViewportLayer builds its own `L.canvas` and Leaflet appends
    renderer containers with no z-index, so stacking follows the order these
    blocks are emitted in. hcas.py builds its payload dict from
    `names = sorted(...)`, i.e. alphabetically, which put landuse (4th) above
    buildings (1st) — a 30%-opacity farmland wash over every building
    footprint, the exact inversion `_LAYER_SPEC`'s own comment promises does
    not happen. The payload dict below is deliberately alphabetical to
    reproduce the conditions that caused it.
    """
    sa = county["study_area"]
    alphabetical = {
        n: hcas._detail_payload(_clip_to_study_area(county[n], sa))
        for n in sorted(LAYERS) if not county[n].empty
    }
    assert list(alphabetical) == sorted(alphabetical), "fixture must be alphabetical"
    empty = hcas._empty_layer()
    slim = {n: hcas._overview_geoms(county[n])[0] for n in LAYERS}
    doc = _html.unescape(render_assessment_map(
        sa, slim["buildings"], slim["greenspace"], slim["roads"],
        {"building_coverage_ratio": 0.05}, parking=slim["parking"],
        sidewalks=slim["sidewalks"], facilities=slim["facilities"],
        landuse=slim["landuse"], detail_payloads=alphabetical))

    by_colour = {(st.get("fillColor") or st.get("color")): k
                 for k, _l, _s, _sh, st in _LAYER_SPEC}
    emitted = []
    for opts in _emitted_options(doc):
        style = opts.get("style", {})
        emitted.append(by_colour.get(style.get("fillColor") or style.get("color")))

    spec_order = [k for k, _l, _s, _sh, _st in _LAYER_SPEC]
    assert emitted == [k for k in spec_order if k in emitted], (
        f"emitted {emitted}, expected _LAYER_SPEC order")
    assert emitted.index("landuse") < emitted.index("buildings"), (
        "landuse must draw beneath buildings, not over them")


def test_the_legend_makes_no_sampling_claim(county):
    """Nothing is sampled, so the sampling sentence must not appear. It said
    'showing 3,496 of 202,704' on a run where only ~800 were actually drawn.

    Scoped to the metrics panel, not to the whole document: viewport_layers.js
    is inlined into the page and its header comment *describes* the 1-in-58
    defect, so a document-wide search matches the explanation of the bug rather
    than the bug.
    """
    doc = _rendered(county)
    start = doc.find("Assessment Metrics")
    assert start != -1, "metrics panel not found"
    panel = doc[start:doc.find("</div>", start) + 6]
    assert "showing" not in panel, panel[:400]
    assert "1 in " not in panel, panel[:400]
    # and the sentence generator itself stays silent at stride 1
    from backend.assess.renderer import _sample_legend_line
    for name in LAYERS:
        if county[name].empty:
            continue
        _slim, info = hcas._overview_geoms(county[name])
        assert _sample_legend_line(name, info, has_detail=True) == ""


def test_the_map_fetches_nothing_for_its_own_features(county):
    """It must open from disk with no server. Base-map tiles are a separate
    matter; what cannot happen is the page fetching its own geometry."""
    doc = _rendered(county)
    assert "fetch(" not in doc
    assert "XMLHttpRequest" not in doc
    assert "cell_deg" in doc, "payload should be inline, not referenced"
