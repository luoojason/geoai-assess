"""Defects found in the 2026-08-09 full-system sweep, after the Shi feedback round.

Each class here pins a claim the application makes about itself. The theme of the
sweep was that the code was correct and the *statements about it* had gone stale
or were never made at all — a note that outlived its feature, a limitation that
stopped being true when the study area changed, a reporting unit measured over
ground that was never surveyed, and an error path that reported nothing.
"""
from __future__ import annotations

import json

import pytest

from backend import hcas
from fastapi.testclient import TestClient

from backend.tests.test_hcas import (  # shared canned fixtures
    FRANKLIN,
    _mock_llm_and_fetchers,  # noqa: F401 — autouse fixture, imported for reuse
    _project,
    _walk,
)


class TestTheAppDoesNotUnderstateItself:
    """Step 3 told the analyst that per-unit tabulation was "not implemented
    yet" for three commits after step 7 implemented it — and a test asserted the
    stale sentence, so the suite defended the false claim. Pin the promise the
    app keeps, and pin that it is kept."""

    def test_step3_promises_the_per_unit_table_step7_delivers(self, tmp_path):
        pid, db = _project(tmp_path, areal_unit="tract")
        _walk(pid, db, through=2)
        note = hcas.run_step(pid, 3, db=db)["areal_unit"]["note"]
        assert "step 7" in note
        assert "not implemented" not in note

    def test_and_step7_actually_produces_one(self, tmp_path, monkeypatch):
        """The promise above is only honest if the table arrives."""
        import geopandas as gpd
        from shapely.geometry import box

        from backend.gis import profile as gis_profile

        # The shared fixture forces the disc fallback (no network); a per-unit
        # table needs the county path, so give this test a county polygon.
        county = gpd.GeoDataFrame(
            geometry=[box(-83.2, 39.8, -82.8, 40.2)], crs="EPSG:4326").to_crs("EPSG:5070")
        monkeypatch.setattr(hcas, "_county_geometry", lambda s, c: county)
        monkeypatch.setattr(
            gis_profile, "fetch_areal_unit_geometries",
            lambda s, c, unit: gpd.GeoDataFrame(
                {"GEOID": ["39049000100"], "NAME": ["Tract 1"]},
                geometry=[box(-83.1, 39.9, -83.0, 40.0)], crs="EPSG:4326"),
        )
        pid, db = _project(tmp_path, areal_unit="tract")
        _walk(pid, db, through=6)
        by_unit = hcas.run_step(pid, 7, db=db)["by_unit"]
        assert by_unit["rows"], "step 3 promised a per-unit table; step 7 returned none"


class TestHealthOutcomesAreDescribedTruthfully:
    """CDC PLACES was described as "coarser than the study area" in four places,
    including the published report. That was written when the study area was a
    5 km disc; since 2026-07-27 it is the whole county, so the extents match and
    the sentence was simply false. The real limitation is that the estimates are
    modelled and carry no within-county variation."""

    def test_the_report_does_not_call_county_estimates_coarser(self, tmp_path):
        pid, db = _project(tmp_path)
        _walk(pid, db, through=8)
        md = hcas.get_project(pid, db=db)["steps"][7]["ai_draft"]["markdown"]
        assert "coarser than the study area" not in md

    def test_the_report_states_the_real_limitation(self, tmp_path):
        pid, db = _project(tmp_path)
        _walk(pid, db, through=8)
        md = hcas.get_project(pid, db=db)["steps"][7]["ai_draft"]["markdown"]
        assert "no variation within the county" in md

    def test_the_step4_prompt_does_not_teach_the_model_the_false_version(self):
        assert "coarser than the 5km study area" not in hcas._STEP4_SYSTEM
        assert "no variation within the county" in hcas._STEP4_SYSTEM


