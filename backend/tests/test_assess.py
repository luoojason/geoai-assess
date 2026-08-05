"""Tests for the assessment pipeline (backend/assess/).

All OSM network calls are mocked — no live internet required.
"""
from __future__ import annotations

from unittest.mock import patch

import geopandas as gpd
import networkx as nx
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import LineString, box


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sq(minx: float, miny: float, maxx: float, maxy: float, crs: str = "EPSG:5070") -> gpd.GeoDataFrame:
    """Return a single-row GeoDataFrame with a square polygon."""
    return gpd.GeoDataFrame(geometry=[box(minx, miny, maxx, maxy)], crs=crs)


def _line_gdf(length_m: float = 1000.0, crs: str = "EPSG:5070") -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(geometry=[LineString([(0, 0), (length_m, 0)])], crs=crs)


def _empty_gdf(crs: str = "EPSG:5070") -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(geometry=[], crs=crs)


# 1 km × 1 km study area in EPSG:5070
STUDY = _sq(0, 0, 1000, 1000)


# ---------------------------------------------------------------------------
# TestOsmFetchers
# ---------------------------------------------------------------------------

class TestOsmFetchers:
    def test_fetch_buildings_returns_geodataframe(self):
        from backend.assess.osm_layers import fetch_buildings

        fake_gdf = gpd.GeoDataFrame(geometry=[box(-83.1, 39.9, -83.0, 40.0)], crs="EPSG:4326")
        with patch("backend.assess.osm_layers.ox.features_from_bbox", return_value=fake_gdf):
            result = fetch_buildings((-83.1, 39.9, -83.0, 40.0))
        assert isinstance(result, gpd.GeoDataFrame)
        assert result.crs.to_epsg() == 5070

    def test_fetch_greenspace_empty_returns_empty_gdf(self):
        from backend.assess.osm_layers import fetch_greenspace

        empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        with patch("backend.assess.osm_layers.ox.features_from_bbox", return_value=empty):
            result = fetch_greenspace((-83.1, 39.9, -83.0, 40.0))
        assert result.empty
        assert result.crs.to_epsg() == 5070

    def test_fetch_road_network_returns_edges(self):
        from backend.assess.osm_layers import fetch_road_network

        # Build a minimal fake graph that graph_to_gdfs can process
        fake_graph = nx.MultiDiGraph()
        fake_graph.graph["crs"] = "EPSG:4326"
        fake_graph.add_node(1, x=-83.05, y=39.95)
        fake_graph.add_node(2, x=-83.04, y=39.95)
        fake_graph.add_edge(1, 2, 0, geometry=LineString([(-83.05, 39.95), (-83.04, 39.95)]), length=100.0)

        fake_edges = gpd.GeoDataFrame(
            geometry=[LineString([(-83.05, 39.95), (-83.04, 39.95)])], crs="EPSG:4326"
        )
        with patch("backend.assess.osm_layers.ox.graph_from_bbox", return_value=fake_graph), \
             patch("backend.assess.osm_layers.ox.graph_to_gdfs", return_value=(None, fake_edges)):
            result = fetch_road_network((-83.1, 39.9, -83.0, 40.0))
        assert isinstance(result, gpd.GeoDataFrame)
        assert result.crs.to_epsg() == 5070

    def test_fetch_buildings_returns_empty_on_exception(self):
        from backend.assess.osm_layers import fetch_buildings

        with patch("backend.assess.osm_layers.ox.features_from_bbox", side_effect=Exception("Overpass timeout")):
            result = fetch_buildings((-83.1, 39.9, -83.0, 40.0))
        assert result.empty

    def test_fetch_road_network_returns_empty_on_exception(self):
        from backend.assess.osm_layers import fetch_road_network

        with patch("backend.assess.osm_layers.ox.graph_from_bbox", side_effect=Exception("network error")):
            result = fetch_road_network((-83.1, 39.9, -83.0, 40.0))
        assert result.empty


# ---------------------------------------------------------------------------
# TestMetrics
# ---------------------------------------------------------------------------

