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
        county_gdf = gpd.GeoDataFrame(
            {"GEOID": ["39049"], "NAME": ["Franklin County"]},
            geometry=[box(-83.2, 39.8, -82.8, 40.2)],
            crs="EPSG:4326",
        )

        with patch("backend.gis.collector_lite.collect_county_boundaries", return_value=county_gdf), \
             patch("backend.app.fetch_buildings", return_value=fake_poly_gdf.to_crs("EPSG:5070")), \
             patch("backend.app.fetch_greenspace", return_value=fake_poly_gdf.to_crs("EPSG:5070")), \
             patch("backend.app.fetch_road_network", return_value=fake_line_gdf.to_crs("EPSG:5070")):
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
        assert "interventions" in data
        assert isinstance(data["interventions"], list)
        assert len(data["map_html"]) > 500

    def test_assess_200_with_custom_bbox(self, client):
        fake_gdf = gpd.GeoDataFrame(geometry=[box(-83.1, 39.9, -83.0, 40.0)], crs="EPSG:4326")
        fake_line_gdf = gpd.GeoDataFrame(geometry=[LineString([(-83.05, 39.95), (-83.04, 39.95)])], crs="EPSG:4326")

        with patch("backend.app.fetch_buildings", return_value=fake_gdf.to_crs("EPSG:5070")), \
             patch("backend.app.fetch_greenspace", return_value=fake_gdf.to_crs("EPSG:5070")), \
             patch("backend.app.fetch_road_network", return_value=fake_line_gdf.to_crs("EPSG:5070")):
            resp = client.post("/assess", json={
                "state_fips": "39",
                "county_fips": "049",
                "bbox": [-83.1, 39.9, -83.0, 40.0],
            })

        assert resp.status_code == 200
        data = resp.json()
        assert "map_html" in data
        assert data["metrics"]["building_coverage_ratio"] >= 0.0