class TestUnitsAreMeasuredOnlyWhereWeLooked:
    """A ZCTA does not nest in a county: it is selected by overlap with the
    county envelope and can lie mostly outside it. OSM layers are fetched for the
    county bbox only, so an unclipped ZCTA puts its whole area in the denominator
    against features from just the part we surveyed — a real-looking low number
    where the honest answer is "not measured here"."""

    def _units(self):
        import geopandas as gpd
        from shapely.geometry import box

        return gpd.GeoDataFrame(
            {"GEOID": ["03301", "03302"], "NAME": ["ZIP inside", "ZIP half out"]},
            # first sits inside the study area; second is half beyond its edge
            geometry=[box(0, 0, 1000, 1000), box(1000, 0, 3000, 1000)],
            crs="EPSG:5070",
        )

    def _study_area(self):
        import geopandas as gpd
        from shapely.geometry import box

        return gpd.GeoDataFrame(geometry=[box(0, 0, 2000, 1000)], crs="EPSG:5070")

    def _green(self):
        import geopandas as gpd
        from shapely.geometry import box

        # 500,000 m2 of park inside the surveyed half of the second ZIP
        return gpd.GeoDataFrame(geometry=[box(1000, 0, 1500, 1000)], crs="EPSG:5070")

    def test_unclipped_the_ratio_is_diluted_by_unsurveyed_ground(self):
        """The defect, stated as a measurement: without clipping, the same park
        in the same place reports a materially lower coverage."""
        from backend.assess.by_unit import tabulate_by_unit

        out = tabulate_by_unit(self._units(), {"greenspace": self._green()},
                               hcas._metric_computers(), ["greenspace_coverage"])
        row = {r["name"]: r for r in out["rows"]}["ZIP half out"]
        # 500,000 m2 of park over 2,000,000 m2 of claimed area
        assert row["metrics"]["greenspace_coverage_ratio"] == pytest.approx(0.25)

    def test_clipping_reports_the_part_that_was_actually_surveyed(self):
        from backend.assess.by_unit import tabulate_by_unit

        out = tabulate_by_unit(self._units(), {"greenspace": self._green()},
                               hcas._metric_computers(), ["greenspace_coverage"],
                               clip_to=self._study_area())
        row = {r["name"]: r for r in out["rows"]}["ZIP half out"]
        # same park, now over the 1,000,000 m2 that was inside the county
        assert row["metrics"]["greenspace_coverage_ratio"] == pytest.approx(0.5)
        assert row["area_km2"] == pytest.approx(1.0)

    def test_a_unit_entirely_outside_is_dropped_not_reported_as_zero(self):
        import geopandas as gpd
        from shapely.geometry import box

        from backend.assess.by_unit import tabulate_by_unit

        far = gpd.GeoDataFrame(
            {"GEOID": ["09999"], "NAME": ["ZIP elsewhere"]},
            geometry=[box(50_000, 0, 51_000, 1000)], crs="EPSG:5070")
        out = tabulate_by_unit(far, {"greenspace": self._green()},
                               hcas._metric_computers(), ["greenspace_coverage"],
                               clip_to=self._study_area())
        assert out["rows"] == []

    def test_nesting_units_are_left_alone(self, tmp_path, monkeypatch):
        """Tracts nest inside the county, so clipping them would only shave
        slivers off boundaries that already agree. Only the non-nesting case is
        clipped, and only that case carries the disclosure."""
        import geopandas as gpd
        from shapely.geometry import box

        from backend.gis import profile as gis_profile

        monkeypatch.setattr(
            gis_profile, "fetch_areal_unit_geometries",
            lambda s, c, unit: gpd.GeoDataFrame(
                {"GEOID": ["1"], "NAME": ["U"]},
                geometry=[box(0, 0, 1000, 1000)], crs="EPSG:5070"))

        def tab(unit):
            home = tmp_path / unit
            home.mkdir()
            pid, db = _project(home, areal_unit=unit)
            return hcas._tabulate_units(
                hcas.get_project(pid, db=db), {"greenspace": self._green()},
                hcas._metric_computers(), ["greenspace_coverage"], {},
                {"kind": "county"}, study_area=self._study_area())

        assert tab("tract").get("clipped_to_study_area") is None
        zcta = tab("zcta")
        assert zcta["clipped_to_study_area"] is True
        assert "inside this county" in zcta["note"]


