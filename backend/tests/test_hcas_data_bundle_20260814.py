"""Bring-your-own-data (Xun Shi 2026-08-14, #all-aigis).

"right now sine our data comes from open source platforms, the queue to get
the data is very lengthy ... We may allow the user to download and upload the
data if they want to. They may upload their own data so that they don't have
to download data every time from the opensource and they can use their own
special data if they want to."

Two claims to pin: (1) a layer this project already fetched is never
re-fetched by a later step (step 3 -> step 7 no longer double-fetches the
same bbox), and (2) an uploaded layer is used instead of any fetch at all,
survives a re-run, and is attributed honestly in the artifact.
"""
from __future__ import annotations

import io
import json
import zipfile

import geopandas as gpd
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Point

from backend import hcas
from backend.tests.test_hcas import (  # shared canned fixtures
    FRANKLIN,
    _mock_llm_and_fetchers,  # noqa: F401 — autouse fixture, imported for reuse
    _fake_gdf,
    _project,
)

HDRS = {"X-Requested-With": "geoai"}


def _through_step_2(tmp_path):
    pid, db = _project(tmp_path)
    hcas.confirm_step(pid, 1, hcas.run_step(pid, 1, db=db), db=db)
    hcas.confirm_step(pid, 2, hcas.run_step(pid, 2, db=db), db=db)
    return pid, db


def _counted_fetchers(counts):
    def make(name):
        def fetch(bbox):
            counts[name] = counts.get(name, 0) + 1
            return _fake_gdf(3)
        return fetch
    return lambda: {n: make(n) for n in ("buildings", "greenspace", "roads", "parking",
                                          "sidewalks", "transit_stops", "facilities", "landuse")}


def _geojson_bytes(n=2):
    gdf = gpd.GeoDataFrame(geometry=[Point(i, i) for i in range(n)], crs="EPSG:5070")
    buf = io.BytesIO()
    gdf.to_file(buf, driver="GeoJSON")
    return buf.getvalue()


class TestWithinProjectReuse:
    """Step 3 fetches a layer; step 7's own _fetch_layers call for the same
    project/bbox must not hit the network for it again."""

    def test_step7_reuses_what_step3_already_fetched(self, tmp_path, monkeypatch):
        counts = {}
        monkeypatch.setattr(hcas, "_layer_fetchers", _counted_fetchers(counts))
        pid, db = _through_step_2(tmp_path)  # canned step 2 -> greenspace only
        draft3 = hcas.run_step(pid, 3, db=db)
        hcas.confirm_step(pid, 3, draft3, db=db)
        assert counts == {"greenspace": 1}

        draft4 = hcas.run_step(pid, 4, db=db)
        hcas.confirm_step(pid, 4, draft4, db=db)
        draft5 = hcas.run_step(pid, 5, db=db)
        hcas.confirm_step(pid, 5, draft5, db=db)
        draft6 = hcas.run_step(pid, 6, db=db)
        hcas.confirm_step(pid, 6, draft6, db=db)
        hcas.run_step(pid, 7, db=db)
        # Step 7 needs greenspace too (the confirmed step-6 plan computes the
        # one confirmed metric) — it must come from the step-3 cache, not a
        # second fetch.
        assert counts == {"greenspace": 1}

    def test_a_second_project_reuses_the_download_but_says_so(self, tmp_path, monkeypatch):
        """Reversed 2026-09-16 at Prof. Shi's request ("save the retrieved data
        locally, so that they don't have to be re-retrieved"). This test used to
        demand the opposite — a new project for the same county fetched again —
        so as never to borrow another project's data SILENTLY. The download is
        now shared, but not silently: the layer says it was reused, a Re-fetch
        ignores the saved copy, and a user's upload is never shared at all."""
        counts = {}
        monkeypatch.setattr(hcas, "_layer_fetchers", _counted_fetchers(counts))
        pid1, db = _through_step_2(tmp_path)
        hcas.run_step(pid1, 3, db=db)
        assert counts == {"greenspace": 1}

        pid2 = hcas.create_project("second", *FRANKLIN, db=db)
        hcas.confirm_step(pid2, 1, hcas.run_step(pid2, 1, db=db), db=db)
        hcas.confirm_step(pid2, 2, hcas.run_step(pid2, 2, db=db), db=db)
        hcas.run_step(pid2, 3, db=db)
        assert counts == {"greenspace": 1}
        assert hcas.list_data_bundle(pid2, db=db)["layers"]["greenspace"]["kind"] == "shared"

        hcas.run_step(pid2, 3, db=db, refresh=True)
        assert counts == {"greenspace": 2}

    def test_an_upload_is_never_shared_with_another_project(self, tmp_path, monkeypatch):
        counts = {}
        monkeypatch.setattr(hcas, "_layer_fetchers", _counted_fetchers(counts))
        pid1, db = _through_step_2(tmp_path)
        hcas.import_data_bundle(pid1, "greenspace.geojson", _geojson_bytes(5), db=db)
        hcas.run_step(pid1, 3, db=db)

        pid2 = hcas.create_project("second", *FRANKLIN, db=db)
        hcas.confirm_step(pid2, 1, hcas.run_step(pid2, 1, db=db), db=db)
        hcas.confirm_step(pid2, 2, hcas.run_step(pid2, 2, db=db), db=db)
        draft = hcas.run_step(pid2, 3, db=db)
        assert counts == {"greenspace": 1}
        assert draft["layers"][0]["source"] != "Your uploaded data"


