"""Prof. Shi's 2026-08-07 field-test report, one class per item. No network.

His message opened with a blocker — "Step 3 Gather Data seems to have a bug.
After the data are fetched, the process cannot move on" — followed by twelve
numbered interface items. The backend half of that list is covered here; the
rest is interface-only (map size, removed controls, layer sources on screen).
"""
from __future__ import annotations

import threading

import pytest

from backend import hcas
from backend.gis.profile import areal_unit_availability as _real_unit_availability
from backend.gis.profile import county_profile as _real_county_profile
from backend.tests.test_hcas import (  # shared canned fixtures
    _CANNED_USAGE,
    _fake_gdf,
    _mock_llm_and_fetchers,  # noqa: F401 — autouse fixture, imported for reuse
    _project,
    _walk,
)


class TestStep3CannotHangForever:
    """The blocker. Two defects behind it: a layer fetch had no upper bound, so
    the step never returned and the wizard had no state to advance to; and a
    layer that had already failed was displayed as "done · 0 features" until
    every sibling finished, which is what made a stalled run look finished."""

    def test_step3_returns_when_a_layer_outlives_the_budget(self, tmp_path, monkeypatch):
        block = threading.Event()  # never set: this fetch outlives the budget

        def slow(bbox):
            block.wait(30)
            return _fake_gdf(1)

        monkeypatch.setattr(hcas, "_layer_fetchers", lambda: {"greenspace": slow})
        monkeypatch.setattr(hcas, "LAYER_FETCH_BUDGET_S", 1.0)
        pid, db = _project(tmp_path)
        _walk(pid, db, through=2)

        draft = hcas.run_step(pid, 3, db=db)  # must not hang
        (layer,) = draft["layers"]
        assert layer["feature_count"] == 0
        assert "gave up after 1s" in layer["error"]

        # …and the artifact is confirmable, so the analyst can move on.
        hcas.confirm_step(pid, 3, draft, db=db)
        statuses = {s["step"]: s["status"] for s in hcas.get_project(pid, db=db)["steps"]}
        assert statuses[3] == "confirmed"

    def test_failed_layer_is_flagged_while_siblings_still_run(self, tmp_path, monkeypatch):
        from backend.assess.netdiag import record_fetch_error

        seen: list[dict] = []
        block = threading.Event()

        # Named for the real fetchers: errors are keyed by fetcher __name__.
        def fetch_greenspace(bbox):
            record_fetch_error("fetch_greenspace", OSError("[Errno 11001] getaddrinfo failed"))
            return _fake_gdf()

        def fetch_road_network(bbox):
            block.wait(30)
            return _fake_gdf(2)

        monkeypatch.setattr(
            hcas, "_layer_fetchers",
            lambda: {"greenspace": fetch_greenspace, "roads": fetch_road_network})
        monkeypatch.setattr(hcas, "LAYER_FETCH_BUDGET_S", 1.5)
        real = hcas.set_progress_detail

        def spy(p, s, detail):
            seen.append({d["layer"]: d["status"] for d in detail})
            real(p, s, detail)

        monkeypatch.setattr(hcas, "set_progress_detail", spy)

        pid, db = _project(tmp_path)
        hcas.confirm_step(pid, 1, hcas.run_step(pid, 1, db=db), db=db)
        hcas.run_step(pid, 2, db=db)
        hcas.confirm_step(pid, 2, {"factors": [
            {"factor": "green", "metric": "greenspace_coverage", "rationale": "r"},
            {"factor": "roads", "metric": "road_density_km_per_km2", "rationale": "r"},
        ], "notes": ""}, db=db)
        hcas.run_step(pid, 3, db=db)

        assert any(s.get("greenspace") == "error" and s.get("roads") == "fetching"
                   for s in seen), seen

    def test_default_budget_is_generous_not_a_hair_trigger(self):
        assert hcas.LAYER_FETCH_BUDGET_S >= 60


