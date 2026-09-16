"""2026-09-16 feedback, items 1, 3 and 6: project areas smaller than a county,
a base area for comparison, and community measures beyond the built
environment. No network — Census, CDC, EPA and the geocoder are stubbed.
"""
from __future__ import annotations

import json

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import box

from backend import hcas
from backend.assess import indicators as ind
from backend.gis import areas
from backend.tests.test_hcas import (  # shared canned fixtures
    _CANNED_USAGE,
    _CANNED_STEP4,
    _fake_gdf,
    _mock_llm_and_fetchers,  # noqa: F401 — autouse fixture, imported for reuse
    _walk,
)

HDRS = {"X-Requested-With": "geoai"}
FRANKLIN = ("39", "049")

# Four block groups in two tracts, laid out west to east near Columbus.
BG = [("390490001001", -83.00, 39.95), ("390490001002", -82.99, 39.95),
      ("390490002001", -82.98, 39.95), ("390490002002", -82.97, 39.95)]
BG_POP = {"390490001001": 1000, "390490001002": 3000, "390490002001": 500, "390490002002": 1500}
TRACT_POP = {"39049000100": 4000, "39049000200": 2000}


def _bgs():
    return gpd.GeoDataFrame(
        {"GEOID": [g for g, _, _ in BG], "NAME": [f"Block Group {g[-1]}, Tract {g[5:11]}" for g, _, _ in BG]},
        geometry=[box(x, y, x + 0.01, y + 0.01) for _, x, y in BG], crs="EPSG:4326")


def _tracts():
    return gpd.GeoDataFrame(
        {"GEOID": ["39049000100", "39049000200"], "NAME": ["Census Tract 1", "Census Tract 2"]},
        geometry=[box(-83.00, 39.95, -82.98, 39.96), box(-82.98, 39.95, -82.96, 39.96)], crs="EPSG:4326")


@pytest.fixture(autouse=True)
def _census_units(monkeypatch, _mock_llm_and_fetchers):  # after the shared stubs
    from backend.gis import profile as gis_profile

    def units(s, c, unit):
        return {"block_group": _bgs(), "custom": _bgs(), "tract": _tracts()}.get(unit)

    def pops(s, c, unit, geoids=None):
        return {"block_group": BG_POP, "custom": BG_POP, "tract": TRACT_POP}.get(unit, {})

    monkeypatch.setattr(gis_profile, "fetch_areal_unit_geometries", units)
    monkeypatch.setattr(areas, "unit_populations", pops)
    monkeypatch.setattr(hcas, "_units_cache", {})


def _llm_with(step2):
    def llm(system, prompt, schema, model="m"):
        if "framework" in schema["properties"]:
            return _CANNED_STEP4, _CANNED_USAGE
        return step2, _CANNED_USAGE
    return llm


class TestAreaDefinition:
    def test_customized_needs_block_groups(self, tmp_path):
        with pytest.raises(ValueError, match="at least one block group"):
            hcas.create_project("t", *FRANKLIN, db=tmp_path / "h.db", areal_unit="custom")

    def test_whole_county_has_nothing_to_select(self, tmp_path):
        with pytest.raises(ValueError, match="whole-county"):
            hcas.create_project("t", *FRANKLIN, db=tmp_path / "h.db", areal_unit="county",
                                area_geoids=["390490001001"])

    def test_a_selected_area_must_lie_in_the_county(self, tmp_path):
        with pytest.raises(ValueError, match="chosen county"):
            hcas.create_project("t", *FRANKLIN, db=tmp_path / "h.db", areal_unit="block_group",
                                area_geoids=["391130001001"])

    def test_the_reporting_unit_list_offers_customized(self):
        from backend.gis.profile import AREAL_UNITS

        assert list(AREAL_UNITS)[:3] == ["county", "county_subdivision", "zcta"]
        assert AREAL_UNITS["custom"]["label"].startswith("Customized")