class TestMetrics:
    def test_building_coverage_ratio_correct(self):
        from backend.assess.metrics import compute_building_coverage

        # 500×500 building in 1000×1000 study area → 0.25
        buildings = _sq(0, 0, 500, 500)
        ratio = compute_building_coverage(buildings, STUDY)
        assert abs(ratio - 0.25) < 1e-6

    def test_building_coverage_empty_buildings_returns_zero(self):
        from backend.assess.metrics import compute_building_coverage

        assert compute_building_coverage(_empty_gdf(), STUDY) == 0.0

    def test_greenspace_coverage_returns_dict_with_keys(self):
        from backend.assess.metrics import compute_greenspace_coverage

        greenspace = _sq(0, 0, 500, 500)
        result = compute_greenspace_coverage(greenspace, STUDY)
        assert "area_m2" in result
        assert "ratio" in result
        assert abs(result["ratio"] - 0.25) < 1e-6
        assert result["area_m2"] > 0

    def test_greenspace_empty_returns_zero_dict(self):
        from backend.assess.metrics import compute_greenspace_coverage

        result = compute_greenspace_coverage(_empty_gdf(), STUDY)
        assert result == {"area_m2": 0.0, "ratio": 0.0}

    def test_road_density_correct(self):
        from backend.assess.metrics import compute_road_density

        # 1000m road in 1km² study area → 1.0 km/km²
        roads = _line_gdf(1000.0)
        density = compute_road_density(roads, STUDY)
        assert abs(density - 1.0) < 1e-6

    def test_road_density_empty_returns_zero(self):
        from backend.assess.metrics import compute_road_density

        assert compute_road_density(_empty_gdf(), STUDY) == 0.0

    def test_building_coverage_crs_mismatch_handled(self):
        from backend.assess.metrics import compute_building_coverage

        # Buildings in EPSG:4326 — function should reproject
        buildings_4326 = gpd.GeoDataFrame(
            geometry=[box(-83.01, 39.99, -83.00, 40.00)], crs="EPSG:4326"
        )
        # Should not raise
        ratio = compute_building_coverage(buildings_4326, STUDY)
        assert isinstance(ratio, float)
        assert 0.0 <= ratio <= 1.0


# ---------------------------------------------------------------------------
# TestFAR
# ---------------------------------------------------------------------------

class TestFAR:
    def test_far_correct(self):
        from backend.assess.metrics import compute_far

        # 2-story 500×500 building in 1000×1000 study area → FAR = 0.5
        buildings = gpd.GeoDataFrame(
            {"building:levels": ["2"]},
            geometry=[box(0, 0, 500, 500)],
            crs="EPSG:5070",
        )
        far = compute_far(buildings, STUDY)
        assert abs(far - 0.5) < 1e-6

    def test_far_empty_returns_zero(self):
        from backend.assess.metrics import compute_far

        assert compute_far(_empty_gdf(), STUDY) == 0.0

    def test_far_missing_levels_defaults_to_one(self):
        from backend.assess.metrics import compute_far

        # No building:levels column → treated as 1-story → FAR = 0.25
        buildings = _sq(0, 0, 500, 500)
        far = compute_far(buildings, STUDY)
        assert abs(far - 0.25) < 1e-6


# ---------------------------------------------------------------------------
# TestInterventions
# ---------------------------------------------------------------------------