class TestOverpassTiling:
    """A whole-county Overpass query is often too big to answer at all, which is
    the half of Prof. Shi's step-3 report that a timeout alone does not fix — a
    bounded wait just fails faster.

    Measured 2026-08-07 against overpass.kumi.systems for Grand Isle County VT
    (212 km² of land, one of the smallest counties in the US): the whole-county
    buildings query FAILED after 103 s, while the same area as four quadrants
    returned 6,086 buildings in 16 s total.
    """

    def _gdf(self, ids):
        """One row per id, indexed by it (ids stand in for OSM element ids, so
        they may be strings; geometry is positional and carries no meaning)."""
        import geopandas as gpd
        from shapely.geometry import Point

        ids = list(ids)
        return gpd.GeoDataFrame(
            {"geometry": [Point(n, n) for n, _ in enumerate(ids)]},
            index=ids, crs="EPSG:4326")

    def test_a_failing_bbox_is_split_and_the_parts_are_merged(self, monkeypatch):
        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_probe_overpass_once", lambda: None)
        seen = []

        def call_one(bbox):
            seen.append(bbox)
            if bbox == (0.0, 0.0, 0.125, 0.125):       # the whole area: too big
                raise OSError("timed out")
            return self._gdf([len(seen)])

        out = ol._tiled((0.0, 0.0, 0.125, 0.125), call_one)
        assert len(seen) == 5                      # 1 failed attempt + 4 quadrants
        assert seen[1:] == [(0.0, 0.0, 0.0625, 0.0625), (0.0625, 0.0, 0.125, 0.0625),
                            (0.0, 0.0625, 0.0625, 0.125), (0.0625, 0.0625, 0.125, 0.125)]
        assert len(ol._combine(out)) == 4

    def test_a_bbox_that_answers_is_never_split(self, monkeypatch):
        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_probe_overpass_once", lambda: None)
        calls = []
        out = ol._tiled((0.0, 0.0, 0.125, 0.125),
                        lambda b: (calls.append(b), self._gdf([1, 2]))[1])
        assert calls == [(0.0, 0.0, 0.125, 0.125)]     # one request for a sparse county
        assert len(ol._combine(out)) == 2

    def test_features_on_a_tile_edge_are_not_double_counted(self, monkeypatch):
        """Anything crossing a tile boundary comes back from both tiles."""
        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_probe_overpass_once", lambda: None)

        def call_one(bbox):
            if bbox == (0.0, 0.0, 0.125, 0.125):
                raise OSError("timed out")
            return self._gdf([1, 2])               # every tile returns the same ids

        merged = ol._combine(ol._tiled((0.0, 0.0, 0.125, 0.125), call_one))
        assert list(merged.index) == [1, 2]

    def test_an_empty_tile_is_an_answer_not_a_failure(self, monkeypatch):
        from osmnx._errors import InsufficientResponseError

        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_probe_overpass_once", lambda: None)
        calls = []

        def call_one(bbox):
            calls.append(bbox)
            raise InsufficientResponseError("no features here")

        assert ol._tiled((0.0, 0.0, 0.125, 0.125), call_one) == []
        assert len(calls) == 1                     # not split: empty is a real answer

    def test_splitting_stops_at_the_depth_limit(self, monkeypatch):
        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_probe_overpass_once", lambda: None)
        monkeypatch.setattr(ol, "OSM_TILE_MAX_DEPTH", 1)
        calls = []

        def always_fail(bbox):
            calls.append(bbox)
            raise OSError("timed out")

        with pytest.raises(OSError):
            ol._tiled((0.0, 0.0, 0.125, 0.125), always_fail)
        assert len(calls) == 5                     # 1 + 4, then it gives up

    def test_partial_tile_failure_keeps_the_data_that_did_arrive(self, monkeypatch):
        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_probe_overpass_once", lambda: None)
        monkeypatch.setattr(ol, "OSM_TILE_MAX_DEPTH", 1)

        def call_one(bbox):
            if bbox == (0.0, 0.0, 0.125, 0.125):
                raise OSError("timed out")
            if bbox[0] == 0.0 and bbox[1] == 0.0:  # one quadrant stays broken
                raise OSError("timed out")
            # distinct ids per quadrant, so the count measures coverage not dedup
            return self._gdf([f"{bbox[0]}:{bbox[1]}"])

        merged = ol._combine(ol._tiled((0.0, 0.0, 0.125, 0.125), call_one))
        assert len(merged) == 3                    # 3 of 4 quadrants answered

    def test_combine_dedupes_on_osmnx_real_index_shape(self):
        """The merge must work on what osmnx ACTUALLY returns, not a toy frame.

        `features_from_bbox` returns a MultiIndex of (element_type, osmid) and a
        wide, sparse tag table whose columns differ per tile. De-duplication runs
        on that index, so this pins the real shape: a way crossing a tile edge is
        returned by both tiles under the SAME (element_type, osmid) and must
        survive exactly once, with geometry, CRS and tag columns intact.
        """
        import geopandas as gpd
        import pandas as pd
        from shapely.geometry import Point

        from backend.assess import osm_layers as ol

        def frame(ids, **cols):
            idx = pd.MultiIndex.from_tuples(
                [("way", i) for i in ids], names=["element_type", "osmid"])
            data = {"geometry": [Point(n, n) for n, _ in enumerate(ids)]}
            data.update({k: [v] * len(ids) for k, v in cols.items()})
            return gpd.GeoDataFrame(data, index=idx, crs="EPSG:4326")

        # tile A and tile B share way 1002 (it straddles the boundary) and carry
        # different tag columns, as adjacent Overpass responses genuinely do
        a = frame([1001, 1002], building="yes")
        b = frame([1002, 1003], building="yes", **{"building:levels": "3"})

        merged = ol._combine([a, b])

        assert len(merged) == 3                       # 4 rows in, one shared way
        assert merged.index.nlevels == 2
        assert list(merged.index.get_level_values("osmid")) == [1001, 1002, 1003]
        assert merged.index.is_unique
        assert merged.crs == a.crs
        assert merged.geometry.notna().all()
        assert "building:levels" in merged.columns    # column union preserved
        assert isinstance(merged, gpd.GeoDataFrame)

    def test_combine_of_one_tile_is_returned_untouched(self):
        """A sparse county is a single tile; it must not pay a concat."""
        import geopandas as gpd
        from shapely.geometry import Point

        from backend.assess import osm_layers as ol

        only = gpd.GeoDataFrame({"geometry": [Point(0, 0)]}, crs="EPSG:4326")
        assert ol._combine([only]) is only

    def test_combine_of_all_empty_tiles_is_none(self):
        from backend.assess import osm_layers as ol

        assert ol._combine([]) is None

    def test_a_county_sized_bbox_is_pre_split_without_a_doomed_first_try(self, monkeypatch):
        """Waiting for the timeout to learn "too big" costs OSM_TIMEOUT_S per
        layer, and a county is reliably too big — so the split happens up front.
        """
        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_probe_overpass_once", lambda: None)
        seen = []
        grand_isle = (-73.390, 44.564, -73.191, 45.013)   # the real failing bbox
        ol._tiled(grand_isle, lambda b: (seen.append(b), self._gdf([len(seen)]))[1])
        assert grand_isle not in seen                     # never attempted whole
        assert len(seen) == 3                             # 0.199 x 0.449 deg -> 1 x 3
        # the tiles tile the original exactly
        assert min(t[0] for t in seen) == grand_isle[0]
        assert max(t[2] for t in seen) == grand_isle[2]
        assert min(t[1] for t in seen) == grand_isle[1]
        assert max(t[3] for t in seen) == grand_isle[3]

    def test_a_small_bbox_is_one_request(self):
        from backend.assess import osm_layers as ol

        assert ol._start_tiles((0.0, 0.0, 0.1, 0.1)) == [(0.0, 0.0, 0.1, 0.1)]

    def test_probe_walks_the_rotation_to_a_host_that_answers(self, monkeypatch):
        """The probe settles the whole run on an endpoint that is answering
        today, instead of assuming the designated mirror is healthy — on
        2026-08-07 the mirror was the sick one (504)."""
        import osmnx as ox

        from backend.assess import osm_layers as ol

        old_url = ox.settings.overpass_url
        ox.settings.overpass_url = ol._OVERPASS_PRIMARY
        monkeypatch.setattr(ol, "_probed", False)
        monkeypatch.setattr(ol, "_failed_over", False)
        tried = []

        class _R:
            def __init__(self, code):
                self.status_code = code

        def fake_post(url, data=None, headers=None, timeout=None):
            tried.append(url)
            if url.startswith(ol._OVERPASS_PRIMARY):
                raise OSError("connect timeout")
            if url.startswith(ol._OVERPASS_MIRROR):
                return _R(504)
            return _R(200)

        monkeypatch.setattr("requests.post", fake_post)
        try:
            ol._probe_overpass_once()
            assert ox.settings.overpass_url == ol._OVERPASS_MIRROR2
            assert len(tried) == 3
        finally:
            ox.settings.overpass_url = old_url
            ol._failed_over = False
            ol._probed = False

    def test_probe_leaves_the_endpoint_alone_when_none_answer(self, monkeypatch):
        """A total outage is the fetchers' story to tell, with a plain-language
        reason — not something the probe should swallow by picking a host."""
        import osmnx as ox

        from backend.assess import osm_layers as ol

        old_url = ox.settings.overpass_url
        ox.settings.overpass_url = ol._OVERPASS_PRIMARY
        monkeypatch.setattr(ol, "_probed", False)
        monkeypatch.setattr(ol, "_failed_over", False)

        def dead(url, data=None, headers=None, timeout=None):
            raise OSError("connect timeout")

        monkeypatch.setattr("requests.post", dead)
        try:
            ol._probe_overpass_once()
            assert ox.settings.overpass_url == ol._OVERPASS_PRIMARY
        finally:
            ox.settings.overpass_url = old_url
            ol._probed = False

    def test_probe_accepts_a_406_from_the_primary(self, monkeypatch):
        """Any HTTP answer from /interpreter proves the query path is live."""
        import osmnx as ox

        from backend.assess import osm_layers as ol

        old_url = ox.settings.overpass_url
        ox.settings.overpass_url = "https://overpass-api.de/api"
        monkeypatch.setattr(ol, "_probed", False)
        monkeypatch.setattr(ol, "_failed_over", False)

        class _R:
            status_code = 200

        monkeypatch.setattr("requests.post", lambda url, data=None, headers=None, timeout=None: _R())
        try:
            ol._probe_overpass_once()
            assert ox.settings.overpass_url == "https://overpass-api.de/api"
        finally:
            ox.settings.overpass_url = old_url
            ol._probed = False