class TestUploadOverridesFetch:
    def test_uploaded_layer_is_never_fetched_and_is_attributed_honestly(self, tmp_path, monkeypatch):
        counts = {}
        monkeypatch.setattr(hcas, "_layer_fetchers", _counted_fetchers(counts))
        pid, db = _through_step_2(tmp_path)
        result = hcas.import_data_bundle(pid, "greenspace.geojson", _geojson_bytes(5), db=db)
        assert result == {"accepted": [{"layer": "greenspace", "feature_count": 5, "crs": "EPSG:5070"}],
                           "rejected": []}

        draft = hcas.run_step(pid, 3, db=db)
        assert counts == {}  # the fetcher was never called
        (layer,) = draft["layers"]
        assert layer["feature_count"] == 5
        assert layer["source"] == "Your uploaded data"

    def test_upload_survives_a_rerun(self, tmp_path, monkeypatch):
        """An uploaded layer must not be silently overwritten by an automatic
        re-fetch — the whole point is the user's data sticks until they clear it."""
        counts = {}
        monkeypatch.setattr(hcas, "_layer_fetchers", _counted_fetchers(counts))
        pid, db = _through_step_2(tmp_path)
        hcas.import_data_bundle(pid, "greenspace.geojson", _geojson_bytes(5), db=db)
        hcas.run_step(pid, 3, db=db)
        draft2 = hcas.run_step(pid, 3, db=db)  # re-run
        assert counts == {}
        assert draft2["layers"][0]["feature_count"] == 5
        assert draft2["layers"][0]["source"] == "Your uploaded data"


class TestUploadValidation:
    def test_unknown_layer_name_rejected_without_crashing(self, tmp_path):
        pid, db = _through_step_2(tmp_path)
        result = hcas.import_data_bundle(pid, "not_a_real_layer.geojson", _geojson_bytes(), db=db)
        assert result["accepted"] == []
        assert "not_a_real_layer" in result["rejected"][0]["reason"]

    def test_malformed_geojson_rejected_without_crashing(self, tmp_path):
        pid, db = _through_step_2(tmp_path)
        result = hcas.import_data_bundle(pid, "greenspace.geojson", b"{not json at all", db=db)
        assert result["accepted"] == []
        assert result["rejected"][0]["file"] == "greenspace.geojson"

    def test_one_bad_file_in_a_zip_does_not_sink_the_others(self, tmp_path):
        pid, db = _through_step_2(tmp_path)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("greenspace.geojson", _geojson_bytes(3))
            zf.writestr("not_a_layer.geojson", _geojson_bytes(1))
        result = hcas.import_data_bundle(pid, "bundle.zip", buf.getvalue(), db=db)
        assert {a["layer"] for a in result["accepted"]} == {"greenspace"}
        assert {r["file"] for r in result["rejected"]} == {"not_a_layer.geojson"}

    def test_unknown_project_404s(self, tmp_path):
        db = tmp_path / "hcas.db"
        with pytest.raises(KeyError):
            hcas.import_data_bundle("nope", "greenspace.geojson", _geojson_bytes(), db=db)


class TestExportImportRoundTrip:
    def test_download_then_upload_into_a_fresh_project(self, tmp_path, monkeypatch):
        counts = {}
        monkeypatch.setattr(hcas, "_layer_fetchers", _counted_fetchers(counts))
        pid1, db = _through_step_2(tmp_path)
        hcas.run_step(pid1, 3, db=db)  # fetches greenspace for real (mocked network)
        blob = hcas.export_data_bundle(pid1, db=db)
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = set(zf.namelist())
            assert "greenspace.geojson" in names
            assert "manifest.json" in names
            manifest = json.loads(zf.read("manifest.json"))
            assert manifest["layers"]["greenspace"]["kind"] == "fetched"

        pid2 = hcas.create_project("reuse", *FRANKLIN, db=db)
        hcas.confirm_step(pid2, 1, hcas.run_step(pid2, 1, db=db), db=db)
        hcas.confirm_step(pid2, 2, hcas.run_step(pid2, 2, db=db), db=db)
        result = hcas.import_data_bundle(pid2, "bundle.zip", blob, db=db)
        assert {a["layer"] for a in result["accepted"]} == {"greenspace"}
        draft = hcas.run_step(pid2, 3, db=db)
        assert counts == {"greenspace": 1}  # only project 1 ever hit the network
        assert draft["layers"][0]["source"] == "Your uploaded data"

    def test_export_before_any_data_404s(self, tmp_path):
        pid, db = _project(tmp_path)
        with pytest.raises(KeyError):
            hcas.export_data_bundle(pid, db=db)


