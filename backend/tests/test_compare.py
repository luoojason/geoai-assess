"""Tests for the /compare endpoint."""
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


def _bbox_payload(west, south, east, north, state="39", county="049"):
    return {"state_fips": state, "county_fips": county, "bbox": [west, south, east, north]}


class TestCompareEndpoint:
    def test_compare_200_with_two_areas(self, client):
        fake_poly = gpd.GeoDataFrame(geometry=[box(-83.1, 39.9, -83.0, 40.0)], crs="EPSG:4326").to_crs("EPSG:5070")
        fake_line = gpd.GeoDataFrame(geometry=[LineString([(-83.05, 39.95), (-83.04, 39.95)])], crs="EPSG:4326").to_crs("EPSG:5070")
        fake_empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")

        with patch("backend.app.fetch_buildings", return_value=fake_poly), \
             patch("backend.app.fetch_greenspace", return_value=fake_poly), \
             patch("backend.app.fetch_road_network", return_value=fake_line), \
             patch("backend.app.fetch_parking", return_value=fake_empty), \
             patch("backend.app.fetch_sidewalks", return_value=fake_empty), \
             patch("backend.app.fetch_transit_stops", return_value=fake_empty), \
             patch("backend.app.fetch_facilities", return_value=fake_empty):
            resp = client.post("/compare", json={
                "area_a": _bbox_payload(-83.1, 39.9, -83.0, 40.0),
                "area_b": _bbox_payload(-83.2, 39.8, -83.1, 39.9),
            })

        assert resp.status_code == 200
        data = resp.json()
        assert "area_a" in data
        assert "area_b" in data
        assert "diff" in data
        assert "metrics" in data["area_a"]
        assert "metrics" in data["area_b"]
        assert "interventions" in data["area_a"]

    def test_compare_diff_is_b_minus_a(self, client):
        poly_a = gpd.GeoDataFrame(geometry=[box(0, 0, 500, 500)], crs="EPSG:5070")
        poly_b = gpd.GeoDataFrame(geometry=[box(0, 0, 700, 700)], crs="EPSG:5070")
        fake_empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")
        fake_line = gpd.GeoDataFrame(geometry=[LineString([(0, 0), (1000, 0)])], crs="EPSG:5070")

        call_count = {"n": 0}

        def buildings_side_effect(bbox):
            call_count["n"] += 1
            return poly_a if call_count["n"] <= 3 else poly_b

        with patch("backend.app.fetch_buildings", side_effect=buildings_side_effect), \
             patch("backend.app.fetch_greenspace", return_value=fake_empty), \
             patch("backend.app.fetch_road_network", return_value=fake_line), \
             patch("backend.app.fetch_parking", return_value=fake_empty), \
             patch("backend.app.fetch_sidewalks", return_value=fake_empty), \
             patch("backend.app.fetch_transit_stops", return_value=fake_empty), \
             patch("backend.app.fetch_facilities", return_value=fake_empty):
            resp = client.post("/compare", json={
                "area_a": _bbox_payload(-83.1, 39.9, -83.0, 40.0),
                "area_b": _bbox_payload(-83.2, 39.8, -83.1, 39.9),
            })

        assert resp.status_code == 200
        data = resp.json()
        for k, v in data["diff"].items():
            expected = (data["area_b"]["metrics"].get(k, 0) or 0) - (data["area_a"]["metrics"].get(k, 0) or 0)
            assert abs(v - expected) < 1e-9, f"diff[{k}] = {v}, expected {expected}"