class TestLineAndPointMetricsRespectTheBoundary:
    """The area metrics have clipped to the study area since the coverage ratios
    were seen to exceed 1.0. The LINE and POINT metrics never got the same
    treatment: they summed the full length of every road and counted every stop
    in the fetched bounding box, then divided by the study area — a numerator
    from one region over a denominator from another.

    Per unit it is worse than approximate. Tabulation hands each unit every
    feature that touches it, at full length, so one county-long highway was
    counted whole in every tract it crossed. Measured before the fix: a 1 km²
    tract holding 1 km of road reported 3.0 km/km².
    """

    def _sa(self, x0, x1):
        import geopandas as gpd
        from shapely.geometry import box

        return gpd.GeoDataFrame(geometry=[box(x0, 0, x1, 1000)], crs="EPSG:5070")

    def _road(self):
        import geopandas as gpd
        from shapely.geometry import LineString

        # 3 km of road; only the first 2 km lie inside the 2 km² county
        return gpd.GeoDataFrame(geometry=[LineString([(0, 500), (3000, 500)])],
                                crs="EPSG:5070")

    def _points(self):
        import geopandas as gpd
        from shapely.geometry import Point

        # two inside the county, two beyond its eastern edge
        return gpd.GeoDataFrame(
            geometry=[Point(500, 500), Point(1500, 500), Point(2500, 500), Point(2600, 500)],
            crs="EPSG:5070")

    def test_road_density_counts_only_road_inside_the_area(self):
        from backend.assess import metrics as m

        # 2 km of road in 2 km² == 1.0, not the 1.5 an unclipped 3 km gives
        assert m.compute_road_density(self._road(), self._sa(0, 2000)) == pytest.approx(1.0)

    def test_road_density_per_unit_is_not_multiplied_across_units(self):
        from backend.assess import metrics as m

        for lo, hi in ((0, 1000), (1000, 2000)):
            assert m.compute_road_density(self._road(), self._sa(lo, hi)) == pytest.approx(1.0)

    def test_sidewalk_density_clips_too(self):
        from backend.assess import metrics as m

        assert m.compute_pedestrian_density(self._road(), self._sa(0, 2000)) == pytest.approx(1.0)

    def test_stops_outside_the_area_are_not_counted(self):
        from backend.assess import metrics as m

        out = m.compute_transit_access(self._points(), self._sa(0, 2000))
        assert out["stop_count"] == 2
        assert out["stop_density_per_km2"] == pytest.approx(1.0)

    def test_facilities_outside_the_area_are_not_counted(self):
        from backend.assess import metrics as m

        assert m.compute_facility_access(self._points(), self._sa(0, 2000))["facility_count"] == 2

    def test_land_use_mix_is_weighted_by_the_part_inside(self):
        """An even split inside the county, wildly uneven if the overhang counts."""
        import geopandas as gpd
        from shapely.geometry import box
        from backend.assess import metrics as m

        lu = gpd.GeoDataFrame(
            {"landuse": ["residential", "forest"]},
            # 1 km² of residential inside; forest is 1 km² inside plus 9 km² outside
            geometry=[box(0, 0, 1000, 1000), box(1000, 0, 11000, 1000)],
            crs="EPSG:5070")
        assert m.compute_land_use_mix(lu, self._sa(0, 2000))["land_use_mix"] == pytest.approx(1.0)

    def test_line_and_point_units_sum_to_the_county_figure(self):
        """The partition test the area metrics already had, extended to the
        metrics that lacked it — this is the property that was violated."""
        import geopandas as gpd
        from shapely.geometry import box

        from backend.assess.by_unit import tabulate_by_unit

        units = gpd.GeoDataFrame(
            {"GEOID": ["a", "b"], "NAME": ["T1", "T2"]},
            geometry=[box(0, 0, 1000, 1000), box(1000, 0, 2000, 1000)], crs="EPSG:5070")
        layers = {"roads": self._road(), "transit_stops": self._points()}
        computers = hcas._metric_computers()
        county = self._sa(0, 2000)

        county_stops = computers["transit_access"](layers, county)["transit_stop_count"]
        out = tabulate_by_unit(units, layers, computers,
                               ["road_density_km_per_km2", "transit_access"])
        rows = out["rows"]
        assert sum(r["metrics"]["transit_stop_count"] for r in rows) == county_stops == 2
        # each tract is 1 km2 and holds 1 km of road, so both densities are 1.0
        assert [r["metrics"]["road_density_km_per_km2"] for r in rows] == [
            pytest.approx(1.0), pytest.approx(1.0)]


class TestUrbanThresholdsAreNotPresentedAsACountyVerdict:
    """The intervention thresholds are urban-neighborhood standards (APA, EPA
    Smart Growth, FTA, ULI) written when the study area was a 5 km disc. The
    study area has been an entire county since 2026-07-27, so a rural county
    fails several of them by construction — its transit density is low because
    most of it is farmland. The findings are unchanged; what was missing was
    saying what they can and cannot support."""

    def _county_run(self, tmp_path, monkeypatch):
        import geopandas as gpd
        from shapely.geometry import box

        county = gpd.GeoDataFrame(
            geometry=[box(-83.2, 39.8, -82.8, 40.2)], crs="EPSG:4326").to_crs("EPSG:5070")
        monkeypatch.setattr(hcas, "_county_geometry", lambda s, c: county)
        pid, db = _project(tmp_path)
        _walk(pid, db, through=8)
        return hcas.get_project(pid, db=db)["steps"][7]["ai_draft"]["markdown"]

    def test_a_county_report_says_the_thresholds_are_urban(self, tmp_path, monkeypatch):
        md = self._county_run(tmp_path, monkeypatch)
        assert "urban-neighborhood planning standards" in md
        assert "not as a county-wide verdict" in md

    def test_the_disc_fallback_does_not_claim_it(self, tmp_path):
        """On the 5 km disc the thresholds are being used as written, so the
        caveat would be wrong."""
        pid, db = _project(tmp_path)
        _walk(pid, db, through=8)
        md = hcas.get_project(pid, db=db)["steps"][7]["ai_draft"]["markdown"]
        assert "county-wide verdict" not in md