class TestInterventions:
    def test_interventions_low_greenspace_flagged(self):
        from backend.assess.interventions import generate_interventions

        metrics = {
            "greenspace_coverage_ratio": 0.02,
            "greenspace_area_m2": 500.0,
            "building_coverage_ratio": 0.3,
            "road_density_km_per_km2": 5.0,
            "far": 1.0,
        }
        results = generate_interventions(metrics)
        assert len(results) > 0
        severities = {r["severity"] for r in results}
        assert "high" in severities

    def test_interventions_all_ok_returns_empty(self):
        from backend.assess.interventions import generate_interventions

        metrics = {
            "greenspace_coverage_ratio": 0.15,
            "greenspace_area_m2": 20_000.0,
            "building_coverage_ratio": 0.35,
            "road_density_km_per_km2": 8.0,
            "far": 1.5,
        }
        assert generate_interventions(metrics) == []

    def test_interventions_severity_keys_present(self):
        from backend.assess.interventions import generate_interventions

        metrics = {
            "greenspace_coverage_ratio": 0.02,
            "greenspace_area_m2": 500.0,
            "building_coverage_ratio": 0.75,
            "road_density_km_per_km2": 1.0,
            "far": 4.0,
        }
        results = generate_interventions(metrics)
        assert len(results) > 0
        for item in results:
            assert "category" in item
            assert "finding" in item
            assert "recommendation" in item
            assert "standard" in item
            assert item["severity"] in ("high", "medium", "low")


# ---------------------------------------------------------------------------
# TestAssessEndpoint
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    from backend.app import app
    return TestClient(app, headers={"X-Requested-With": "geoai"})


class TestAssessEndpoint:
    def test_assess_404_invalid_fips(self, client):
        with patch("backend.gis.collector_lite.collect_county_boundaries") as mock_cb:
            mock_cb.return_value = gpd.GeoDataFrame(
                {"GEOID": ["99999"], "NAME": ["Fake County"]},
                geometry=[box(-83.1, 39.9, -83.0, 40.0)],
                crs="EPSG:4326",
            )
            resp = client.post("/assess", json={"state_fips": "99", "county_fips": "000"})
        assert resp.status_code == 404

    def test_assess_200_with_mocked_osm(self, client):
        fake_poly_gdf = gpd.GeoDataFrame(geometry=[box(-83.1, 39.9, -83.0, 40.0)], crs="EPSG:4326")
        fake_line_gdf = gpd.GeoDataFrame(geometry=[LineString([(-83.05, 39.95), (-83.04, 39.95)])], crs="EPSG:4326")
        fake_empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")
        county_gdf = gpd.GeoDataFrame(
            {"GEOID": ["39049"], "NAME": ["Franklin County"]},
            geometry=[box(-83.2, 39.8, -82.8, 40.2)],
            crs="EPSG:4326",
        )

        with patch("backend.gis.collector_lite.collect_county_boundaries", return_value=county_gdf), \
             patch("backend.app.fetch_buildings", return_value=fake_poly_gdf.to_crs("EPSG:5070")), \
             patch("backend.app.fetch_greenspace", return_value=fake_poly_gdf.to_crs("EPSG:5070")), \
             patch("backend.app.fetch_road_network", return_value=fake_line_gdf.to_crs("EPSG:5070")), \
             patch("backend.app.fetch_parking", return_value=fake_empty), \
             patch("backend.app.fetch_sidewalks", return_value=fake_empty), \
             patch("backend.app.fetch_transit_stops", return_value=fake_empty), \
             patch("backend.app.fetch_facilities", return_value=fake_empty):
            resp = client.post("/assess", json={"state_fips": "39", "county_fips": "049"})

        assert resp.status_code == 200
        data = resp.json()
        assert "map_html" in data
        assert "metrics" in data
        assert "study_area_display" in data
        assert "building_coverage_ratio" in data["metrics"]
        assert "greenspace_coverage_ratio" in data["metrics"]
        assert "road_density_km_per_km2" in data["metrics"]
        assert "far" in data["metrics"]
        assert "parking_coverage_ratio" in data["metrics"]
        assert "pedestrian_density_km_per_km2" in data["metrics"]
        assert "transit_stop_count" in data["metrics"]
        assert "transit_stop_density_per_km2" in data["metrics"]
        assert "interventions" in data
        assert isinstance(data["interventions"], list)
        assert len(data["map_html"]) > 500

    def test_assess_200_with_custom_bbox(self, client):
        fake_gdf = gpd.GeoDataFrame(geometry=[box(-83.1, 39.9, -83.0, 40.0)], crs="EPSG:4326")
        fake_line_gdf = gpd.GeoDataFrame(geometry=[LineString([(-83.05, 39.95), (-83.04, 39.95)])], crs="EPSG:4326")
        fake_empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")

        with patch("backend.app.fetch_buildings", return_value=fake_gdf.to_crs("EPSG:5070")), \
             patch("backend.app.fetch_greenspace", return_value=fake_gdf.to_crs("EPSG:5070")), \
             patch("backend.app.fetch_road_network", return_value=fake_line_gdf.to_crs("EPSG:5070")), \
             patch("backend.app.fetch_parking", return_value=fake_empty), \
             patch("backend.app.fetch_sidewalks", return_value=fake_empty), \
             patch("backend.app.fetch_transit_stops", return_value=fake_empty), \
             patch("backend.app.fetch_facilities", return_value=fake_empty):
            resp = client.post("/assess", json={
                "state_fips": "39",
                "county_fips": "049",
                "bbox": [-83.1, 39.9, -83.0, 40.0],
            })

        assert resp.status_code == 200
        data = resp.json()
        assert "map_html" in data
        assert data["metrics"]["building_coverage_ratio"] >= 0.0