class TestCustomArea:
    def _project(self, tmp_path, geoids=("390490001001", "390490001002")):
        db = tmp_path / "h.db"
        pid = hcas.create_project("hood", *FRANKLIN, db=db, areal_unit="custom",
                                  area_geoids=list(geoids), area_name="Short North")
        return pid, db

    def test_step1_describes_the_selected_block_groups(self, tmp_path):
        pid, db = self._project(tmp_path)
        a = hcas.run_step(pid, 1, db=db)
        pa = a["project_area"]
        assert a["kind"] == "us_sub_county"
        assert a["place_name"].startswith("Short North, Franklin County")
        assert pa["count"] == 2 and pa["population"] == 4000
        assert pa["population_source"].startswith("2020 Census")
        assert hcas._area_path(pid, "project").exists()

    def test_steps_fetch_only_the_areas_own_box(self, tmp_path, monkeypatch):
        seen = []
        monkeypatch.setattr(hcas, "_layer_fetchers",
                            lambda: {"greenspace": lambda bbox: seen.append(bbox) or _fake_gdf()})
        pid, db = self._project(tmp_path)
        _walk(pid, db, through=2)
        draft = hcas.run_step(pid, 3, db=db)
        west, south, east, north = seen[0]
        assert west >= -83.0001 and east <= -82.9799 and south >= 39.9499 and north <= 39.9601
        assert draft["study_area"]["kind"] == "custom"
        assert draft["study_area"]["population"] == 4000
        assert "2 selected block groups" in draft["areal_unit"]["note"]

    def test_step7_breaks_the_area_down_by_its_own_block_groups(self, tmp_path):
        pid, db = self._project(tmp_path)
        _walk(pid, db, through=6)
        a7 = hcas.run_step(pid, 7, db=db)
        rows = a7["by_unit"]["rows"]
        assert sorted(r["geoid"] for r in rows) == ["390490001001", "390490001002"]
        assert a7["study_area"]["kind"] == "custom"

    def test_a_single_unit_area_has_no_breakdown(self, tmp_path):
        pid, db = self._project(tmp_path, geoids=("390490001001",))
        _walk(pid, db, through=6)
        bu = hcas.run_step(pid, 7, db=db)["by_unit"]
        assert bu["rows"] == [] and "single block group" in bu["note"]

    def test_the_report_names_the_area_not_the_county(self, tmp_path):
        pid, db = self._project(tmp_path)
        _walk(pid, db, through=8)
        md = hcas.get_project(pid, db=db)["steps"][7]["ai_draft"]["markdown"]
        assert "Short North" in md.splitlines()[0]
        assert "2 block groups selected" in md

    def test_changing_the_selection_invalidates_the_stored_area(self, tmp_path):
        pid, db = self._project(tmp_path)
        _walk(pid, db, through=2)
        out = hcas.update_project_scope(pid, area_geoids=["390490001001"], db=db)
        assert out["scope_changed"] and out["stale_steps"] == 1
        assert not hcas._area_path(pid, "project").exists()
        assert hcas.get_project(pid, db=db)["meta"]["area_geoids"] == ["390490001001"]

    def test_the_locator_map_draws_the_area(self, tmp_path):
        pid, db = self._project(tmp_path)
        hcas.run_step(pid, 1, db=db)
        html = hcas.basemap_for_project(pid, db=db)
        assert "Project area · 2 selected block groups" in html

    def test_an_unknown_block_group_fails_step1_by_name(self, tmp_path):
        db = tmp_path / "h.db"
        pid = hcas.create_project("hood", *FRANKLIN, db=db, areal_unit="custom",
                                  area_geoids=["390499999999"], area_name="x")
        with pytest.raises(ValueError, match="not in this county"):
            hcas.run_step(pid, 1, db=db)


