"""Assessment map renderer — produces a multi-layer Folium map for sub-tract analysis."""
from __future__ import annotations

import logging

import geopandas as gpd
import folium

logger = logging.getLogger(__name__)

# 0, not 16: the viewport tier now draws at EVERY zoom, because there is no
# longer a second (sampled) tier for it to hand over to below a gate. The gate
# existed to protect a county-wide view from drawing everything at once —
# measured 2026-08-13 on New York County, the densest case to hand, drawing all
# 96,294 clipped features at county zoom costs 202 ms once and 82-96 ms per pan,
# against a 100 ms budget. The sampled overview it was protecting cost far more
# than that: at 1-in-58 it drew about one building per two city blocks, which
# reads as "the map didn't load".
GATE_ZOOM = 0


# OSM `landuse=*` says what an area of land is FOR — a zone, not an object, and
# independent of whatever is built on it. Measured on Adams County: 18 distinct
# values, and only 51% of buildings have any landuse polygon under them at all,
# because drawing zones is optional work most mappers skip.
#
# Drawing all 18 in one colour (how landuse first shipped, 2026-08-13) makes
# farmland, a cemetery, a quarry and a housing estate pixel-identical: the layer
# says the land is spoken for and nothing else. These classes are the coarsest
# split that still answers "for what?", and they line up with what
# `land_use_mix` measures.
#
# Every value is kept, including the four `_GREENSPACE_TAGS` also fetches
# (grass, forest, meadow, recreation_ground — 31% of the greenspace layer by
# area). Dropping them would make the Land use layer a subset of what
# `land_use_mix` counts: a map disagreeing with its own metric, which is the
# defect class this area keeps producing. They genuinely overlap; showing that
# beats hiding it.
_LANDUSE_CLASSES = (
    # label, colour, member values
    ("Farmland & pasture", "#c9b072",
     ("farmland", "farmyard", "meadow", "animal_keeping", "orchard", "vineyard",
      "greenhouse_horticulture", "allotments", "aquaculture", "plant_nursery")),
    ("Grass, forest & parks", "#8fae7b",
     ("grass", "forest", "recreation_ground", "village_green", "flowerbed",
      "park", "greenfield")),
    ("Residential", "#d8a48f", ("residential",)),
    ("Commercial & retail", "#c58fbf", ("commercial", "retail", "office")),
    ("Industrial & extractive", "#9b8579",
     ("industrial", "quarry", "landfill", "brownfield", "construction",
      "railway", "port", "depot")),
    ("Civic & institutional", "#8fa9c4",
     ("cemetery", "religious", "institutional", "education", "military",
      "school", "hospital")),
)
_LANDUSE_OTHER = ("Other land use", "#b3aca4")
_LANDUSE_CLASS_OF = {v: i for i, (_l, _c, vals) in enumerate(_LANDUSE_CLASSES)
                     for v in vals}


def landuse_class_index(value) -> int:
    """Map a raw OSM `landuse=*` value to an index into _LANDUSE_CLASSES.

    Anything unlisted — a value new to OSM, a typo, or a NaN from a feature
    that matched on some other tag — lands on the "Other" bucket rather than
    being dropped. A value that vanished from the map because nobody had
    enumerated it would be the same silent-omission defect as the rest of
    this area's history.
    """
    try:
        return _LANDUSE_CLASS_OF.get(str(value).strip().lower(), len(_LANDUSE_CLASSES))
    except Exception:
        return len(_LANDUSE_CLASSES)


def _landuse_styles(base: dict) -> list:
    """One style per class, in class-index order, with "Other" last.

    Shares the base style's opacity and stroke weight so the classes differ
    only in hue — the point is to tell farmland from a quarry, not to make
    landuse louder than the buildings drawn on top of it.
    """
    out = []
    for _label, colour, _vals in _LANDUSE_CLASSES + (
            (_LANDUSE_OTHER[0], _LANDUSE_OTHER[1], ()),):
        st = dict(base)
        st["fillColor"] = colour
        st["color"] = colour
        out.append(st)
    return out