# ---------------------------------------------------------------------------
# TestNewOsmFetchers
# ---------------------------------------------------------------------------

class TestNewOsmFetchers:
    def test_fetch_parking_returns_gdf(self):
        from backend.assess.osm_layers import fetch_parking

        fake_gdf = gpd.GeoDataFrame(geometry=[box(-83.1, 39.9, -83.0, 40.0)], crs="EPSG:4326")
        with patch("backend.assess.osm_layers.ox.features_from_bbox", return_value=fake_gdf):
            result = fetch_parking((-83.1, 39.9, -83.0, 40.0))
        assert isinstance(result, gpd.GeoDataFrame)
        assert result.crs.to_epsg() == 5070

    def test_fetch_sidewalks_returns_gdf(self):
        from backend.assess.osm_layers import fetch_sidewalks

        fake_gdf = gpd.GeoDataFrame(
            geometry=[LineString([(-83.05, 39.95), (-83.04, 39.95)])], crs="EPSG:4326"
        )
        with patch("backend.assess.osm_layers.ox.features_from_bbox", return_value=fake_gdf):
            result = fetch_sidewalks((-83.1, 39.9, -83.0, 40.0))
        assert isinstance(result, gpd.GeoDataFrame)
        assert result.crs.to_epsg() == 5070

    def test_fetch_transit_stops_returns_gdf(self):
        from backend.assess.osm_layers import fetch_transit_stops
        from shapely.geometry import Point

        fake_gdf = gpd.GeoDataFrame(geometry=[Point(-83.05, 39.95)], crs="EPSG:4326")
        with patch("backend.assess.osm_layers.ox.features_from_bbox", return_value=fake_gdf):
            result = fetch_transit_stops((-83.1, 39.9, -83.0, 40.0))
        assert isinstance(result, gpd.GeoDataFrame)
        assert result.crs.to_epsg() == 5070

    def test_fetch_parking_returns_empty_on_exception(self):
        from backend.assess.osm_layers import fetch_parking

        with patch("backend.assess.osm_layers.ox.features_from_bbox", side_effect=Exception("timeout")):
            result = fetch_parking((-83.1, 39.9, -83.0, 40.0))
        assert result.empty


# ---------------------------------------------------------------------------
# TestNewMetrics
# ---------------------------------------------------------------------------