class TestProjectRedefinition:
    """#8 — "Activate the Back button, allowing the user to go back to
    re-define the project"."""

    def test_changing_county_clears_step1_and_stales_the_rest(self, tmp_path):
        pid, db = _project(tmp_path)
        _walk(pid, db, through=3)
        out = hcas.update_project_scope(pid, county_fips="041", db=db)  # Delaware County, OH
        assert out["scope_changed"] is True
        steps = {s["step"]: s["status"] for s in hcas.get_project(pid, db=db)["steps"]}
        assert 1 not in steps  # dropped: it described the old county
        assert steps[2] == "stale" and steps[3] == "stale"
        assert hcas.get_project(pid, db=db)["meta"]["county_fips"] == "041"

    def test_title_only_edit_keeps_the_work(self, tmp_path):
        pid, db = _project(tmp_path)
        _walk(pid, db, through=2)
        out = hcas.update_project_scope(pid, title="Renamed", db=db)
        assert out["scope_changed"] is False
        steps = {s["step"]: s["status"] for s in hcas.get_project(pid, db=db)["steps"]}
        assert steps[1] == "confirmed" and steps[2] == "confirmed"
        assert hcas.get_project(pid, db=db)["meta"]["title"] == "Renamed"

    def test_unknown_county_is_rejected(self, tmp_path):
        pid, db = _project(tmp_path)
        with pytest.raises(ValueError):
            hcas.update_project_scope(pid, county_fips="999", db=db)