class TestBlockGroupSuggestion:
    """#1: "the system automatically asks AI to recommend corresponding Census
    Block Group(s)"."""

    COUNTY_BOX = (-83.3, 39.8, -82.7, 40.2)

    def _llm(self, bbox):
        return lambda system, prompt, schema: ({
            "place": "Short North", "geocode_query": "Short North, Columbus, Ohio",
            "bbox": bbox, "interpretation": "arts district north of downtown", "caveat": ""},
            _CANNED_USAGE)

    def test_an_osm_boundary_wins_over_the_ai_estimate(self):
        polygon = box(-83.001, 39.949, -82.982, 39.961).__geo_interface__
        out = areas.suggest_block_groups(
            "Short North", "Franklin County, Ohio", self.COUNTY_BOX, _bgs(),
            llm=self._llm({"west": -82.975, "south": 39.9, "east": -82.96, "north": 40.0}),
            geocode=lambda q, vb: [{"geojson": polygon}])
        assert out["geoids"] == ["390490001001", "390490001002"]
        assert out["method"] == "osm_boundary"

    def test_the_ai_extent_is_used_when_osm_has_only_a_point(self):
        out = areas.suggest_block_groups(
            "Short North", "Franklin County, Ohio", self.COUNTY_BOX, _bgs(),
            llm=self._llm({"west": -82.981, "south": 39.94, "east": -82.959, "north": 39.97}),
            geocode=lambda q, vb: [{"geojson": {"type": "Point", "coordinates": [-83, 39.95]}}])
        assert out["geoids"] == ["390490002001", "390490002002"]
        assert out["method"] == "ai_extent"

    def test_a_community_smaller_than_one_block_group_gets_the_one_it_is_in(self):
        out = areas.suggest_block_groups(
            "tiny", "Franklin County, Ohio", self.COUNTY_BOX, _bgs(),
            llm=self._llm({"west": -82.9899, "south": 39.9549, "east": -82.9891, "north": 39.9551}),
            geocode=lambda q, vb: [])
        assert out["geoids"] == ["390490001002"]

    def test_nothing_is_invented_when_the_place_cannot_be_found(self):
        out = areas.suggest_block_groups(
            "Atlantis", "Franklin County, Ohio", self.COUNTY_BOX, _bgs(),
            llm=self._llm({"west": 10, "south": 10, "east": 11, "north": 11}),
            geocode=lambda q, vb: [])
        assert out["geoids"] == [] and "select them on the map" in out["caveat"]

    def test_a_county_sized_polygon_is_not_the_community(self):
        huge = box(*self.COUNTY_BOX).__geo_interface__
        out = areas.suggest_block_groups(
            "Columbus", "Franklin County, Ohio", self.COUNTY_BOX, _bgs(),
            llm=self._llm({"west": -82.981, "south": 39.94, "east": -82.959, "north": 39.97}),
            geocode=lambda q, vb: [{"geojson": huge}])
        assert out["method"] == "ai_extent"

    def test_the_endpoint_returns_population_and_area(self, tmp_path, monkeypatch):
        monkeypatch.setattr(areas, "nominatim_search", lambda q, vb: [])
        monkeypatch.setattr(hcas, "_llm_structured", lambda system, prompt, schema, model="m": ({
            "place": "Short North", "geocode_query": "x",
            "bbox": {"west": -83.0005, "south": 39.94, "east": -82.9795, "north": 39.97},
            "interpretation": "", "caveat": ""}, _CANNED_USAGE))
        monkeypatch.setattr(areas, "suggest_block_groups",
                            lambda *a, geocode=None, **k: _orig_suggest(*a, geocode=lambda q, vb: [], **k))
        from backend.app import app

        r = TestClient(app).post("/hcas/suggest-area", json={"text": "Short North",
                                 "state_fips": "39", "county_fips": "049"}, headers=HDRS)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["geoids"] == ["390490001001", "390490001002"]
        assert body["population"] == 4000


_orig_suggest = areas.suggest_block_groups


