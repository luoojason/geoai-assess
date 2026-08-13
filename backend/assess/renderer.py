"""Assessment map renderer — produces a multi-layer Folium map for sub-tract analysis."""
from __future__ import annotations

import geopandas as gpd
import folium

# Lowest zoom whose densest real-world viewport (Delaware County, 1200x800px
# pane) holds <=5,000 building features — see
# docs/measurements/2026-08-12-map-payload-sizes.md. Below this zoom the
# viewport module stays gated off and the thinned overview (drawn by Folium
# in the ordinary way) is what's on screen.
GATE_ZOOM = 16


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
) -> str:
    """Render buildings, greenspace, and roads onto a single Folium map.

    Each non-empty layer becomes a FeatureGroup. A metrics panel is shown
    in the top-right corner. Returns m._repr_html_().

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

    if not buildings.empty:
        fg_b = folium.FeatureGroup(name="Buildings", show=True)
        folium.GeoJson(
            buildings.to_crs("EPSG:4326").__geo_interface__,
            style_function=lambda _: {
                "fillColor": "#555555",
                "fillOpacity": 0.6,
                "color": "#333333",
                "weight": 0.5,
            },
        ).add_to(fg_b)
        fg_b.add_to(m)

    if not greenspace.empty:
        fg_g = folium.FeatureGroup(name="Greenspace", show=True)
        folium.GeoJson(
            greenspace.to_crs("EPSG:4326").__geo_interface__,
            style_function=lambda _: {
                "fillColor": "#2d8a4e",
                "fillOpacity": 0.5,
                "color": "#1a5c33",
                "weight": 0.5,
            },
        ).add_to(fg_g)
        fg_g.add_to(m)

    if not roads.empty:
        fg_r = folium.FeatureGroup(name="Roads", show=True)
        folium.GeoJson(
            roads.to_crs("EPSG:4326").__geo_interface__,
            style_function=lambda _: {
                "color": "#3a7abf",
                "weight": 1,
                "opacity": 0.8,
            },
        ).add_to(fg_r)
        fg_r.add_to(m)

    if parking is not None and not parking.empty:
        fg_p = folium.FeatureGroup(name="Parking", show=False)
        folium.GeoJson(
            parking.to_crs("EPSG:4326").__geo_interface__,
            style_function=lambda _: {
                "fillColor": "#e07b00",
                "fillOpacity": 0.5,
                "color": "#a05500",
                "weight": 0.5,
            },
        ).add_to(fg_p)
        fg_p.add_to(m)

    if sidewalks is not None and not sidewalks.empty:
        fg_sw = folium.FeatureGroup(name="Sidewalks", show=False)
        folium.GeoJson(
            sidewalks.to_crs("EPSG:4326").__geo_interface__,
            style_function=lambda _: {
                "color": "#7b4fa6",
                "weight": 1.5,
                "opacity": 0.8,
            },
        ).add_to(fg_sw)
        fg_sw.add_to(m)

    if transit_stops is not None and not transit_stops.empty:
        fg_ts = folium.FeatureGroup(name="Transit Stops", show=False)
        folium.GeoJson(
            transit_stops.to_crs("EPSG:4326").__geo_interface__,
            style_function=lambda _: {
                "fillColor": "#0d9488",
                "fillOpacity": 0.9,
                "color": "#0f766e",
                "weight": 1,
                "radius": 5,
            },
        ).add_to(fg_ts)
        fg_ts.add_to(m)

    if facilities is not None and not facilities.empty:
        fg_f = folium.FeatureGroup(name="Facilities", show=False)
        folium.GeoJson(
            facilities.to_crs("EPSG:4326")[["geometry"]].__geo_interface__,
            style_function=lambda _: {
                "fillColor": "#c2410c",
                "fillOpacity": 0.9,
                "color": "#9a3412",
                "weight": 1,
                "radius": 5,
            },
        ).add_to(fg_f)
        fg_f.add_to(m)

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
    for _layer in ("buildings", "roads", "sidewalks"):
        _line = _sample_legend_line(
            _layer, (sample_info or {}).get(_layer),
            has_detail=bool(detail_payloads),
        )
        if _line:
            keys.append(_line)
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
    if detail_payloads:
        import json
        from pathlib import Path

        js = (Path(__file__).resolve().parents[2]
              / "frontend" / "static" / "viewport_layers.js").read_text(encoding="utf-8")
        # No encoder: map_payload.py emits plain 6-decimal-place coordinates,
        # so there is no `encoded:` option to pass.
        _styles = {
            "buildings": {"color": "#333333", "weight": 0.5,
                          "fillColor": "#555555", "fillOpacity": 0.6},
            "roads": {"color": "#3a7abf", "weight": 1, "fill": False},
            "sidewalks": {"color": "#7b4fa6", "weight": 1.5, "fill": False},
        }
        # Roads and sidewalks are LineStrings: 'polyline' draws only their
        # first ring open, whereas the 'polygon' default would close each one
        # with a spurious segment back to its first point.
        _shapes = {"buildings": "polygon", "roads": "polyline", "sidewalks": "polyline"}
        blocks = [js]
        for name, payload in detail_payloads.items():
            if not payload or not payload.get("total"):
                continue
            opts = {
                "gateZoom": gate_zoom,
                "style": _styles.get(name, {}),
                "shape": _shapes.get(name, "polygon"),
            }
            blocks.append(
                "(function(){var p=%s;new HCASViewportLayer(%s,p,%s).attach();})();"
                % (json.dumps(payload), m.get_name(), json.dumps(opts))
            )
        m.get_root().script.add_child(folium.Element("\n".join(blocks)))

    # Fit to the study area, as the step-1 locator map already did. A fixed
    # zoom_start=13 centred on the centroid opens a county-wide assessment
    # showing a few km of its middle, so the analyst has to zoom out before the
    # map means anything — and the study-area outline, the one thing every layer
    # is relative to, is off screen.
    west, south, east, north = study_4326.geometry.total_bounds
    m.fit_bounds([[south, west], [north, east]])

    return m._repr_html_()
