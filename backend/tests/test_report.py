"""Tests for the /report endpoint."""
from __future__ import annotations

from unittest.mock import patch

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import LineString, box


@pytest.fixture()
def client():
    from backend.app import app
    return TestClient(app, headers={"X-Requested-With": "geoai"})


def _mock_layers():
    fake_poly = gpd.GeoDataFrame(geometry=[box(-83.1, 39.9, -83.0, 40.0)], crs="EPSG:4326").to_crs("EPSG:5070")
    fake_line = gpd.GeoDataFrame(geometry=[LineString([(-83.05, 39.95), (-83.04, 39.95)])], crs="EPSG:4326").to_crs("EPSG:5070")
    fake_empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")
    return fake_poly, fake_line, fake_empty


class TestReportEndpoint:
    def test_report_200_with_mocked_osm(self, client):
        fake_poly, fake_line, fake_empty = _mock_layers()

        with patch("backend.app.fetch_buildings", return_value=fake_poly), \
             patch("backend.app.fetch_greenspace", return_value=fake_poly), \
             patch("backend.app.fetch_road_network", return_value=fake_line), \
             patch("backend.app.fetch_parking", return_value=fake_empty), \
             patch("backend.app.fetch_sidewalks", return_value=fake_empty), \
             patch("backend.app.fetch_transit_stops", return_value=fake_empty), \
             patch("backend.app.fetch_facilities", return_value=fake_empty):
            resp = client.post("/report", json={
                "state_fips": "39",
                "county_fips": "049",
                "bbox": [-83.1, 39.9, -83.0, 40.0],
            })

        assert resp.status_code == 200
        data = resp.json()
        assert "area" in data
        assert "generated_at" in data
        assert "metrics" in data
        assert "intervention_count" in data
        assert "high_severity_count" in data
        assert "medium_severity_count" in data
        assert "low_severity_count" in data
        assert "top_issues" in data
        assert "map_html" in data
        assert isinstance(data["top_issues"], list)
        assert len(data["map_html"]) > 500

    def test_report_returns_severity_counts(self, client):
        *_, fake_empty = _mock_layers()

        with patch("backend.app.fetch_buildings", return_value=fake_empty), \
             patch("backend.app.fetch_greenspace", return_value=fake_empty), \
             patch("backend.app.fetch_road_network", return_value=fake_empty), \
             patch("backend.app.fetch_parking", return_value=fake_empty), \
             patch("backend.app.fetch_sidewalks", return_value=fake_empty), \
             patch("backend.app.fetch_transit_stops", return_value=fake_empty), \
             patch("backend.app.fetch_facilities", return_value=fake_empty):
            resp = client.post("/report", json={
                "state_fips": "39",
                "county_fips": "049",
                "bbox": [-83.1, 39.9, -83.0, 40.0],
            })

        assert resp.status_code == 200
        data = resp.json()
        total = data["high_severity_count"] + data["medium_severity_count"] + data["low_severity_count"]
        assert total == data["intervention_count"]
        assert len(data["top_issues"]) == data["high_severity_count"]