# Draw order is bottom-to-top, and it is load-bearing: landuse (farmland,
# forest, meadow — the large background polygons) has to sit UNDER greenspace
# and buildings or it buries them. Landuse was fetched, counted in the step-3
# inventory and shown in the layer list from the beginning, but had no draw
# path at all until 2026-08-13 — reported as "the green is missing" on a rural
# county, where landuse is most of the green there is.
# key, LayerControl label, on by default, geometry shape, style
_LAYER_SPEC = (
    ("landuse", "Land use", True, "polygon",
     {"fillColor": "#a3b18a", "fillOpacity": 0.30, "color": "#84957a", "weight": 0.3}),
    ("greenspace", "Greenspace", True, "polygon",
     {"fillColor": "#2d8a4e", "fillOpacity": 0.5, "color": "#1a5c33", "weight": 0.5}),
    ("parking", "Parking", False, "polygon",
     {"fillColor": "#e07b00", "fillOpacity": 0.5, "color": "#a05500", "weight": 0.5}),
    ("buildings", "Buildings", True, "polygon",
     {"fillColor": "#555555", "fillOpacity": 0.6, "color": "#333333", "weight": 0.5}),
    ("roads", "Roads", True, "polyline",
     {"color": "#3a7abf", "weight": 1, "opacity": 0.8, "fill": False}),
    ("sidewalks", "Sidewalks", False, "polyline",
     {"color": "#7b4fa6", "weight": 1.5, "opacity": 0.8, "fill": False}),
    ("transit_stops", "Transit Stops", False, "point",
     {"fillColor": "#0d9488", "fillOpacity": 0.9, "color": "#0f766e", "weight": 1, "radius": 4}),
    ("facilities", "Facilities", False, "point",
     {"fillColor": "#c2410c", "fillOpacity": 0.9, "color": "#9a3412", "weight": 1, "radius": 4}),
)


def _clip_to_study_area(gdf: gpd.GeoDataFrame, study_area: gpd.GeoDataFrame):
    """Only the part of each feature that lies inside the study area.

    OSM layers are fetched by BOUNDING BOX, so a county-shaped study area comes
    back with features in the bbox corners that are not in the county at all.
    The metrics have always excluded them (they clip); the map drew them, so a
    reader saw green blobs sitting outside the boundary and reasonably concluded
    either the outline or the number was wrong. Reported from a live Adams
    County run, 2026-08-09. The map must show the ground the metrics describe.
    """
    if gdf is None or gdf.empty:
        return gdf
    try:
        sa = study_area.to_crs(gdf.crs).geometry.union_all()
        out = gdf.copy()
        out["geometry"] = out.geometry.intersection(sa)
        # Empties first, THEN notna: geopandas warns about notna() on a series
        # that still contains empty geometries. A feature wholly outside the
        # study area clips to empty and is dropped, not drawn at zero size.
        out = out[~out.geometry.is_empty]
        return out[out.geometry.notna()]
    except Exception:  # a drawing aid must never fail the step
        return gdf


def render_base_map(study_area: gpd.GeoDataFrame, place_name: str = "") -> str:
    """A lightweight locator map: the study-area (county) boundary drawn over
    CartoDB base tiles, fit to the boundary's extent. Shown from step 1 so the
    Map View carries real context before the analysis runs (Prof. Shi 2026-07-27).

    Raises ValueError if study_area is empty or None.
    """
    if study_area is None or study_area.empty:
        raise ValueError("study_area must be a non-empty GeoDataFrame")

    study_4326 = study_area.to_crs("EPSG:4326")
    west, south, east, north = study_4326.geometry.total_bounds
    centroid = study_4326.geometry.union_all().centroid
    m = folium.Map(location=[centroid.y, centroid.x], zoom_start=10, tiles="CartoDB positron")

    fg = folium.FeatureGroup(name=place_name or "Study area", show=True)
    folium.GeoJson(
        study_4326.__geo_interface__,
        style_function=lambda _: {
            "fillColor": "#0d9488",
            "fillOpacity": 0.10,
            "color": "#0f766e",
            "weight": 2,
        },
    ).add_to(fg)
    fg.add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)

    if place_name:
        # left:56px, not 10px: Leaflet's zoom buttons occupy the top-left corner,
        # and a fixed-position div at 10px sits directly on top of them.
        label_html = f"""
        <div style="
            position: fixed; top: 10px; left: 56px; z-index: 1000;
            background: white; padding: 7px 12px; border-radius: 6px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.25); font-size: 13px;
            font-family: sans-serif;
        "><b>{place_name}</b><br>
        <span style="color:#555">Study area · full county boundary</span></div>
        """
        m.get_root().html.add_child(folium.Element(label_html))

    m.fit_bounds([[south, west], [north, east]])
    return m._repr_html_()


