"""Prof. Shi and Prof. Wang's 2026-09-16 feedback document, one class per item.

Posted in #all-aigis as Feedback_forJason_260916.docx after both walked the
knowledgebase build end to end. Six items: define the community flexibly,
faster data retrieval, compare with a base area, "Save as", interface fixes,
and AI answers that refuse to add factors the system cannot yet measure.
No network: every Census, CDC, EPA and OpenStreetMap call is stubbed.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import hcas
from backend.tests.test_hcas import (  # shared canned fixtures
    FRANKLIN,
    _fake_gdf,
    _mock_llm_and_fetchers,  # noqa: F401 — autouse fixture, imported for reuse
    _project,
    _walk,
)

HDRS = {"X-Requested-With": "geoai"}
TEMPLATE = Path(__file__).resolve().parents[2] / "frontend" / "templates" / "hcas.html"


class TestSaveAs:
    """#4: "a project can only be saved to itself, i.e., a new project cannot
    start based on an existing project." """

    def test_copy_keeps_every_step_and_leaves_the_original_alone(self, tmp_path):
        pid, db = _project(tmp_path)
        _walk(pid, db, through=3)
        new = hcas.copy_project(pid, "Second look", db=db)

        assert new != pid
        src, dst = hcas.get_project(pid, db=db), hcas.get_project(new, db=db)
        assert dst["meta"]["title"] == "Second look"
        assert dst["meta"]["copied_from"] == pid
        assert [(s["step"], s["status"]) for s in dst["steps"]] == \
               [(s["step"], s["status"]) for s in src["steps"]]
        assert dst["steps"][2]["ai_draft"] == src["steps"][2]["ai_draft"]

        # Working in the copy never reaches back into the original.
        hcas.confirm_step(new, 1, {**dst["steps"][0]["ai_draft"], "edited": True}, db=db)
        assert [s["status"] for s in hcas.get_project(pid, db=db)["steps"]] == ["confirmed"] * 3
        assert "copied_from" not in hcas.get_project(pid, db=db)["meta"]

    def test_copy_takes_the_map_and_the_fetched_data(self, tmp_path):
        pid, db = _project(tmp_path)
        _walk(pid, db, through=7)
        cache = hcas._project_cache_dir(pid)
        (cache / "greenspace.abc.geojson").write_text("{}")
        new = hcas.copy_project(pid, None, db=db)

        assert hcas.get_map(new, db=db) == hcas.get_map(pid, db=db)
        assert (hcas.LAYER_CACHE_DIR / new / "greenspace.abc.geojson").exists()
        assert hcas.get_project(new, db=db)["meta"]["title"] == "test (copy)"

    def test_audit_csv_marks_copied_rows(self, tmp_path):
        pid, db = _project(tmp_path)
        _walk(pid, db, through=2)
        new = hcas.copy_project(pid, "copy", db=db)
        rows = list(csv.DictReader(io.StringIO(hcas.export_audit_csv(db=db))))
        assert {r["copied_from"] for r in rows if r["project_id"] == new} == {pid}
        assert {r["copied_from"] for r in rows if r["project_id"] == pid} == {""}

    def test_unknown_project_is_404(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hcas, "DB_PATH", tmp_path / "hcas.db")
        from backend.app import app

        client = TestClient(app)
        r = client.post("/hcas/projects/nope/copy", json={"title": "x"}, headers=HDRS)
        assert r.status_code == 404

    def test_endpoint_returns_the_new_project(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hcas, "DB_PATH", tmp_path / "hcas.db")
        from backend.app import app

        client = TestClient(app)
        pid = client.post("/hcas/projects", json={"title": "t", "state_fips": "39",
                                                  "county_fips": "049"}, headers=HDRS).json()["project_id"]
        r = client.post(f"/hcas/projects/{pid}/copy", json={"title": "t2"}, headers=HDRS)
        assert r.status_code == 200
        assert client.get(f"/hcas/projects/{r.json()['project_id']}", headers=HDRS).json()["meta"]["title"] == "t2"