class TestReportNumbersAreReadable:
    """The per-unit table formatted with `:g`, which switches to scientific
    notation above six digits — so a real tract's greenspace printed as
    "2.28373e+07 m²" in a document a planner is meant to skim."""

    def test_large_values_are_not_scientific_notation(self):
        out = hcas._fmt_report_num(22837312.583446)
        assert "e+" not in out
        assert out == "22,837,313"

    def test_ratios_keep_their_precision(self):
        assert hcas._fmt_report_num(0.057603) == "0.057603"

    def test_mid_range_values_are_trimmed_not_padded(self):
        assert hcas._fmt_report_num(396.4628) == "396.46"
        assert hcas._fmt_report_num(96.0) == "96"

    def test_the_rendered_table_carries_no_exponent(self, tmp_path, monkeypatch):
        import geopandas as gpd
        from shapely.geometry import box

        from backend.gis import profile as gis_profile

        county = gpd.GeoDataFrame(
            geometry=[box(-83.2, 39.8, -82.8, 40.2)], crs="EPSG:4326").to_crs("EPSG:5070")
        monkeypatch.setattr(hcas, "_county_geometry", lambda s, c: county)
        monkeypatch.setattr(
            gis_profile, "fetch_areal_unit_geometries",
            lambda s, c, unit: gpd.GeoDataFrame(
                {"GEOID": ["39049000100"], "NAME": ["Tract 1"]},
                geometry=[box(-83.1, 39.9, -83.0, 40.0)], crs="EPSG:4326"))
        pid, db = _project(tmp_path, areal_unit="tract")
        _walk(pid, db, through=8)
        md = hcas.get_project(pid, db=db)["steps"][7]["ai_draft"]["markdown"]
        table = md.split("## Results by")[1].split("##")[0]
        assert "e+0" not in table


class TestTheMapShowsTheGroundTheMetricsDescribe:
    """Reported from a live Adams County run: green polygons drawn OUTSIDE the
    county outline, two control panels overlapping in the top-right corner, and
    no way to tell why the map held nothing but greenspace."""

    def _county(self):
        import geopandas as gpd
        from shapely.geometry import box

        return gpd.GeoDataFrame(geometry=[box(0, 0, 2000, 2000)], crs="EPSG:5070")

    def _green(self):
        import geopandas as gpd
        from shapely.geometry import box

        # inside · straddling the eastern edge · wholly outside (a bbox corner)
        return gpd.GeoDataFrame(
            geometry=[box(100, 100, 300, 300), box(1900, 100, 2400, 300),
                      box(3000, 3000, 3200, 3200)], crs="EPSG:5070")

    def _empty(self):
        import geopandas as gpd

        return gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")

    def test_features_outside_the_boundary_are_not_drawn(self):
        from backend.assess.renderer import _clip_to_study_area

        out = _clip_to_study_area(self._green(), self._county())
        assert len(out) == 2, "the feature wholly outside should be dropped"
        assert sorted(round(a) for a in out.geometry.area) == [20_000, 40_000]

    def test_the_drawn_area_equals_what_the_metric_counted(self):
        """The map and the number must not disagree — the whole point."""
        from backend.assess import metrics as m
        from backend.assess.renderer import _clip_to_study_area

        county, green = self._county(), self._green()
        drawn = _clip_to_study_area(green, county).geometry.area.sum()
        measured = m.compute_greenspace_coverage(green, county)["area_m2"]
        assert drawn == pytest.approx(measured)

    def test_the_two_panels_do_not_share_a_corner(self):
        from backend.assess.renderer import render_assessment_map

        html = render_assessment_map(county := self._county(), self._empty(),
                                     self._green(), self._empty(), {})
        assert "topleft" in html, "layer control must leave the top-right to the metrics panel"
        assert county is not None

    def test_a_factor_nobody_selected_is_not_reported_as_a_failure(self):
        """'not measured' means we tried and could not. A metric absent from the
        confirmed factor list was never attempted, and saying the same words for
        both is what made an absent Buildings layer look like a broken fetch."""
        from backend.assess.renderer import render_assessment_map

        html = render_assessment_map(
            self._county(), self._empty(), self._green(), self._empty(),
            # greenspace measured; roads requested but failed; buildings absent
            {"greenspace_coverage_ratio": 0.05, "road_density_km_per_km2": None})
        assert "not selected" in html      # buildings
        assert "not measured" in html      # roads
        assert "never fetched" in html and "could not be reached" in html

    def test_the_map_opens_fitted_to_the_study_area(self):
        """zoom_start=13 on a county-wide assessment opened a few km of its
        middle, with the study-area outline — the thing every layer is relative
        to — off screen. The step-1 locator map already fit its bounds."""
        from backend.assess.renderer import render_assessment_map

        html = render_assessment_map(self._county(), self._empty(),
                                     self._green(), self._empty(), {})
        assert "fitBounds" in html

    def test_the_key_is_omitted_when_everything_was_measured(self):
        from backend.assess.renderer import render_assessment_map

        html = render_assessment_map(
            self._county(), self._empty(), self._green(), self._empty(),
            {k: 1.0 for k in ("building_coverage_ratio", "greenspace_coverage_ratio",
                              "road_density_km_per_km2", "parking_coverage_ratio",
                              "pedestrian_density_km_per_km2", "transit_stop_count",
                              "facility_count")})
        assert "not selected" not in html and "not measured" not in html