class TestNewMetrics:
    def test_parking_coverage_correct(self):
        from backend.assess.metrics import compute_parking_coverage

        parking = _sq(0, 0, 500, 500)
        ratio = compute_parking_coverage(parking, STUDY)
        assert abs(ratio - 0.25) < 1e-6

    def test_pedestrian_density_correct(self):
        from backend.assess.metrics import compute_pedestrian_density

        sidewalks = _line_gdf(1000.0)
        density = compute_pedestrian_density(sidewalks, STUDY)
        assert abs(density - 1.0) < 1e-6

    def test_transit_access_returns_dict(self):
        from backend.assess.metrics import compute_transit_access
        from shapely.geometry import Point

        stops = gpd.GeoDataFrame(geometry=[Point(500, 500), Point(200, 200)], crs="EPSG:5070")
        result = compute_transit_access(stops, STUDY)
        assert result["stop_count"] == 2
        assert result["stop_density_per_km2"] > 0.0

    def test_transit_empty_returns_zeros(self):
        from backend.assess.metrics import compute_transit_access

        result = compute_transit_access(_empty_gdf(), STUDY)
        assert result == {"stop_count": 0, "stop_density_per_km2": 0.0}


# ---------------------------------------------------------------------------
# TestFacilities
# ---------------------------------------------------------------------------

class TestFacilities:
    def test_fetch_facilities_mixes_points_and_polygon_centroids(self):
        from backend.assess.osm_layers import fetch_facilities
        from shapely.geometry import Point

        fake_gdf = gpd.GeoDataFrame(
            geometry=[Point(-83.05, 39.95), box(-83.06, 39.94, -83.055, 39.945)],
            crs="EPSG:4326",
        )
        with patch("backend.assess.osm_layers.ox.features_from_bbox", return_value=fake_gdf):
            result = fetch_facilities((-83.1, 39.9, -83.0, 40.0))
        assert isinstance(result, gpd.GeoDataFrame)
        assert result.crs.to_epsg() == 5070
        assert len(result) == 2  # polygon reduced to its centroid, not dropped
        assert set(result.geometry.geom_type) == {"Point"}

    def test_fetch_facilities_returns_empty_on_exception(self):
        from backend.assess.osm_layers import fetch_facilities

        with patch("backend.assess.osm_layers.ox.features_from_bbox", side_effect=Exception("timeout")):
            result = fetch_facilities((-83.1, 39.9, -83.0, 40.0))
        assert result.empty

    def test_facility_access_counts_and_density(self):
        from backend.assess.metrics import compute_facility_access
        from shapely.geometry import Point

        facilities = gpd.GeoDataFrame(
            geometry=[Point(100, 100), Point(200, 200)], crs="EPSG:5070"
        )
        result = compute_facility_access(facilities, STUDY)  # STUDY is 1 km²
        assert result["facility_count"] == 2
        assert result["facility_density_per_km2"] == pytest.approx(2.0)

    def test_facility_access_empty_returns_zeros(self):
        from backend.assess.metrics import compute_facility_access

        result = compute_facility_access(_empty_gdf(), STUDY)
        assert result == {"facility_count": 0, "facility_density_per_km2": 0.0}

    def test_renderer_includes_facilities_layer(self):
        from backend.assess.renderer import render_assessment_map
        from shapely.geometry import Point

        facilities = gpd.GeoDataFrame(geometry=[Point(500, 500)], crs="EPSG:5070")
        html = render_assessment_map(
            STUDY, _empty_gdf(), _empty_gdf(), _empty_gdf(),
            {"facility_count": 1}, facilities=facilities,
        )
        assert "Facilities" in html


# ---------------------------------------------------------------------------
# TestHonestDegradation - a value that reaches the caller must be a measurement
#
# Regression cover for the single try/except that used to span fetch + compute +
# intervention generation + map render in _run_assessment: ANY exception rebound
# `metrics` to an all-zeros literal and returned a complete-looking HTTP 200.
# ---------------------------------------------------------------------------

_FAKE_POLY = gpd.GeoDataFrame(geometry=[box(-83.1, 39.9, -83.0, 40.0)], crs="EPSG:4326")
_FAKE_LINE = gpd.GeoDataFrame(
    geometry=[LineString([(-83.05, 39.95), (-83.04, 39.95)])], crs="EPSG:4326"
)

_BBOX_BODY = {"state_fips": "39", "county_fips": "049", "bbox": [-83.1, 39.9, -83.0, 40.0]}


