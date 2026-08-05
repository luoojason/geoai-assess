"""Assessment map renderer — produces a multi-layer Folium map for sub-tract analysis."""
from __future__ import annotations

import geopandas as gpd
import folium


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
        label_html = f"""
        <div style="
            position: fixed; top: 10px; left: 10px; z-index: 1000;
            background: white; padding: 7px 12px; border-radius: 6px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.25); font-size: 13px;
            font-family: sans-serif;
        "><b>{place_name}</b><br>
        <span style="color:#555">Study area · full county boundary</span></div>
        """
        m.get_root().html.add_child(folium.Element(label_html))

    m.fit_bounds([[south, west], [north, east]])
    return m._repr_html_()


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
    m = folium.Map(location=[centroid.y, centroid.x], zoom_start=13, tiles="CartoDB positron")

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

    folium.LayerControl(collapsed=False).add_to(m)

    # A metric that is absent or explicitly None was not measured (its source
    # layer failed to fetch). Printing the old 0.0 default drew a confident
    # "Greenspace Coverage: 0.0%" over a map of a county whose park data never
    # arrived, so say so instead.
    _MISSING = "not measured"

    def _fmt(key, spec, suffix=""):
        v = metrics.get(key)
        return _MISSING if v is None else f"{v:{spec}}{suffix}"

    bldg_pct = _fmt("building_coverage_ratio", ".1%")
    green_pct = _fmt("greenspace_coverage_ratio", ".1%")
    road_den = _fmt("road_density_km_per_km2", ".1f", " km/km&sup2;")
    park_pct = _fmt("parking_coverage_ratio", ".1%")
    ped_den = _fmt("pedestrian_density_km_per_km2", ".1f", " km/km&sup2;")
    transit_ct = _fmt("transit_stop_count", "d")
    facility_ct = _fmt("facility_count", "d")
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
    </div>
    """
    m.get_root().html.add_child(folium.Element(panel_html))

    return m._repr_html_()