class TestRetrievalRecoversFromAnOutage:
    """#2: "the retrieval process is slow and often failed". The log of the
    professors' own run shows why: the first probe of the day found every
    endpoint down, and that verdict never expired — every later layer of every
    later run got one attempt and no rotation, long after the servers came back.
    """

    def _probe(self, monkeypatch, responder):
        import osmnx as ox

        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ox.settings, "overpass_url", ol._OVERPASS_PRIMARY)
        monkeypatch.setattr(ol, "_failed_over", False)
        monkeypatch.setattr(ol, "_stale_endpoints", {})
        monkeypatch.setattr(ol, "_stale_warning", None)
        monkeypatch.setattr(ol, "_learn_dead_addresses", lambda host: None)
        monkeypatch.setattr("requests.post", responder)
        ol._probe_overpass_once()
        return ox.settings.overpass_url

    def test_an_outage_verdict_is_rechecked(self, monkeypatch):
        from backend.assess import osm_layers as ol

        class _R:
            status_code = 200

            def json(self):
                return {}

        def down(url, **kw):
            raise OSError("connect timeout")

        monkeypatch.setattr(ol, "_probed", False)
        self._probe(monkeypatch, down)
        assert ol._outage_suspected()

        # Within the recheck window nothing is re-asked…
        calls = []
        monkeypatch.setattr("requests.post", lambda url, **kw: calls.append(url) or _R())
        ol._probe_overpass_once()
        assert calls == [] and ol._outage_suspected()

        # …and once it lapses the servers get a fresh hearing.
        monkeypatch.setattr(ol, "_probe_expires_at", 0.0)
        self._probe(monkeypatch, lambda url, **kw: _R())
        assert not ol._outage_suspected()

    def test_a_working_endpoint_is_not_reprobed_every_layer(self, monkeypatch):
        from backend.assess import osm_layers as ol

        class _R:
            status_code = 200

            def json(self):
                return {}

        calls = []
        monkeypatch.setattr(ol, "_probed", False)
        self._probe(monkeypatch, lambda url, **kw: calls.append(url) or _R())
        ol._probe_overpass_once()
        ol._probe_overpass_once()
        assert len(calls) == 1

    def test_the_rotation_has_more_than_one_machine_behind_it(self):
        """kumi.systems and private.coffee resolve to the same address, so
        before this the only real alternative to overpass-api.de was one box."""
        from urllib.parse import urlsplit

        from backend.assess import osm_layers as ol

        hosts = [urlsplit(e).hostname for e in ol._OVERPASS_ENDPOINTS]
        assert "z.overpass-api.de" in hosts and "lz4.overpass-api.de" in hosts
        assert hosts.index("z.overpass-api.de") < hosts.index("overpass.private.coffee")


def _polys(n, x0=0.0, y0=0.0, crs="EPSG:5070"):
    import geopandas as gpd
    from shapely.geometry import box

    return gpd.GeoDataFrame({"building": ["yes"] * n},
                            geometry=[box(x0 + i * 10, y0, x0 + i * 10 + 5, y0 + 5) for i in range(n)],
                            crs=crs)