def _patched_layers(**overrides):
    """ExitStack patching every /assess fetcher; overrides take a side_effect fn."""
    from contextlib import ExitStack

    empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")
    defaults = {
        "fetch_buildings": _FAKE_POLY.to_crs("EPSG:5070"),
        "fetch_greenspace": _FAKE_POLY.to_crs("EPSG:5070"),
        "fetch_road_network": _FAKE_LINE.to_crs("EPSG:5070"),
        "fetch_parking": empty,
        "fetch_sidewalks": empty,
        "fetch_transit_stops": empty,
        "fetch_facilities": empty,
    }
    stack = ExitStack()
    for name, value in defaults.items():
        if name in overrides:
            stack.enter_context(patch("backend.app." + name, side_effect=overrides[name]))
        else:
            stack.enter_context(patch("backend.app." + name, return_value=value))
    return stack


class TestHonestDegradation:
    def test_map_render_failure_keeps_real_metrics(self, client):
        """A folium/MemoryError blowup must lose the map, not the measurements."""
        with _patched_layers():
            baseline = client.post("/assess", json=_BBOX_BODY).json()
        assert baseline["metrics"]["building_coverage_ratio"] > 0.0

        with _patched_layers(), patch(
            "backend.app.render_assessment_map", side_effect=MemoryError("map too big")
        ):
            resp = client.post("/assess", json=_BBOX_BODY)

        assert resp.status_code == 200
        data = resp.json()
        # The numbers survive untouched - this is the whole point.
        assert data["metrics"] == baseline["metrics"]
        assert data["metrics"]["building_coverage_ratio"] > 0.0
        # ...and the loss of the map is stated rather than hidden.
        assert data["map_html"] == ""
        assert "MemoryError" in data["map_note"]

    def test_compute_failure_502s_instead_of_returning_zeros(self, client):
        """No usable numbers exist, so there is nothing honest to return with a 200."""
        from shapely.errors import GEOSException

        with _patched_layers(), patch(
            "backend.app.compute_building_coverage",
            side_effect=GEOSException("TopologyException: side location conflict"),
        ):
            resp = client.post("/assess", json=_BBOX_BODY)

        assert resp.status_code == 502
        assert "No metrics were produced" in resp.json()["detail"]

    def test_fetch_failure_nulls_the_metric_and_names_it(self, client):
        """An unreachable Overpass must not publish 'this county has no parks'."""
        from backend.assess import netdiag

        def failing_greenspace(bbox):
            netdiag.record_fetch_error(
                "fetch_greenspace", OSError("[Errno 11001] getaddrinfo failed")
            )
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")

        with _patched_layers(fetch_greenspace=failing_greenspace):
            resp = client.post("/assess", json=_BBOX_BODY)

        assert resp.status_code == 200
        data = resp.json()
        assert data["metrics"]["greenspace_coverage_ratio"] is None
        assert data["metrics"]["greenspace_area_m2"] is None
        # An unaffected layer is still a real measurement.
        assert data["metrics"]["building_coverage_ratio"] > 0.0
        assert "DNS lookup failed" in data["fetch_errors"]["greenspace"]
        assert set(data["unmeasured_metrics"]) == {
            "greenspace_coverage_ratio", "greenspace_area_m2"
        }
        # No APA/EPA finding may be manufactured from a failed fetch.
        assert not [i for i in data["interventions"]
                    if i["category"] in ("Greenspace", "Park Access")]
        assert "not measured" in data["map_html"]

    def test_healthy_run_carries_no_degradation_flags(self, client):
        with _patched_layers():
            data = client.post("/assess", json=_BBOX_BODY).json()
        assert data["fetch_errors"] == {}
        assert data["unmeasured_metrics"] == []
        assert data["map_note"] is None

    def test_report_endpoint_carries_the_same_flags(self, client):
        from backend.assess import netdiag

        def failing_transit(bbox):
            netdiag.record_fetch_error("fetch_transit_stops", TimeoutError("timed out"))
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")

        with _patched_layers(fetch_transit_stops=failing_transit):
            data = client.post("/report", json=_BBOX_BODY).json()

        assert data["metrics"]["transit_stop_count"] is None
        assert "transit_stops" in data["fetch_errors"]
        # The FTA "limited public transit access" finding must not appear.
        assert not [t for t in data["top_issues"] if "transit" in t.lower()]

    def test_compare_never_diffs_against_an_unmeasured_side(self, client):
        from backend.assess import netdiag

        calls = {"n": 0}

        def greenspace_failing_once(bbox):
            calls["n"] += 1
            if calls["n"] == 1:
                netdiag.record_fetch_error(
                    "fetch_greenspace", OSError("[Errno 11001] getaddrinfo failed")
                )
                return gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")
            return _FAKE_POLY.to_crs("EPSG:5070")

        with _patched_layers(fetch_greenspace=greenspace_failing_once):
            resp = client.post("/compare", json={"area_a": _BBOX_BODY, "area_b": _BBOX_BODY})

        assert resp.status_code == 200
        data = resp.json()
        # Subtracting an unmeasured side would have invented the whole delta.
        assert "greenspace_coverage_ratio" not in data["diff"]
        assert "greenspace_coverage_ratio" in data["undiffable_metrics"]
        assert "building_coverage_ratio" in data["diff"]
        # Exactly one of the two areas is flagged - the scoped error registry
        # must not let one run steal (or share) the other run's failure.
        flagged = [a for a in ("area_a", "area_b") if data[a]["fetch_errors"]]
        assert len(flagged) == 1