class TestAnInFlightStepIsVisibleToANewBrowser:
    """A run continues on the server even if the browser goes away, but the
    wizard tracked "is a step running" purely in browser state — so reopening a
    project mid-run showed "Nothing here yet" over work that was actively
    happening, and landed on the finished step BEFORE the running one (a step in
    flight has no artifact row yet, so it is not in the project's step list).

    The fix re-attaches by asking the progress endpoint. These pin the server
    contract that depends on."""

    def test_progress_reports_a_running_step_that_has_no_artifact_yet(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(hcas, "DB_PATH", tmp_path / "hcas.db")
        from backend.app import app

        client = TestClient(app)
        hdrs = {"X-Requested-With": "geoai"}
        pid = client.post("/hcas/projects",
                          json={"title": "t", "state_fips": FRANKLIN[0],
                                "county_fips": FRANKLIN[1]},
                          headers=hdrs).json()["project_id"]
        hcas.set_progress(pid, 3, 40, "fetching 6 map layers…")
        hcas.set_progress_detail(pid, 3, [{"name": "Roads", "layer": "roads",
                                           "source": "OSM", "status": "fetching"}])
        try:
            # No artifact row for step 3 — this is exactly the state a reopening
            # browser sees, and the only way it can tell work is happening.
            assert 3 not in {s["step"] for s in hcas.get_project(pid)["steps"]}
            body = client.get(f"/hcas/projects/{pid}/steps/3/progress",
                              headers=hdrs).json()
            assert body["pct"] == 40
            assert body["detail"] and body["detail"][0]["status"] == "fetching"
        finally:
            hcas.clear_progress(pid, 3)

    def test_a_finished_run_reports_idle_so_the_watcher_stops(
            self, tmp_path, monkeypatch):
        """clear_progress at the end of a run is the completion signal the
        re-attach loop waits for."""
        monkeypatch.setattr(hcas, "DB_PATH", tmp_path / "hcas.db")
        from backend.app import app

        client = TestClient(app)
        hdrs = {"X-Requested-With": "geoai"}
        pid = client.post("/hcas/projects",
                          json={"title": "t", "state_fips": FRANKLIN[0],
                                "county_fips": FRANKLIN[1]},
                          headers=hdrs).json()["project_id"]
        hcas.set_progress(pid, 3, 40, "working")
        hcas.clear_progress(pid, 3)
        body = client.get(f"/hcas/projects/{pid}/steps/3/progress", headers=hdrs).json()
        assert body == {"pct": 0, "label": ""}


class TestAFastMirrorCannotServeStaleData:
    """Measured 2026-08-11 against overpass.private.coffee, one of our own
    rotation entries: 504, then a read timeout, then 200 in 1.6 s carrying
    timestamp_osm_base 2026-05-06 — three months old — then 504 again. An
    earlier call the same hour returned 2026-07-07, so its backends sit at
    different vintages.

    The probe picks whichever endpoint answers FASTEST, and a stale backend
    answers a one-node probe in 1.6 s, so it wins the probe it should lose. A
    run could then take May features for one tile and August for the next inside
    one layer, and nothing on screen would say so."""

    def _resp(self, code=200, ts=None):
        from types import SimpleNamespace

        body = {"osm3s": {"timestamp_osm_base": ts}} if ts else {}
        return SimpleNamespace(status_code=code, json=lambda: body)

    def _fresh_ts(self, days_ago):
        from datetime import datetime, timedelta, timezone

        return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")

    def _probe_with(self, monkeypatch, responder):
        import osmnx as ox

        from backend.assess import osm_layers as ol

        old = ox.settings.overpass_url
        ox.settings.overpass_url = ol._OVERPASS_PRIMARY
        monkeypatch.setattr(ol, "_probed", False)
        monkeypatch.setattr(ol, "_failed_over", False)
        monkeypatch.setattr(ol, "_stale_endpoints", {})
        monkeypatch.setattr(ol, "_stale_warning", None)
        monkeypatch.setattr("requests.post", responder)
        try:
            ol._probe_overpass_once()
            return ox.settings.overpass_url, ol.stale_data_warning(), dict(ol._stale_endpoints)
        finally:
            ox.settings.overpass_url = old
            ol._probed = False

    def test_a_stale_mirror_is_skipped_even_when_it_answers_fastest(self, monkeypatch):
        from backend.assess import osm_layers as ol

        def responder(url, data=None, headers=None, timeout=None):
            if url.startswith(ol._OVERPASS_PRIMARY):
                raise OSError("connect timeout")
            if url.startswith(ol._OVERPASS_MIRROR):
                return self._resp(200, self._fresh_ts(0.2))     # current
            return self._resp(200, "2026-05-06T03:25:00Z")      # three months old
        chosen, warning, stale = self._probe_with(monkeypatch, responder)
        assert chosen == ol._OVERPASS_MIRROR, "picked the current endpoint"
        assert warning is None
        assert ol._OVERPASS_MIRROR2 not in stale or stale[ol._OVERPASS_MIRROR2] > 30

    def test_an_endpoint_with_unreadable_age_is_still_allowed(self, monkeypatch):
        """A response we cannot parse for a timestamp is not evidence of
        staleness; refusing on it would reject working hosts on a format change.
        Fail open on unknown, closed on known-too-old."""
        from backend.assess import osm_layers as ol

        chosen, warning, _ = self._probe_with(
            monkeypatch, lambda url, **k: self._resp(200))   # no osm3s block
        assert chosen == ol._OVERPASS_PRIMARY
        assert warning is None

    def test_when_only_a_stale_mirror_answers_it_is_used_AND_announced(self, monkeypatch):
        """Old data beats no data — but it must arrive wearing its age."""
        from backend.assess import osm_layers as ol

        def responder(url, data=None, headers=None, timeout=None):
            if url.startswith(ol._OVERPASS_MIRROR2):
                return self._resp(200, "2026-05-06T03:25:00Z")
            raise OSError("connect timeout")
        chosen, warning, _ = self._probe_with(monkeypatch, responder)
        assert chosen == ol._OVERPASS_MIRROR2
        assert warning and "days old" in warning

    def test_rotation_does_not_hand_the_run_back_to_a_stale_host(self, monkeypatch):
        """Without this, the probe could correctly refuse a stale mirror and the
        very first timeout would rotate straight back onto it."""
        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_stale_endpoints", {ol._OVERPASS_MIRROR2: 96.0})
        for _ in range(len(ol._OVERPASS_ENDPOINTS) + 1):
            assert ol._next_endpoint(ol._OVERPASS_PRIMARY) != ol._OVERPASS_MIRROR2
            assert ol._next_endpoint(ol._OVERPASS_MIRROR) != ol._OVERPASS_MIRROR2

    def test_the_warning_reaches_the_step3_artifact(self, tmp_path, monkeypatch):
        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_stale_warning",
                            "serving data about 96 days old, so the map features "
                            "below are that old.")
        pid, db = _project(tmp_path)
        _walk(pid, db, through=2)
        draft = hcas.run_step(pid, 3, db=db)
        assert "96 days old" in draft["data_freshness_warning"]

    def test_and_the_report_carries_it_too(self, tmp_path, monkeypatch):
        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_stale_warning", "serving data about 96 days old.")
        pid, db = _project(tmp_path)
        _walk(pid, db, through=8)
        md = hcas.get_project(pid, db=db)["steps"][7]["ai_draft"]["markdown"]
        assert "Out-of-date map data" in md and "96 days old" in md