class TestSavedDataIsReusedAcrossProjects:
    """#2: "Save the retrieved data locally, so that they don't have to be
    re-retrieved when the user comes back to the same project." The per-project
    cache did that for one project id; the same county under a new project, or
    a neighbourhood inside a county already fetched, paid the whole fetch again.
    """

    BBOX = (-83.2, 39.8, -82.8, 40.1)

    def test_a_second_project_reuses_the_first_projects_download(self, monkeypatch):
        calls = []

        def fetch_buildings(bbox):
            calls.append(bbox)
            return _polys(3)

        monkeypatch.setattr(hcas, "_layer_fetchers", lambda: {"buildings": fetch_buildings})
        r1, e1, _ = hcas._fetch_layers("proj-a", 3, ["buildings"], self.BBOX, budget_s=30)
        r2, e2, d2 = hcas._fetch_layers("proj-b", 3, ["buildings"], self.BBOX, budget_s=30)

        assert len(calls) == 1, "the second project fetched again"
        assert not e1 and not e2
        assert len(r2["buildings"]) == 3
        assert d2[0]["cached"] == "shared" and "reused" in d2[0]["note"]
        assert hcas.list_data_bundle.__name__  # manifest notes the reuse:
        manifest = json.loads((hcas.LAYER_CACHE_DIR / "proj-b" / "manifest.json").read_text())
        assert manifest["layers"]["buildings"]["kind"] == "shared"

    def test_an_area_inside_a_cached_area_is_cut_from_it(self, monkeypatch):
        import geopandas as gpd
        from shapely.geometry import Point

        calls = []
        # Two buildings: one inside the small box, one far outside it.
        inside = gpd.GeoSeries([Point(-83.0, 39.95)], crs="EPSG:4326").to_crs("EPSG:5070").iloc[0]
        outside = gpd.GeoSeries([Point(-82.85, 40.08)], crs="EPSG:4326").to_crs("EPSG:5070").iloc[0]
        layer = gpd.GeoDataFrame({"building": ["a", "b"]},
                                 geometry=[inside.buffer(5), outside.buffer(5)], crs="EPSG:5070")

        def fetch_buildings(bbox):
            calls.append(bbox)
            return layer

        monkeypatch.setattr(hcas, "_layer_fetchers", lambda: {"buildings": fetch_buildings})
        hcas._fetch_layers("county", 3, ["buildings"], self.BBOX, budget_s=30)
        small = (-83.05, 39.9, -82.95, 40.0)
        res, errs, detail = hcas._fetch_layers("hood", 3, ["buildings"], small, budget_s=30)

        assert len(calls) == 1
        assert list(res["buildings"]["building"]) == ["a"]
        assert detail[0]["count"] == 1

    def test_expired_entries_are_fetched_again(self, monkeypatch):
        calls = []
        monkeypatch.setattr(hcas, "_layer_fetchers",
                            lambda: {"buildings": lambda bbox: calls.append(1) or _polys(1)})
        hcas._fetch_layers("p1", 3, ["buildings"], self.BBOX, budget_s=30)
        monkeypatch.setattr(hcas, "SHARED_CACHE_DAYS", 0.0)
        hcas._fetch_layers("p2", 3, ["buildings"], self.BBOX, budget_s=30)
        assert len(calls) == 2

    def test_refresh_downloads_again_but_keeps_an_upload(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(hcas, "_layer_fetchers", lambda: {
            "buildings": lambda bbox: calls.append("b") or _polys(2),
            "facilities": lambda bbox: calls.append("f") or _polys(2),
        })
        pid = "refresh-me"
        hcas._fetch_layers(pid, 3, ["buildings", "facilities"], self.BBOX, budget_s=30)
        _polys(7).to_file(hcas._user_layer_path(pid, "facilities"), driver="GeoJSON")
        calls.clear()
        res, errs, _ = hcas._fetch_layers(pid, 3, ["buildings", "facilities"], self.BBOX,
                                          budget_s=30, refresh=True)
        assert calls == ["b"]
        assert len(res["facilities"]) == 7

    def test_a_failed_download_is_never_saved_as_an_empty_layer(self, monkeypatch):
        from backend.assess.netdiag import record_fetch_error

        def fetch_buildings(bbox):
            record_fetch_error("fetch_buildings", OSError("timed out"))
            return _polys(0)

        monkeypatch.setattr(hcas, "_layer_fetchers", lambda: {"buildings": fetch_buildings})
        _, errs, _ = hcas._fetch_layers("p", 3, ["buildings"], self.BBOX, budget_s=30)
        assert "buildings" in errs
        assert hcas._shared_layer("buildings", self.BBOX) is None
        assert hcas._cached_layer("p2", "buildings", self.BBOX) == (None, None)

    def test_osm_lists_survive_the_geopackage(self, monkeypatch):
        layer = _polys(2)
        layer["nodes"] = [[1, 2, 3], [4, 5]]
        monkeypatch.setattr(hcas, "_layer_fetchers", lambda: {"buildings": lambda bbox: layer})
        hcas._fetch_layers("p", 3, ["buildings"], self.BBOX, budget_s=30)
        hit = hcas._shared_layer("buildings", self.BBOX)
        assert hit is not None and len(hit[0]) == 2


class TestOneDownloadPerLayer:
    """A prefetch and a step (or two users) wanting the same layer for the same
    area must not both ask Overpass — the second waits and reads the first."""

    def test_a_concurrent_request_waits_for_the_download_in_flight(self, monkeypatch):
        import threading

        started, release, calls = threading.Event(), threading.Event(), []

        def fetch_buildings(bbox):
            calls.append(1)
            started.set()
            release.wait(10)
            return _polys(4)

        monkeypatch.setattr(hcas, "_layer_fetchers", lambda: {"buildings": fetch_buildings})
        bbox = (-83.2, 39.8, -82.8, 40.1)
        out = {}
        first = threading.Thread(target=lambda: out.setdefault(
            "a", hcas._fetch_layers("p-a", 0, ["buildings"], bbox, budget_s=30)))
        first.start()
        assert started.wait(5)
        second = threading.Thread(target=lambda: out.setdefault(
            "b", hcas._fetch_layers("p-b", 3, ["buildings"], bbox, budget_s=30)))
        second.start()
        release.set()
        first.join(10)
        second.join(10)

        assert len(calls) == 1
        res, errs, detail = out["b"]
        assert not errs and len(res["buildings"]) == 4
        assert detail[0]["cached"] in ("inflight", "shared")

    def test_an_error_stays_with_the_run_that_had_it(self, monkeypatch):
        """Errors used to sit in one process-wide registry popped by name. Two
        runs at once could each read the other's failure — so a run could cache
        an empty layer as real because another run had drained its error."""
        from backend.assess.netdiag import record_fetch_error

        def fetch_roads(bbox):
            record_fetch_error("fetch_road_network", OSError("timed out"))
            return _polys(0)

        fetch_roads.__name__ = "fetch_road_network"
        monkeypatch.setattr(hcas, "_layer_fetchers", lambda: {"roads": fetch_roads,
                                                              "buildings": lambda b: _polys(1)})
        _, errs, _ = hcas._fetch_layers("p", 3, ["roads", "buildings"], (0, 0, 1, 1), budget_s=30)
        assert set(errs) == {"roads"}
        from backend.assess.netdiag import take_fetch_errors

        assert take_fetch_errors() == {}, "the failure leaked into the global registry"


class TestPrefetch:
    """Once the area is confirmed, the layers start downloading — step 3 no
    longer begins from nothing after minutes spent choosing factors."""

    def test_confirming_step_1_downloads_every_layer_in_the_background(self, tmp_path, monkeypatch):
        import time

        calls = []

        def make(name):
            return lambda bbox: calls.append(name) or _polys(1)

        monkeypatch.setattr(hcas, "_layer_fetchers", lambda: {n: make(n) for n in hcas.LAYER_META})
        monkeypatch.setattr(hcas, "PREFETCH_ENABLED", True)
        pid, db = _project(tmp_path)
        hcas.confirm_step(pid, 1, hcas.run_step(pid, 1, db=db), db=db)
        deadline = time.time() + 20
        while time.time() < deadline and pid in hcas._prefetching:
            time.sleep(0.05)
        assert sorted(calls) == sorted(hcas.LAYER_META)

        # Step 3 then fetches nothing at all.
        calls.clear()
        hcas.confirm_step(pid, 2, hcas.run_step(pid, 2, db=db), db=db)
        draft = hcas.run_step(pid, 3, db=db)
        assert calls == []
        assert all(not l.get("error") for l in draft["layers"])

    def test_it_is_off_in_the_test_suite_by_default(self):
        assert hcas.PREFETCH_ENABLED is False


class TestInterface:
    """#5, checked against the page source: these are layout promises, and the
    template is the only place they live."""

    html = TEMPLATE.read_text(encoding="utf-8")

    def test_menu_comes_before_the_system_name(self):
        hdr = self.html[self.html.index('<div id="hdr">'):]
        assert hdr.index('id="nav"') < hdr.index("Healthy Community Assessment System</div>")

    def test_edit_underlying_data_is_gone(self):
        assert "Edit underlying data…" not in self.html
        assert "openRawEditor" not in self.html and "rawEditor()" not in self.html

    def test_long_notes_open_in_their_own_window(self):
        assert "function openNotes" in self.html
        assert 'notesBlock("step2"' in self.html

    def test_save_as_is_on_the_menu(self):
        assert 'onclick="saveProjectAs()"' in self.html