class TestFarHonestyFlag:
    """compute_far assumes 1 storey per untagged building; the METRICS registry
    has always promised a levels_tagged_pct flag disclosing how much of FAR that
    assumption covers. It was never implemented."""

    def test_tagged_pct_reported(self):
        from backend.assess.metrics import compute_far_detail

        buildings = gpd.GeoDataFrame(
            {"building:levels": ["2", None, "bogus", None]},
            geometry=[box(0, 0, 100, 100), box(200, 0, 300, 100),
                      box(400, 0, 500, 100), box(600, 0, 700, 100)],
            crs="EPSG:5070",
        )
        out = compute_far_detail(buildings, STUDY)
        assert out["levels_tagged_pct"] == 25.0  # 1 of 4 parseable

    def test_no_levels_column_is_zero_pct_not_silence(self):
        from backend.assess.metrics import compute_far_detail

        out = compute_far_detail(_sq(0, 0, 500, 500), STUDY)
        assert out["far"] == pytest.approx(0.25)
        assert out["levels_tagged_pct"] == 0.0

    def test_no_buildings_leaves_tagged_pct_undefined(self):
        from backend.assess.metrics import compute_far_detail

        out = compute_far_detail(_empty_gdf(), STUDY)
        assert out["far"] == 0.0
        # Not 0.0 - there was nothing to tag, so reporting "0% tagged" would
        # itself be a fabricated coverage figure.
        assert out["levels_tagged_pct"] is None

    def test_assess_publishes_the_flag(self, client):
        with _patched_layers():
            data = client.post("/assess", json=_BBOX_BODY).json()
        assert "levels_tagged_pct" in data["metrics"]


class TestRendererMarksUnmeasured:
    def test_none_metric_renders_as_not_measured(self):
        from backend.assess.renderer import render_assessment_map

        html = render_assessment_map(
            STUDY, _empty_gdf(), _empty_gdf(), _empty_gdf(),
            {"greenspace_coverage_ratio": None, "building_coverage_ratio": 0.25},
        )
        # folium escapes the panel into an iframe srcdoc, so match the escaped form.
        assert "Greenspace Coverage: &lt;b&gt;not measured&lt;/b&gt;" in html
        assert "Building Coverage: &lt;b&gt;25.0%&lt;/b&gt;" in html
        # The old code printed a confident 0.0% here for a layer nobody fetched.
        assert "Greenspace Coverage: &lt;b&gt;0.0%&lt;/b&gt;" not in html