class TestUnitsForThePicker:
    def test_units_geojson_carries_name_population_and_area(self):
        fc = hcas.units_geojson("39", "049", "block_group")
        props = fc["features"][0]["properties"]
        assert set(props) >= {"GEOID", "name", "population", "area_km2"}
        assert props["population"] == 1000

    def test_resolve_area_reports_the_scale(self, monkeypatch):
        monkeypatch.setattr(hcas, "_llm_structured", lambda s, p, sc, model="m": ({
            "county": "Franklin", "state": "Ohio", "interpretation": "", "caveat": "",
            "scale": "zip", "zip": "43215", "town": ""}, _CANNED_USAGE))
        out = hcas.resolve_area("43215")
        assert out["scale"] == "zip" and out["zip"] == "43215"


# --- community measures (#6) ----------------------------------------------------

SVI = {"39049000100": {"pop": 4000, "EP_POV150": 10.0, "EP_UNEMP": 4.0, "EP_NOINT": 5.0},
       "39049000200": {"pop": 2000, "EP_POV150": 40.0, "EP_UNEMP": None, "EP_NOINT": 20.0}}
CHR = {"_year": 2025, "39049": {"v125_rawvalue": 8.2, "v004_rawvalue": 0.0010155788},
       "00000": {"v125_rawvalue": 7.3, "v004_rawvalue": 0.0007494266}}
AQI = {"_year": 2025, "Ohio|Hamilton": {"Median AQI": 50.0, "pct_unhealthy_days": 1.0}}


class TestAggregation:
    def test_population_weighted(self):
        v, cov = ind.aggregate(SVI, "EP_POV150", {"39049000100": 1.0, "39049000200": 1.0})
        assert v == pytest.approx(20.0) and cov == 1.0

    def test_partial_tracts_weight_by_the_residents_inside(self):
        v, _ = ind.aggregate(SVI, "EP_POV150", {"39049000100": 0.25, "39049000200": 1.0})
        assert v == pytest.approx((1000 * 10 + 2000 * 40) / 3000)

    def test_too_little_coverage_is_not_a_figure(self):
        v, cov = ind.aggregate(SVI, "EP_UNEMP", {"39049000100": 0.2, "39049000200": 1.0})
        assert v is None and cov < 0.5

    def test_block_group_shares_are_population_exact(self):
        shares = hcas._tract_shares({"state_fips": "39", "county_fips": "049", "areal_unit": "custom",
                                     "area_geoids": ["390490001002", "390490002001"]})
        assert shares == {"39049000100": 0.75, "39049000200": 0.25}