class TestEveryCaveatSurvives:
    """Each caveat used to assign result["note"], so the last one written won and
    the earlier ones vanished — a run that was both truncated and missing a layer
    disclosed only the truncation."""

    def test_truncation_does_not_swallow_the_omitted_metrics(self, tmp_path, monkeypatch):
        import geopandas as gpd
        from shapely.geometry import box

        from backend.assess import by_unit as bu
        from backend.gis import profile as gis_profile

        monkeypatch.setattr(bu, "MAX_UNITS", 2)
        monkeypatch.setattr(
            gis_profile, "fetch_areal_unit_geometries",
            lambda s, c, unit: gpd.GeoDataFrame(
                {"GEOID": [str(i) for i in range(5)], "NAME": [f"T{i}" for i in range(5)]},
                geometry=[box(i * 100, 0, i * 100 + 100, 100) for i in range(5)],
                crs="EPSG:5070"))
        pid, db = _project(tmp_path, areal_unit="tract")
        out = hcas._tabulate_units(
            hcas.get_project(pid, db=db),
            {"greenspace": gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")},
            hcas._metric_computers(),
            ["greenspace_coverage", "road_density_km_per_km2"],
            {"roads": "DNS lookup failed"},          # one metric unmeasurable
            {"kind": "county"})
        assert "road_density_km_per_km2" in out["note"]   # the omission
        assert "the first 2 are tabulated" in out["note"]  # and the truncation


class TestTheAssistantKnowsWhatItWasNotShown:
    """Prof. Shi 2026-08-07 #10 was the assistant denying a figure it had never
    been given. Step 7 rebuilds that trap on its own: the artifact is clipped to
    fit the prompt, `by_unit` sits near the end, and a per-unit table of hundreds
    of rows is the first thing cut. Clipped silently, the model reads the
    fragment as the whole artifact."""

    def test_a_short_artifact_is_passed_through_untouched(self):
        out = hcas._clip_json({"a": 1}, 6000)
        assert "TRUNCATED" not in out
        assert json.loads(out) == {"a": 1}

    def test_a_clipped_artifact_says_so(self):
        big = {"rows": [{"name": f"Tract {i}", "v": i} for i in range(500)]}
        out = hcas._clip_json(big, 400)
        assert "TRUNCATED" in out
        assert "does not exist" in out  # the instruction not to deny it

    def test_the_step7_prompt_carries_the_warning_when_it_clips(
            self, tmp_path, monkeypatch):
        seen = {}

        def fake_text(system, prompt, model="m"):
            seen["prompt"] = prompt
            return "ok", {"input_tokens": 1, "output_tokens": 1,
                          "cache_read_input_tokens": 0}

        monkeypatch.setattr(hcas, "_llm_text", fake_text)
        pid, db = _project(tmp_path)
        _walk(pid, db, through=7)
        # Fatten step 7's confirmed artifact past the prompt budget, as a real
        # per-unit table does.
        project = hcas.get_project(pid, db=db)
        art = project["steps"][6]["human_final"]
        art["by_unit"] = {"rows": [{"name": f"Tract {i}", "v": i} for i in range(500)]}
        hcas.confirm_step(pid, 7, art, db=db)

        hcas.chat(pid, 7, "what is the greenspace in Tract 480?", db=db)
        assert "TRUNCATED" in seen["prompt"]


class TestTheSubscriptionPathSurvivesATransientCli:
    """The `claude` CLI path already retried a non-zero exit. A live run on
    2026-08-09 hit the other shape — exit 0 with an unreadable envelope, while
    the identical command succeeded from a shell seconds later — which was not
    retried and escaped as a raw JSONDecodeError."""

    SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
              "required": ["ok"], "additionalProperties": False}

    def _proc(self, rc, stdout, stderr=""):
        from types import SimpleNamespace

        return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)

    def _envelope(self, payload):
        return json.dumps({"result": payload, "usage": {"input_tokens": 1}})

    def _run(self, monkeypatch, procs):
        """Feed `procs` to consecutive subprocess.run calls."""
        import subprocess

        calls = {"n": 0}

        def fake_run(*a, **k):
            p = procs[min(calls["n"], len(procs) - 1)]
            calls["n"] += 1
            return p

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("shutil.which", lambda name: "claude")
        out = hcas._llm_via_claude_cli("sys", "prompt", self.SCHEMA, "m")
        return out, calls["n"]

    def test_a_zero_exit_with_garbage_output_is_retried(self, monkeypatch):
        (parsed, _usage), n = self._run(monkeypatch, [
            self._proc(0, "Some notice that is not JSON at all"),
            self._proc(0, self._envelope('{"ok": true}')),
        ])
        assert parsed == {"ok": True}
        assert n == 2, "the unreadable envelope should have been retried once"

    def test_a_reply_that_is_not_json_is_corrected_not_crashed(self, monkeypatch):
        (parsed, _usage), n = self._run(monkeypatch, [
            self._proc(0, self._envelope("Sure! Here is the object you asked for.")),
            self._proc(0, self._envelope('{"ok": true}')),
        ])
        assert parsed == {"ok": True}
        assert n == 2

    def test_the_cli_gets_a_turn_budget_not_a_single_turn(self):
        """--max-turns 1 fails outright whenever the model spends its one turn on
        anything but the final answer. That is what broke step 4 on the shipping
        build twice in a row (2026-08-10), on a machine where `claude` worked
        fine from a terminal — and it is the real cause of the rc=1 recorded as
        "transient" the day before."""
        assert int(hcas.CLI_MAX_TURNS) >= 4

    def test_running_out_of_turns_is_named_not_blamed_on_the_install(self, monkeypatch):
        envelope = json.dumps({"subtype": "error_max_turns",
                               "errors": ["Reached maximum number of turns (1)"]})
        with pytest.raises(RuntimeError) as exc:
            self._run(monkeypatch, [self._proc(1, envelope)])
        msg = str(exc.value)
        assert "turn budget" in msg
        assert "HCAS_CLI_MAX_TURNS" in msg
        # the misleading advice from before: `claude` was working perfectly
        assert "works in a terminal" not in msg

    def test_failing_twice_is_reported_in_plain_language(self, monkeypatch):
        with pytest.raises(RuntimeError) as exc:
            self._run(monkeypatch, [self._proc(0, "still not JSON")])
        msg = str(exc.value)
        assert "Run the step again" in msg
        assert "JSONDecodeError" not in msg and "Expecting value" not in msg

    def test_a_bad_reply_twice_is_also_plain_language(self, monkeypatch):
        with pytest.raises(RuntimeError) as exc:
            self._run(monkeypatch, [self._proc(0, self._envelope("prose, always prose"))])
        assert "could not be used" in str(exc.value)