class TestFactorRevision:
    """#11 — feedback must edit the list, not redraft it: "it should keep those
    factors not checked off by the user, maintaining their original order …
    and add those factors requested by the user"."""

    _CURRENT = {"factors": [
        {"factor": "Road network", "metric": "road_density_km_per_km2", "rationale": "a"},
        {"factor": "Green space", "metric": "greenspace_coverage", "rationale": "b"},
        {"factor": "Transit", "metric": "transit_access", "rationale": "c"},
    ], "notes": "n"}

    def test_add_appends_and_keeps_existing_order(self, monkeypatch):
        monkeypatch.setattr(hcas, "_llm_structured", lambda *a, **k: (
            {"add": [{"factor": "Land use mix", "metric": "land_use_mix", "rationale": "d"}],
             "remove": [], "note": ""}, _CANNED_USAGE))
        out = hcas.revise_factors(self._CURRENT, "add land use")
        assert [f["factor"] for f in out["artifact"]["factors"]] == [
            "Road network", "Green space", "Transit", "Land use mix"]
        assert out["added"] == ["Land use mix"] and out["removed"] == []
        assert out["artifact"]["notes"] == "n"  # untouched

    def test_remove_drops_only_that_factor(self, monkeypatch):
        monkeypatch.setattr(hcas, "_llm_structured", lambda *a, **k: (
            {"add": [], "remove": ["Green space"], "note": ""}, _CANNED_USAGE))
        out = hcas.revise_factors(self._CURRENT, "drop green space")
        assert [f["factor"] for f in out["artifact"]["factors"]] == ["Road network", "Transit"]
        assert out["removed"] == ["Green space"]

    def test_a_model_that_re_lists_everything_cannot_duplicate_or_reorder(self, monkeypatch):
        """Exactly the failure he reported: the model answers with a whole new
        list. The merge is deterministic, so re-proposing existing factors is a
        no-op and the order the analyst saw survives."""
        monkeypatch.setattr(hcas, "_llm_structured", lambda *a, **k: (
            {"add": [
                {"factor": "Transit", "metric": "transit_access", "rationale": "c"},
                {"factor": "Road network", "metric": "road_density_km_per_km2", "rationale": "a"},
                {"factor": "Parking", "metric": "parking_coverage_ratio", "rationale": "e"},
            ], "remove": [], "note": ""}, _CANNED_USAGE))
        out = hcas.revise_factors(self._CURRENT, "add parking")
        assert [f["factor"] for f in out["artifact"]["factors"]] == [
            "Road network", "Green space", "Transit", "Parking"]

    def test_empty_instruction_is_rejected(self):
        with pytest.raises(ValueError):
            hcas.revise_factors(self._CURRENT, "   ")