class TestCommunityMeasuresEndToEnd:
    STEP2 = {"factors": [
        {"factor": "Poverty", "metric": "poverty_rate", "rationale": "r", "data_needed": ""},
        {"factor": "Air pollution", "metric": "air_pollution_pm25", "rationale": "r", "data_needed": ""},
        {"factor": "Doctors", "metric": "primary_care_physicians", "rationale": "r", "data_needed": ""},
        {"factor": "Unhealthy air", "metric": "unhealthy_air_days", "rationale": "r", "data_needed": ""},
        {"factor": "Traffic exposure", "metric": "near_major_road_exposure", "rationale": "r", "data_needed": ""},
        {"factor": "Noise", "metric": "needs_data", "rationale": "loud",
         "data_needed": "measured noise levels, e.g. a city noise monitoring network"},
    ], "notes": ""}

    @pytest.fixture(autouse=True)
    def _tables(self, monkeypatch):
        tables = {"svi": SVI, "chr": CHR, "aqi": AQI}
        monkeypatch.setattr(hcas, "_fetch_table", lambda src, spec: tables[src])
        monkeypatch.setattr(hcas, "_llm_structured", _llm_with(self.STEP2))

    def test_measured_from_public_tables_with_their_level_and_source(self, tmp_path):
        db = tmp_path / "h.db"
        pid = hcas.create_project("t", *FRANKLIN, db=db)
        _walk(pid, db, through=6)
        a7 = hcas.run_step(pid, 7, db=db)
        m, d = a7["metrics"], a7["measure_details"]
        assert m["poverty_rate"] == pytest.approx(20.0)
        assert m["air_pollution_pm25"] == 8.2 and d["air_pollution_pm25"]["us"] == 7.3
        assert m["primary_care_physicians"] == pytest.approx(101.5579, rel=1e-3)
        assert d["air_pollution_pm25"]["level"] == "county-wide"
        # Franklin has no EPA monitor in the stub: not measured, and says why.
        assert m["unhealthy_air_days"] is None
        assert "no EPA air-quality monitor" in d["unhealthy_air_days"]["not_measured"]
        assert "unhealthy_air_days" in a7["unmeasured_metrics"]
        assert a7["awaiting_data"][0]["factor"] == "Noise"

    def test_step3_lists_the_tables_and_the_factor_awaiting_data(self, tmp_path):
        db = tmp_path / "h.db"
        pid = hcas.create_project("t", *FRANKLIN, db=db)
        _walk(pid, db, through=2)
        a3 = hcas.run_step(pid, 3, db=db)
        assert {t["source"] for t in a3["tables"]} == {"svi", "chr", "aqi"}
        aqi = next(t for t in a3["tables"] if t["source"] == "aqi")
        assert "no EPA air monitor" in aqi["note"]
        assert a3["awaiting_data"][0]["data_needed"].startswith("measured noise")

    def test_the_report_keeps_the_unmeasured_factor(self, tmp_path):
        db = tmp_path / "h.db"
        pid = hcas.create_project("t", *FRANKLIN, db=db)
        _walk(pid, db, through=8)
        md = hcas.get_project(pid, db=db)["steps"][7]["ai_draft"]["markdown"]
        assert "### Factors awaiting data" in md and "**Noise**" in md
        assert "`poverty_rate`: 20" in md
        assert "`unhealthy_air_days`: not measured — no EPA air-quality monitor" in md

    def test_a_failed_table_is_not_measured_not_zero(self, tmp_path, monkeypatch):
        def fail(src, spec):
            raise ConnectionError("unreachable host")
        monkeypatch.setattr(hcas, "_fetch_table", fail)
        db = tmp_path / "h.db"
        pid = hcas.create_project("t", *FRANKLIN, db=db)
        _walk(pid, db, through=6)
        a7 = hcas.run_step(pid, 7, db=db)
        assert a7["metrics"]["poverty_rate"] is None
        assert set(a7["table_errors"]) == {"svi", "chr", "aqi"}


class TestNoRefusalAndPlainWords:
    def test_the_prompts_offer_pollution_and_socioeconomic_measures(self):
        for text in (hcas._STEP2_SYSTEM, hcas._REVISE_SYSTEM):
            assert "air_pollution_pm25" in text and "poverty_rate" in text
            assert "STILL INCLUDE IT" in text
            assert "METRIC REGISTRY" not in text

    def test_revise_no_longer_says_add_nothing(self):
        assert "add nothing" not in hcas._REVISE_SYSTEM

    def test_needs_data_is_a_valid_choice(self):
        item = hcas._STEP2_SCHEMA["properties"]["factors"]["items"]
        assert "needs_data" in item["properties"]["metric"]["enum"]
        assert "data_needed" in item["required"]

    def test_every_measure_has_a_label_and_a_topic(self):
        cat = hcas.measure_catalog()
        assert all(v["label"] and "_" not in v["label"] for v in cat.values())
        assert cat["poverty_rate"]["source"] == "CDC Social Vulnerability Index"