class TestAFailedStepReportsWhyItFailed:
    """The /run route classified KeyError, ValueError and RuntimeError and let
    everything else out as a bare ASGI 500 with no body. The chat route had
    carried a catch-all since Prof. Shi hit exactly that; the run route — the one
    every step goes through — had not."""

    def _client(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hcas, "DB_PATH", tmp_path / "hcas.db")
        from backend.app import app

        return TestClient(app, raise_server_exceptions=False), {"X-Requested-With": "geoai"}

    def _pid(self, client, hdrs):
        return client.post(
            "/hcas/projects",
            json={"title": "t", "state_fips": FRANKLIN[0], "county_fips": FRANKLIN[1]},
            headers=hdrs).json()["project_id"]

    def test_an_unexpected_error_becomes_a_502_that_names_the_type(
            self, tmp_path, monkeypatch):
        client, hdrs = self._client(tmp_path, monkeypatch)
        pid = self._pid(client, hdrs)

        def boom(*a, **k):
            # jsonschema.ValidationError on the CLI path is exactly this shape:
            # not a KeyError, ValueError or RuntimeError.
            raise TypeError("something the route never anticipated")

        monkeypatch.setattr(hcas, "run_step", boom)
        r = client.post(f"/hcas/projects/{pid}/steps/1/run", headers=hdrs)
        assert r.status_code == 502
        assert "TypeError" in r.json()["detail"]

    def test_an_unreadable_ai_reply_is_not_blamed_on_the_analyst(
            self, tmp_path, monkeypatch):
        """JSONDecodeError IS a ValueError, so it used to surface as a 400
        reading "Expecting value: line 1 column 1" — the provider's fault
        reported as a bad request."""
        client, hdrs = self._client(tmp_path, monkeypatch)
        pid = self._pid(client, hdrs)

        def boom(*a, **k):
            raise json.JSONDecodeError("Expecting value", "", 0)

        monkeypatch.setattr(hcas, "run_step", boom)
        r = client.post(f"/hcas/projects/{pid}/steps/1/run", headers=hdrs)
        assert r.status_code == 502
        assert "could not be read" in r.json()["detail"]

    def test_a_real_bad_request_is_still_a_400(self, tmp_path, monkeypatch):
        """The catch-all must not swallow the genuine 400s: running a step whose
        predecessor is unconfirmed is the analyst's error, not the server's."""
        client, hdrs = self._client(tmp_path, monkeypatch)
        pid = self._pid(client, hdrs)
        r = client.post(f"/hcas/projects/{pid}/steps/3/run", headers=hdrs)
        assert r.status_code == 400