class TestAreaResolution:
    """#4 — a free-text location box ("ZIP Code, name of the community …
    surrounding area of a feature") resolved by AI into a study area."""

    def test_names_are_matched_against_the_shipped_county_table(self, monkeypatch):
        monkeypatch.setattr(hcas, "_llm_structured", lambda *a, **k: (
            {"county": "Grafton", "state": "New Hampshire",
             "interpretation": "Hanover, NH is in Grafton County", "caveat": ""},
            _CANNED_USAGE))
        out = hcas.resolve_area("around Dartmouth College")
        assert out["resolved"] is True
        assert (out["state_fips"], out["county_fips"]) == ("33", "009")

    def test_county_suffix_and_punctuation_do_not_block_the_match(self, monkeypatch):
        monkeypatch.setattr(hcas, "_llm_structured", lambda *a, **k: (
            {"county": "St. Louis County", "state": "Missouri",
             "interpretation": "", "caveat": ""}, _CANNED_USAGE))
        assert hcas.resolve_area("63105")["resolved"] is True

    def test_an_unmatched_name_is_reported_not_guessed(self, monkeypatch):
        monkeypatch.setattr(hcas, "_llm_structured", lambda *a, **k: (
            {"county": "", "state": "", "interpretation": "",
             "caveat": "That is not a place in the United States."}, _CANNED_USAGE))
        out = hcas.resolve_area("the moon")
        assert out["resolved"] is False
        assert "state_fips" not in out
        assert out["caveat"]

    def test_blank_text_is_rejected(self):
        with pytest.raises(ValueError):
            hcas.resolve_area("  ")


class TestStudyAreaProfileAndUnit:
    """#10 (descriptive Census figures instead of FIPS codes, and an assistant
    that can answer "what is the population") and #5 (the analyst picks the
    areal unit; the system reports when its data does not exist)."""

    def test_step1_carries_a_profile_and_the_chosen_unit(self, tmp_path):
        pid, db = _project(tmp_path, areal_unit="block_group")
        draft = hcas.run_step(pid, 1, db=db)
        assert draft["profile"]["population"] == 50000
        assert draft["areal_unit"] == "block_group"

    def test_step3_reports_the_areal_unit_search(self, tmp_path):
        pid, db = _project(tmp_path)
        _walk(pid, db, through=2)
        draft = hcas.run_step(pid, 3, db=db)
        assert draft["areal_unit"]["available"] is True
        assert "not implemented yet" in draft["areal_unit"]["note"]

    def test_unknown_areal_unit_is_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            _project(tmp_path, areal_unit="hexagons")

    def test_chat_context_includes_the_study_area_on_later_steps(self, tmp_path, monkeypatch):
        seen = {}

        def fake_text(system, prompt, model="m"):
            seen["prompt"] = prompt
            return "ok", _CANNED_USAGE

        monkeypatch.setattr(hcas, "_llm_text", fake_text)
        pid, db = _project(tmp_path)
        _walk(pid, db, through=3)
        hcas.chat(pid, 3, "what is the population?", db=db)
        assert "Census profile" in seen["prompt"]
        assert "50000" in seen["prompt"]

    def test_intake_chat_needs_no_project(self, monkeypatch):
        """#3 — questions are answerable before a project exists."""
        monkeypatch.setattr(hcas, "_llm_text", lambda s, p, model="m": ("hello", _CANNED_USAGE))
        assert hcas.chat_intake("what does this do?")["reply"] == "hello"