class TestClearOverride:
    def test_clearing_an_upload_reverts_to_fetching(self, tmp_path, monkeypatch):
        counts = {}
        monkeypatch.setattr(hcas, "_layer_fetchers", _counted_fetchers(counts))
        pid, db = _through_step_2(tmp_path)
        hcas.import_data_bundle(pid, "greenspace.geojson", _geojson_bytes(5), db=db)
        hcas.clear_data_override(pid, "greenspace", db=db)
        draft = hcas.run_step(pid, 3, db=db)
        assert counts == {"greenspace": 1}
        assert draft["layers"][0]["feature_count"] == 3  # the fetcher's canned count

    def test_clearing_a_nonexistent_override_404s(self, tmp_path):
        pid, db = _project(tmp_path)
        with pytest.raises(KeyError):
            hcas.clear_data_override(pid, "greenspace", db=db)


class TestScopeChangeClearsCache:
    """A redefined study area must never let a stale upload for the OLD county
    silently answer for the new one."""

    def test_redefining_the_county_drops_the_uploaded_layer(self, tmp_path, monkeypatch):
        counts = {}
        monkeypatch.setattr(hcas, "_layer_fetchers", _counted_fetchers(counts))
        pid, db = _through_step_2(tmp_path)
        hcas.import_data_bundle(pid, "greenspace.geojson", _geojson_bytes(5), db=db)
        hcas.update_project_scope(pid, state_fips="39", county_fips="041", db=db)  # Delaware Co. OH
        hcas.confirm_step(pid, 1, hcas.run_step(pid, 1, db=db), db=db)
        hcas.confirm_step(pid, 2, hcas.run_step(pid, 2, db=db), db=db)
        draft = hcas.run_step(pid, 3, db=db)
        assert counts == {"greenspace": 1}  # fetched for real, upload is gone
        assert draft["layers"][0]["feature_count"] == 3


class TestEndpoints:
    def test_export_import_clear_over_http(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hcas, "DB_PATH", tmp_path / "hcas.db")
        from backend.app import app

        client = TestClient(app)
        pid = client.post("/hcas/projects", json={"title": "t", "state_fips": "39", "county_fips": "049"},
                           headers=HDRS).json()["project_id"]
        draft1 = client.post(f"/hcas/projects/{pid}/steps/1/run", headers=HDRS).json()["ai_draft"]
        client.post(f"/hcas/projects/{pid}/steps/1/confirm", json={"human_final": draft1}, headers=HDRS)
        draft2 = client.post(f"/hcas/projects/{pid}/steps/2/run", headers=HDRS).json()["ai_draft"]
        client.post(f"/hcas/projects/{pid}/steps/2/confirm", json={"human_final": draft2}, headers=HDRS)

        # nothing uploaded yet -> empty list, export 404s
        assert client.get(f"/hcas/projects/{pid}/data", headers=HDRS).json() == {"layers": {}}
        assert client.get(f"/hcas/projects/{pid}/data.zip", headers=HDRS).status_code == 404

        r = client.post(f"/hcas/projects/{pid}/data?filename=greenspace.geojson", headers=HDRS,
                         content=_geojson_bytes(4))
        assert r.status_code == 200
        assert r.json()["accepted"][0]["layer"] == "greenspace"

        assert client.get(f"/hcas/projects/{pid}/data", headers=HDRS).json()["layers"]["greenspace"]["kind"] == "user"
        z = client.get(f"/hcas/projects/{pid}/data.zip", headers=HDRS)
        assert z.status_code == 200
        assert z.headers["content-type"] == "application/zip"

        d = client.delete(f"/hcas/projects/{pid}/data/greenspace", headers=HDRS)
        assert d.status_code == 200
        assert client.delete(f"/hcas/projects/{pid}/data/greenspace", headers=HDRS).status_code == 404

    def test_import_without_csrf_header_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hcas, "DB_PATH", tmp_path / "hcas.db")
        from backend.app import app

        client = TestClient(app)
        r = client.post("/hcas/projects/anything/data?filename=x.geojson", content=b"{}")
        assert r.status_code == 403