def _sample_legend_line(layer: str, info: dict | None, has_detail: bool = False) -> str:
    """One honest sentence about how much of a layer is drawn, or "" if all of it.

    `has_detail=False` (the default) deliberately does NOT promise "zoom in for
    all" — that is only true once the full-detail viewport payload for this
    layer is actually embedded in the page. `has_detail=True` is the caller's
    assertion that it is.
    """
    if not info or info.get("stride", 1) <= 1:
        return ""
    shown, total, stride = info["shown"], info["total"], info["stride"]
    tail = (" Zoom in and every one in view is drawn."
            if has_detail else
            f" All {total:,} were measured and counted in the metrics above.")
    return (f"<b>{layer}</b>: showing {shown:,} of {total:,} (1 in {stride}) at this "
            f"zoom.{tail}")


def render_assessment_map(
    study_area: gpd.GeoDataFrame,
    buildings: gpd.GeoDataFrame,
    greenspace: gpd.GeoDataFrame,
    roads: gpd.GeoDataFrame,
    metrics: dict,
    parking: gpd.GeoDataFrame | None = None,
    sidewalks: gpd.GeoDataFrame | None = None,
    transit_stops: gpd.GeoDataFrame | None = None,
    facilities: gpd.GeoDataFrame | None = None,
    sample_info: dict | None = None,
    detail_payloads: dict | None = None,
    gate_zoom: int = GATE_ZOOM,
    landuse: gpd.GeoDataFrame | None = None,
) -> str:
    """Render every confirmed layer onto a single Folium map.

    Each non-empty layer becomes a FeatureGroup drawn by the viewport module,
    which holds every feature and paints the ones the camera covers. A metrics
    panel is shown in the top-right corner. Returns m._repr_html_().

    `landuse` sits last in the signature rather than beside the other
    polygon layers because it was added on 2026-08-13, after the positional
    arguments were already load-bearing for existing callers and tests. Pass
    it by keyword.

    Raises:
        ValueError: if study_area is empty or None.
    """
    if study_area is None or study_area.empty:
        raise ValueError("study_area must be a non-empty GeoDataFrame")

    study_4326 = study_area.to_crs("EPSG:4326")
    centroid = study_4326.geometry.union_all().centroid
    m = folium.Map(location=[centroid.y, centroid.x], zoom_start=13, tiles="CartoDB positron",
                    prefer_canvas=True)

    # Draw only what was measured (see _clip_to_study_area).
    buildings = _clip_to_study_area(buildings, study_area)
    greenspace = _clip_to_study_area(greenspace, study_area)
    roads = _clip_to_study_area(roads, study_area)
    parking = _clip_to_study_area(parking, study_area)
    sidewalks = _clip_to_study_area(sidewalks, study_area)
    transit_stops = _clip_to_study_area(transit_stops, study_area)
    facilities = _clip_to_study_area(facilities, study_area)
    landuse = _clip_to_study_area(landuse, study_area)

    # Study area boundary
    fg_area = folium.FeatureGroup(name="Study Area", show=True)
    folium.GeoJson(
        study_4326.__geo_interface__,
        style_function=lambda _: {
            "fillOpacity": 0.0,
            "color": "#333333",
            "weight": 2,
        },
    ).add_to(fg_area)
    fg_area.add_to(m)

    # Filled in below for each layer with
    # (overview_feature_group_js_name, overview_geojson_layer_js_name), so
    # the viewport-layer JS emitted further down can nest the detail canvas
    # inside the SAME FeatureGroup the LayerControl already governs, and can
    # hide the specific overview GeoJson sublayer above the gate zoom without
    # touching the FeatureGroup's own on/off state. See the detail_payloads
    # block below for why: `HCASViewportLayer.group.addTo(this.map)` used to
    # attach the canvas layer directly to the map, invisible to
    # `LayerControl` and impossible to switch off — worst for sidewalks,
    # which default to `show=False` yet still appeared, undismissably, at
    # z16.
    _overview_refs: dict = {}

    frames = {
        "landuse": landuse, "greenspace": greenspace, "buildings": buildings,
        "parking": parking, "roads": roads, "sidewalks": sidewalks,
        "transit_stops": transit_stops, "facilities": facilities,
    }
    for key, label, show, _shape, style in _LAYER_SPEC:
        gdf = frames.get(key)
        payload = (detail_payloads or {}).get(key)
        has_payload = bool(payload and payload.get("total"))
        # A layer exists on the map if EITHER tier has something to draw. The
        # payload is the authority: `gdf` is only still consulted so that a
        # layer whose payload build failed keeps its old GeoJson draw path
        # rather than disappearing.
        if not has_payload and (gdf is None or gdf.empty):
            continue
        fg = folium.FeatureGroup(name=label, show=show)
        gj_name = None
        if not has_payload:
            # Fallback only. Every layer normally draws through the viewport
            # tier below; this path exists so a failed payload degrades to the
            # previous behaviour instead of an empty map.
            gj = folium.GeoJson(
                gdf.to_crs("EPSG:4326")[["geometry"]].__geo_interface__,
                style_function=(lambda s: (lambda _: s))(style),
            )
            gj.add_to(fg)
            gj_name = gj.get_name()
        fg.add_to(m)
        _overview_refs[key] = (fg.get_name(), gj_name)

    # Top-LEFT, stacking under the zoom buttons. Leaflet puts the layer control
    # in the top-right corner by default, which is where the metrics panel below
    # is pinned — the two overlapped and neither could be read (reported from a
    # live run, 2026-08-09). Leaflet stacks controls within a corner, so sharing
    # top-left with the zoom is safe; sharing a corner with a fixed-position div
    # is not.
    folium.LayerControl(collapsed=False, position="topleft").add_to(m)

    # Three different states, and they are NOT the same claim:
    #   value present   -> measured
    #   key present, None -> requested, but its source layer failed this run
    #   key absent      -> never requested (step 2 did not select this factor)
    # These last two both used to print "not measured", which reads as a failure
    # for a metric nobody asked for — the question "why are there no buildings?"
    # on the 2026-08-09 Adams County map, where buildings were simply not part of
    # the confirmed factor list while five other layers really had failed.
    _FAILED = "not measured"
    _UNSELECTED = "not selected"

    def _fmt(key, spec, suffix=""):
        if key not in metrics:
            return _UNSELECTED
        v = metrics.get(key)
        if v is None:
            return _FAILED
        try:
            return f"{v:{spec}}{suffix}"
        except (ValueError, TypeError):
            # A count arriving as a float would raise on "d" and take the whole
            # map with it. The map is an aid, not the record — print the value.
            return f"{v}{suffix}"

    bldg_pct = _fmt("building_coverage_ratio", ".1%")
    green_pct = _fmt("greenspace_coverage_ratio", ".1%")
    road_den = _fmt("road_density_km_per_km2", ".1f", " km/km&sup2;")
    park_pct = _fmt("parking_coverage_ratio", ".1%")
    ped_den = _fmt("pedestrian_density_km_per_km2", ".1f", " km/km&sup2;")
    transit_ct = _fmt("transit_stop_count", "d")
    facility_ct = _fmt("facility_count", "d")

    # Spell out what the two absence words mean, but only when one is on screen —
    # a reader should not have to guess whether a blank map means "this county
    # has none of it", "the data source failed", or "nobody asked for it".
    shown = (bldg_pct, green_pct, road_den, park_pct, ped_den, transit_ct, facility_ct)
    keys = []
    if _FAILED in shown:
        keys.append("<b>not measured</b> = its data source could not be reached this "
                    "run, so nothing is drawn and no finding was derived from it")
    if _UNSELECTED in shown:
        keys.append("<b>not selected</b> = this factor is not in the confirmed list "
                    "for this assessment, so it was never fetched")
    # Every layer, not just buildings/roads/sidewalks. Those three were the only
    # ones over the old 3,500 cap on the county this was designed against, and
    # scoping the legend to them meant a denser county sampled the others in
    # silence: New York County fetched 11,314 greenspace features, so greenspace
    # drew at 1 in 4 with nothing on screen saying so. Nothing is sampled now
    # (stride is always 1), so these lines self-suppress — the loop stays whole
    # so that if sampling ever comes back it cannot come back invisibly.
    for _layer, _lbl, _sh, _shp, _st in _LAYER_SPEC:
        # Per-layer, not a single bool(detail_payloads) for all of them:
        # hcas.py builds each layer's payload in its own try/except, so one
        # layer can fail (dropped, logged) while the rest succeed.
        # Tested against `total`, not mere membership: an empty layer now
        # leaves a zero-total entry in the dict, and the JS-emission loop
        # skips those — so membership alone could claim detail for a layer
        # that has no HCASViewportLayer at all. A dict-wide truthiness check would then have
        # the failed layer's legend line claim "zoom in and every one in
        # view is drawn" for a layer with no embedded payload and no
        # HCASViewportLayer ever registered for it — exactly the
        # legend-overstates-the-map defect this whole plan exists to fix.
        _line = _sample_legend_line(
            _layer, (sample_info or {}).get(_layer),
            has_detail=bool((detail_payloads or {}).get(_layer, {}).get("total")),
        )
        if _line:
            keys.append(_line)
    # Landuse draws in several colours, so it needs a key or the colours mean
    # nothing. Only emitted when the layer is actually on the map, and only for
    # the classes this county actually has — a legend listing seven classes for
    # a county with three is its own small lie.
    lu_payload = (detail_payloads or {}).get("landuse") or {}
    present = sorted(set(lu_payload.get("classes") or []))
    if present:
        _all = list(_LANDUSE_CLASSES) + [(_LANDUSE_OTHER[0], _LANDUSE_OTHER[1], ())]
        swatches = "".join(
            f'<span style="display:inline-block;width:9px;height:9px;'
            f'background:{_all[i][1]};border:1px solid rgba(0,0,0,.25);'
            f'margin-right:5px;vertical-align:middle;"></span>{_all[i][0]}<br>'
            for i in present if 0 <= i < len(_all))
        keys.append("<b>land use</b> = what an area of land is FOR, independent of "
                    "what is built on it:<br>" + swatches)

    legend_html = (
        '<div style="margin-top:8px;padding-top:7px;border-top:1px solid #e5e5e5;'
        'font-size:11px;color:#555;max-width:250px;line-height:1.45;">'
        + "<br>".join(keys) + "</div>"
    ) if keys else ""

    panel_html = f"""
    <div style="
        position: fixed; top: 10px; right: 10px; z-index: 1000;
        background: white; padding: 10px 14px; border-radius: 6px;
        box-shadow: 0 2px 6px rgba(0,0,0,0.25); font-size: 13px;
        font-family: sans-serif; line-height: 1.6;
    ">
        <b style="font-size:14px">Assessment Metrics</b><br>
        Building Coverage: <b>{bldg_pct}</b><br>
        Greenspace Coverage: <b>{green_pct}</b><br>
        Road Density: <b>{road_den}</b><br>
        Parking Coverage: <b>{park_pct}</b><br>
        Pedestrian Density: <b>{ped_den}</b><br>
        Transit Stops: <b>{transit_ct}</b><br>
        Facilities: <b>{facility_ct}</b>
        {legend_html}
    </div>
    """
    m.get_root().html.add_child(folium.Element(panel_html))

    # Full-detail viewport layers: every feature is embedded, but only the
    # cells the camera currently covers are drawn, and only above gate_zoom.
    # MUST land in folium's SCRIPT section, not its HTML section — folium
    # renders html before script, so m.get_name()'s map variable does not
    # exist yet if this runs from the html section, and the layers silently
    # never attach. folium.Element here is raw JS with NO <script> tags: the
    # script section is already inside one.
    #
    # Landing in the script section is NOT enough on its own: folium emits
    # its own `L.map(...)` call into that same script section too, placed by
    # its own Jinja template, and a child added here can land either before
    # OR after it depending on internal render order (measured: it landed
    # BEFORE, so m.get_name() was still undefined when our code ran —
    # TypeError, and the layers never attached; confirmed against real
    # Leaflet in a browser, not caught by any unit test, stubbed JS harness,
    # or two prior code reviews). The one placement-independent fix is to
    # defer to `window`'s `load` event, which fires only after every inline
    # script on the page — including wherever folium put L.map(...) —  has
    # already run. Do not go back to guessing placement in the script
    # section; only the deferral is reliable regardless of what folium does
    # around it.
    if detail_payloads:
        import json
        from pathlib import Path

        # A drawing aid never fails the step: viewport_layers.js is the only
        # I/O in this function, and an OSError here (permissions, a moved
        # install, a partial checkout) used to propagate straight up and
        # take the whole map down with it. Degrade to the overview-only map
        # instead — everything below this except block is skipped, exactly
        # as if detail_payloads had been empty.
        js = None
        try:
            js = (Path(__file__).resolve().parents[2]
                  / "frontend" / "static" / "viewport_layers.js").read_text(encoding="utf-8")
        except OSError as _exc:
            logger.warning(f"could not read viewport_layers.js, shipping overview-only map: {_exc}")

        if js is not None:
            # Both tables come from _LAYER_SPEC now, so the styles the viewport
            # tier paints with cannot drift from the ones the GeoJson fallback
            # uses, and adding a layer means adding one row rather than editing
            # three dicts. Roads and sidewalks are LineStrings ('polyline' draws
            # their first ring open, where the 'polygon' default would close
            # each one with a spurious segment back to its first point); transit
            # stops and facilities are Points ('point' -> circleMarker, since
            # polygon/polyline on a one-coordinate ring draw nothing at all).
            _styles = {k: st for k, _l, _s, _sh, st in _LAYER_SPEC}
            _shapes = {k: sh for k, _l, _s, sh, _st in _LAYER_SPEC}
            blocks = [js]
            # _LAYER_SPEC order, NOT detail_payloads order. Each
            # HCASViewportLayer builds its own L.canvas and passes it per-path,
            # and Leaflet appends renderer containers to the pane with no
            # z-index — so stacking follows ATTACH order, which is the order
            # these blocks are emitted in. detail_payloads arrives keyed by
            # hcas.py's `names = sorted(...)`, i.e. alphabetically, which put
            # landuse (4th) on top of buildings (1st) and greenspace (3rd):
            # a 30%-opacity farmland wash over every building footprint on a
            # rural county, which is the opposite of what _LAYER_SPEC's own
            # comment promises. Any payload key not in the spec is emitted
            # last rather than dropped.
            _order = [k for k, _l, _s, _sh, _st in _LAYER_SPEC]
            _names = ([n for n in _order if n in detail_payloads]
                      + [n for n in detail_payloads if n not in _order])
            for name in _names:
                payload = detail_payloads.get(name)
                if not payload or not payload.get("total"):
                    continue
                opts = {
                    "gateZoom": gate_zoom,
                    "style": _styles.get(name, {}),
                    "shape": _shapes.get(name, "polygon"),
                }
                # `styles` + the payload's own `classes` array let one layer
                # draw in several colours. Only landuse uses it; every other
                # layer keeps the single `style` above and the JS never looks
                # at `styles` for them.
                if payload.get("classes") is not None:
                    opts["styles"] = _landuse_styles(_styles.get(name, {}))
                # overview_group/overview_layer: the JS var names of this
                # layer's own overview FeatureGroup and the GeoJson sublayer
                # inside it (set above, keyed by layer name). Passed as bare
                # JS identifiers (4th/5th constructor args), not through
                # `opts`/json.dumps — `opts` must stay valid JSON on its own
                # because test_viewport_layer_shape_matches_geometry_type
                # decodes it directly with json.JSONDecoder.raw_decode.
                # HCASViewportLayer nests its canvas group inside that
                # FeatureGroup (instead of adding straight to the map) so
                # LayerControl governs both tiers as one unit, and hides that
                # specific GeoJson sublayer above the gate zoom so the two
                # tiers never draw the same features at once. `null` in the normal
                # path, because gj_name is set ONLY in the no-payload fallback
                # branch — a layer cannot have both a viewport tier and an
                # overview to hide — the layer then falls back to attaching
                # straight to the map, same as before this fix.
                fg_name, gj_name = _overview_refs.get(name, (None, None))
                ctor_args = "%s,p,%s,%s,%s" % (
                    m.get_name(), json.dumps(opts),
                    fg_name if fg_name else "null",
                    gj_name if gj_name else "null",
                )
                # Each layer's instantiation gets its OWN try/catch: these
                # three statements used to be bare siblings in one script
                # tag, so an exception thrown building the FIRST layer
                # (buildings) aborted the rest of that tag — roads and
                # sidewalks never even attempted to instantiate. Logged to
                # console rather than swallowed, so a real failure is still
                # visible to whoever opens devtools.
                blocks.append(
                    "try{(function(){var p=%s;new HCASViewportLayer(%s).attach();})();}"
                    "catch(e){console.error(%s,e);}"
                    % (json.dumps(payload), ctor_args,
                       json.dumps(f"HCAS viewport layer ({name}) failed to attach:"))
                )
            # window.addEventListener("load", ...) — not a bare top-level block —
            # so this runs after every inline script has executed, regardless of
            # whether folium's own `L.map(...)` call landed before or after this
            # one in the script section.
            m.get_root().script.add_child(folium.Element(
                'window.addEventListener("load", function () {\n'
                + "\n".join(blocks)
                + "\n});"
            ))

    # Fit to the study area, as the step-1 locator map already did. A fixed
    # zoom_start=13 centred on the centroid opens a county-wide assessment
    # showing a few km of its middle, so the analyst has to zoom out before the
    # map means anything — and the study-area outline, the one thing every layer
    # is relative to, is off screen.
    west, south, east, north = study_4326.geometry.total_bounds
    m.fit_bounds([[south, west], [north, east]])

    return m._repr_html_()