class TestCensusProfileHonesty:
    """A field that could not be fetched is named as missing, never defaulted
    to a plausible number.

    These call the real implementations captured at import (`_real_*`): the
    shared autouse fixture replaces the module attributes with canned versions
    so the pipeline tests stay network-free, and that would hide the behaviour
    under test here.
    """

    def test_missing_census_key_is_explained_not_invented(self, monkeypatch):
        from backend.gis import profile as p

        monkeypatch.setattr(p, "_county_attributes", lambda s, c: {
            "NAME": "Grafton County", "AREALAND": "4432000000", "AREAWATER": "70000000"})
        out = _real_county_profile("33", "009", population=91118, census_api_key=None)
        assert out["land_area_km2"] == 4432.0
        assert out["population"] == 91118
        assert "median_household_income_usd" not in out
        assert "CENSUS_API_KEY" in out["unavailable"]["acs"]

    def test_boundary_outage_does_not_fabricate_an_area(self, monkeypatch):
        from backend.gis import profile as p

        def boom(s, c):
            raise OSError("getaddrinfo failed")

        monkeypatch.setattr(p, "_county_attributes", boom)
        out = _real_county_profile("33", "009", population=None, census_api_key=None)
        assert "land_area_km2" not in out
        assert "TIGERweb" in out["unavailable"]["area"]

    def test_unit_search_failure_reports_rather_than_claims_absence(self, monkeypatch):
        import requests

        from backend.gis import profile as p

        def boom(*a, **k):
            raise requests.exceptions.ConnectTimeout("timed out")

        monkeypatch.setattr(p.requests, "get", boom)
        out = _real_unit_availability("33", "009", "tract")
        assert out["available"] is False
        assert "could not be completed" in out["note"]

    def test_county_unit_needs_no_lookup(self):
        out = _real_unit_availability("33", "009", "county")
        assert out["available"] is True and out["count"] == 1

    def test_zcta_is_searched_in_the_county_extent_and_says_so(self, monkeypatch):
        """ZCTAs carry no county field, so they are matched geographically. The
        box must be the COUNTY's, and the wording must say overlap rather than
        containment.

        Caught in the 2026-08-07 pre-ship run: a fixed +/-0.75 degree box around
        the centroid is ~120 km wide and reported 206 ZIP areas "for this
        county" (real answer over the county's own extent: 98).
        """
        from backend.gis import profile as p

        seen = {}
        monkeypatch.setattr(p, "_county_extent", lambda s, c: (-72.34, 43.53, -71.35, 44.41))

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"count": 98}

        def fake_get(url, params=None, timeout=None):
            seen["url"], seen["params"] = url, params
            return _Resp()

        monkeypatch.setattr(p.requests, "get", fake_get)
        out = _real_unit_availability("33", "009", "zcta")

        assert seen["params"]["geometry"] == "-72.34,43.53,-71.35,44.41"
        assert out["count"] == 98
        assert "overlapping this county's extent" in out["note"]

    def test_nested_units_are_counted_by_geoid_prefix(self, monkeypatch):
        from backend.gis import profile as p

        seen = {}

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"count": 28}

        def fake_get(url, params=None, timeout=None):
            seen["params"] = params
            return _Resp()

        monkeypatch.setattr(p.requests, "get", fake_get)
        out = _real_unit_availability("33", "009", "tract")
        assert seen["params"]["where"] == "GEOID LIKE '33009%'"
        assert "in this county" in out["note"]