class TestExposureProxies:
    def _sa(self):
        return gpd.GeoDataFrame(geometry=[box(0, 0, 1000, 1000)], crs="EPSG:5070")

    def test_near_major_road(self):
        from backend.assess import metrics as mm
        from shapely.geometry import LineString

        buildings = gpd.GeoDataFrame(geometry=[box(10, 10, 20, 20), box(900, 900, 910, 910)], crs="EPSG:5070")
        roads = gpd.GeoDataFrame({"highway": ["primary", "residential"]},
                                 geometry=[LineString([(0, 0), (1000, 0)]), LineString([(0, 950), (1000, 950)])],
                                 crs="EPSG:5070")
        out = mm.compute_near_major_road_exposure(buildings, roads, self._sa())
        assert out["near_major_road_exposure"] == 50.0

    def test_no_buildings_is_not_zero_exposure(self):
        from backend.assess import metrics as mm

        empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")
        assert mm.compute_near_major_road_exposure(empty, empty, self._sa())["near_major_road_exposure"] is None

    def test_list_valued_highway_tags_count(self):
        from backend.assess import metrics as mm

        assert mm._is_major(["secondary", "primary"]) and mm._is_major("['trunk', 'service']")
        assert not mm._is_major("residential")

    def test_industrial_share_counts_overlaps_once(self):
        from backend.assess import metrics as mm

        lu = gpd.GeoDataFrame({"landuse": ["industrial", "quarry", "residential"]},
                              geometry=[box(0, 0, 100, 100), box(50, 50, 100, 100), box(500, 500, 600, 600)],
                              crs="EPSG:5070")
        assert mm.compute_industrial_land_share(lu, self._sa())["industrial_land_share"] == 1.0


# --- base area (#3) --------------------------------------------------------------

class TestBaseArea:
    BASE = {"state_fips": "39", "county_fips": "049", "areal_unit": "block_group",
            "area_geoids": ["390490002001", "390490002002"]}

    def _project(self, tmp_path):
        db = tmp_path / "h.db"
        pid = hcas.create_project("cmp", *FRANKLIN, db=db, areal_unit="custom",
                                  area_geoids=["390490001001", "390490001002"], area_name="West",
                                  base=self.BASE)
        return pid, db

    def test_every_data_step_runs_for_both_areas(self, tmp_path):
        pid, db = self._project(tmp_path)
        _walk(pid, db, through=8)
        steps = {s["step"]: s["ai_draft"] for s in hcas.get_project(pid, db=db)["steps"]}
        assert steps[1]["base"]["project_area"]["population"] == 2000
        assert steps[3]["base"]["study_area"]["kind"] == "units"
        assert steps[5]["base"]["kind"] == "gis_database"
        assert any(s["action"] == "compare_with_base" for s in steps[6]["steps"])
        assert steps[7]["base"]["study_area"]["kind"] == "units"
        assert "greenspace_coverage_ratio" in steps[7]["comparison"]
        assert hcas.get_map(pid, db=db, which="base")
        assert "## Comparison with the base area" in steps[8]["markdown"]

    def test_removing_the_base_area_marks_the_work_stale(self, tmp_path):
        pid, db = self._project(tmp_path)
        _walk(pid, db, through=2)
        out = hcas.update_project_scope(pid, base=None, db=db)
        assert out["scope_changed"]
        assert "base" not in hcas.get_project(pid, db=db)["meta"]

    def test_leaving_base_out_of_an_update_keeps_it(self, tmp_path):
        pid, db = self._project(tmp_path)
        hcas.update_project_scope(pid, title="renamed", db=db)
        assert hcas.get_project(pid, db=db)["meta"]["base"]["area_geoids"] == self.BASE["area_geoids"]

    def test_the_api_accepts_and_clears_a_base_area(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hcas, "DB_PATH", tmp_path / "h.db")
        from backend.app import app

        client = TestClient(app)
        r = client.post("/hcas/projects", json={"title": "t", "state_fips": "39", "county_fips": "049",
                                                "base": self.BASE}, headers=HDRS)
        assert r.status_code == 200, r.text
        pid = r.json()["project_id"]
        assert client.get(f"/hcas/projects/{pid}", headers=HDRS).json()["meta"]["base"]["county_fips"] == "049"
        r = client.patch(f"/hcas/projects/{pid}", json={"base": None}, headers=HDRS)
        assert r.status_code == 200
        assert "base" not in client.get(f"/hcas/projects/{pid}", headers=HDRS).json()["meta"]
        r = client.patch(f"/hcas/projects/{pid}", json={"title": "x"}, headers=HDRS)
        assert r.status_code == 200
