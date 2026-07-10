"""Tests for the HCAS 8-step gated machine (backend/hcas.py). No network."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import hcas

FRANKLIN = ("39", "049")  # Franklin County, OH

_CANNED_USAGE = {"input_tokens": 900, "output_tokens": 200, "cache_read_input_tokens": 0}

_CANNED_STEP2 = {
    "factors": [
        {
            "factor": "greenspace access",
            "metric": "greenspace_coverage",
            "rationale": "Green space correlates with better mental and physical health.",
        }
    ],
    "notes": "Air quality not yet measurable by the registry.",
}

_CANNED_STEP4 = {
    "framework": "Greenspace-led health equity assessment.",
    "theoretical_basis": [{"theory": "WHO Healthy Cities", "application": "settings-based lens"}],
    "assumptions": ["OSM greenspace tagging is representative."],
    "limitations": ["Built-environment metrics only."],
    "references": ["The relationship between greenspace and health: a systematic review"],
}

_CANNED_LLM = (_CANNED_STEP2, _CANNED_USAGE)


def _canned_llm(system, prompt, schema, model="m"):
    """Schema-aware canned LLM: step-4 calls get framework shape."""
    shape = _CANNED_STEP4 if "framework" in schema["properties"] else _CANNED_STEP2
    return shape, _CANNED_USAGE


def _fake_gdf(n=0):
    import geopandas as gpd
    from shapely.geometry import Point

    return gpd.GeoDataFrame(geometry=[Point(i, i) for i in range(n)], crs="EPSG:5070")


@pytest.fixture(autouse=True)
def _mock_llm_and_fetchers(monkeypatch):
    """No test in this file talks to the real API or Overpass; override as needed."""
    monkeypatch.setattr(hcas, "_llm_structured", _canned_llm)
    all_layers = {layer for layers in hcas.METRIC_LAYERS.values() for layer in layers}
    monkeypatch.setattr(
        hcas,
        "_layer_fetchers",
        lambda: {name: (lambda bbox: _fake_gdf()) for name in all_layers},
    )


def _project(tmp_path, **kw):
    db = tmp_path / "hcas.db"
    pid = hcas.create_project("test", *FRANKLIN, db=db, **kw)
    return pid, db


def _walk(pid, db, through=8):
    for n in range(1, through + 1):
        draft = hcas.run_step(pid, n, db=db)
        hcas.confirm_step(pid, n, draft, db=db)


class TestMachine:
    def test_step1_drafts_real_area_artifact(self, tmp_path):
        pid, db = _project(tmp_path)
        draft = hcas.run_step(pid, 1, db=db)
        assert draft["kind"] == "us_county"
        assert "Franklin" in draft["place_name"]
        assert len(draft["centroid"]) == 2

    def test_gate_blocks_unconfirmed_predecessors(self, tmp_path):
        pid, db = _project(tmp_path)
        hcas.run_step(pid, 1, db=db)  # drafted, not confirmed
        with pytest.raises(ValueError, match="not confirmed"):
            hcas.run_step(pid, 2, db=db)

    def test_confirm_records_edit_delta_only_when_edited(self, tmp_path):
        pid, db = _project(tmp_path)
        draft = hcas.run_step(pid, 1, db=db)
        hcas.confirm_step(pid, 1, draft, edit_seconds=3.5, db=db)
        step1 = hcas.get_project(pid, db=db)["steps"][0]
        assert step1["status"] == "confirmed"
        assert step1["edit_delta"] is None
        assert step1["edit_seconds"] == 3.5

        draft2 = hcas.run_step(pid, 2, db=db)
        edited = {**draft2, "note": "human touched this"}
        hcas.confirm_step(pid, 2, edited, db=db)
        step2 = hcas.get_project(pid, db=db)["steps"][1]
        assert "human touched this" in step2["edit_delta"]
        assert step2["human_final"] == edited

    def test_full_walk_confirms_all_8(self, tmp_path):
        pid, db = _project(tmp_path)
        _walk(pid, db)
        steps = hcas.get_project(pid, db=db)["steps"]
        assert [s["status"] for s in steps] == ["confirmed"] * 8

    def test_upstream_reconfirm_stale_flags_downstream(self, tmp_path):
        pid, db = _project(tmp_path)
        _walk(pid, db)
        area = hcas.get_project(pid, db=db)["steps"][0]["ai_draft"]
        result = hcas.confirm_step(pid, 1, {**area, "edited": True}, db=db)
        assert result["stale_downstream"] == 7
        statuses = [s["status"] for s in hcas.get_project(pid, db=db)["steps"]]
        assert statuses == ["confirmed"] + ["stale"] * 7
        # step 1 confirmed again, so step 2 may re-run
        hcas.run_step(pid, 2, db=db)

    def test_unknown_county_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown county"):
            hcas.create_project("bad", "99", "999", db=tmp_path / "hcas.db")

    def test_bad_gate_mode_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="gate_mode"):
            hcas.create_project("bad", *FRANKLIN, gate_mode="yolo", db=tmp_path / "hcas.db")


class TestEndpoints:
    HDRS = {"X-Requested-With": "geoai"}

    def test_endpoint_walk(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hcas, "DB_PATH", tmp_path / "hcas.db")
        from backend.app import app

        client = TestClient(app)
        r = client.post(
            "/hcas/projects",
            json={"title": "t", "state_fips": "39", "county_fips": "049"},
            headers=self.HDRS,
        )
        assert r.status_code == 200
        pid = r.json()["project_id"]

        # gate: step 2 before step 1 confirmed → 400
        assert client.post(f"/hcas/projects/{pid}/steps/2/run", headers=self.HDRS).status_code == 400

        draft = client.post(f"/hcas/projects/{pid}/steps/1/run", headers=self.HDRS).json()["ai_draft"]
        r = client.post(
            f"/hcas/projects/{pid}/steps/1/confirm",
            json={"human_final": draft, "edit_seconds": 1.0},
            headers=self.HDRS,
        )
        assert r.status_code == 200
        assert client.post(f"/hcas/projects/{pid}/steps/2/run", headers=self.HDRS).status_code == 200
        assert client.get(f"/hcas/projects/{pid}", headers=self.HDRS).status_code == 200
        assert client.get("/hcas/projects/nope", headers=self.HDRS).status_code == 404


class TestStep2:
    CANNED = _CANNED_LLM

    def test_step2_records_model_and_usage(self, tmp_path):
        pid, db = _project(tmp_path)
        hcas.confirm_step(pid, 1, hcas.run_step(pid, 1, db=db), db=db)
        draft = hcas.run_step(pid, 2, db=db)
        assert draft["factors"][0]["metric"] in hcas.METRICS
        step2 = hcas.get_project(pid, db=db)["steps"][1]
        assert step2["model"] == "claude-opus-4-8"
        assert step2["usage"]["output_tokens"] == 200

    def test_step2_uses_human_edited_area(self, tmp_path, monkeypatch):
        prompts = []

        def fake_llm(system, prompt, schema, model="m"):
            prompts.append(prompt)
            return self.CANNED

        monkeypatch.setattr(hcas, "_llm_structured", fake_llm)
        pid, db = _project(tmp_path)
        draft1 = hcas.run_step(pid, 1, db=db)
        hcas.confirm_step(pid, 1, {**draft1, "place_name": "Edited Place, OH"}, db=db)
        hcas.run_step(pid, 2, db=db)
        assert "Edited Place, OH" in prompts[0]

    def test_step2_llm_failure_leaves_no_draft(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("LLM stopped with refusal")

        monkeypatch.setattr(hcas, "_llm_structured", boom)
        pid, db = _project(tmp_path)
        hcas.confirm_step(pid, 1, hcas.run_step(pid, 1, db=db), db=db)
        with pytest.raises(RuntimeError):
            hcas.run_step(pid, 2, db=db)
        steps = hcas.get_project(pid, db=db)["steps"]
        assert [s["step"] for s in steps] == [1]  # no step-2 row written


class TestStep3:
    def _through_step_2(self, tmp_path):
        pid, db = _project(tmp_path)
        hcas.confirm_step(pid, 1, hcas.run_step(pid, 1, db=db), db=db)
        hcas.confirm_step(pid, 2, hcas.run_step(pid, 2, db=db), db=db)
        return pid, db

    def test_step3_fetches_only_needed_layers(self, tmp_path, monkeypatch):
        called = []

        def make(name):
            def fetch(bbox):
                called.append(name)
                return _fake_gdf(3)
            return fetch

        monkeypatch.setattr(
            hcas, "_layer_fetchers",
            lambda: {n: make(n) for n in ("buildings", "greenspace", "roads", "parking",
                                          "sidewalks", "transit_stops", "facilities")},
        )
        pid, db = self._through_step_2(tmp_path)  # canned step 2 -> greenspace only
        draft = hcas.run_step(pid, 3, db=db)
        assert called == ["greenspace"]
        assert draft["kind"] == "layer_inventory"
        assert draft["layers"] == [{"layer": "greenspace", "feature_count": 3, "crs": "EPSG:5070"}]
        assert len(draft["bbox"]) == 4

    def test_step3_bbox_brackets_centroid(self, tmp_path):
        pid, db = self._through_step_2(tmp_path)
        draft = hcas.run_step(pid, 3, db=db)
        lat, lon = hcas.get_project(pid, db=db)["steps"][0]["ai_draft"]["centroid"]
        west, south, east, north = draft["bbox"]
        assert west < lon < east and south < lat < north


class TestStep4:
    def test_step4_grounds_on_theories_and_confirmed_artifacts(self, tmp_path, monkeypatch):
        captured = {}

        def fake_llm(system, prompt, schema, model="m"):
            captured.update(system=system, prompt=prompt, schema=schema)
            return (
                {
                    "framework": "Greenspace-led health equity assessment.",
                    "theoretical_basis": [
                        {"theory": "WHO Healthy Cities", "application": "settings-based lens"}
                    ],
                    "assumptions": ["OSM greenspace tagging is representative."],
                    "limitations": ["Built-environment metrics only."],
                    "references": [],
                },
                {"input_tokens": 2000, "output_tokens": 400, "cache_read_input_tokens": 0},
            )

        pid, db = _project(tmp_path)
        for n in (1, 2, 3):
            hcas.confirm_step(pid, n, hcas.run_step(pid, n, db=db), db=db)
        monkeypatch.setattr(hcas, "_llm_structured", fake_llm)
        draft = hcas.run_step(pid, 4, db=db)

        assert "WHO Healthy Cities" in captured["system"]          # mock KG present
        assert "greenspace_coverage" in captured["prompt"]         # confirmed step-2 factors
        assert "greenspace: 0 features" in captured["prompt"]      # step-3 inventory
        assert set(captured["schema"]["properties"]) == {
            "framework", "theoretical_basis", "assumptions", "limitations", "references"
        }
        assert draft["framework"].startswith("Greenspace-led")
        step4 = hcas.get_project(pid, db=db)["steps"][3]
        assert step4["model"] == "claude-opus-4-8"

    def test_reference_enum_matches_database(self):
        from backend.assess.references import REFERENCES

        enum = hcas._STEP4_SCHEMA["properties"]["references"]["items"]["enum"]
        db_titles = {r["title"] for refs in REFERENCES.values() for r in refs}
        assert set(enum) == db_titles and enum  # enum-locked citations, non-empty


class TestSteps5to8:
    def test_full_walk_produces_metrics_interventions_and_report(self, tmp_path, monkeypatch):
        # 3 fake greenspace features covering ~0 area -> ratio 0 -> intervention fires
        all_layers = {l for ls in hcas.METRIC_LAYERS.values() for l in ls}
        monkeypatch.setattr(
            hcas, "_layer_fetchers",
            lambda: {n: (lambda bbox: _fake_gdf(3)) for n in all_layers},
        )
        pid, db = _project(tmp_path)
        _walk(pid, db)
        steps = {s["step"]: s for s in hcas.get_project(pid, db=db)["steps"]}

        results = steps[7]["ai_draft"]
        assert set(results["metrics"]) == {"greenspace_coverage_ratio", "greenspace_area_m2"}
        assert results["metrics"]["greenspace_coverage_ratio"] == 0.0
        assert any(iv["category"] == "Greenspace" for iv in results["interventions"])

        md = steps[8]["ai_draft"]["markdown"]
        assert "# Healthy Community Assessment: Franklin County, Ohio, USA" in md
        assert "Greenspace-led health equity assessment." in md
        assert "greenspace access" in md and "greenspace_coverage_ratio" in md
        assert "Bowler, D.E. et al. (2010)" in md  # full citation resolved from title

    def test_step7_respects_human_edited_workflow(self, tmp_path):
        pid, db = _project(tmp_path)
        for n in (1, 2, 3, 4, 5):
            hcas.confirm_step(pid, n, hcas.run_step(pid, n, db=db), db=db)
        plan = hcas.run_step(pid, 6, db=db)
        # human deletes every compute step
        edited = {**plan, "steps": [s for s in plan["steps"] if s["action"] != "compute"]}
        hcas.confirm_step(pid, 6, edited, db=db)
        results = hcas.run_step(pid, 7, db=db)
        assert results["metrics"] == {}
        assert results["interventions"] == []

    def test_step6_workflow_derives_from_confirmed_metrics(self, tmp_path):
        pid, db = _project(tmp_path)
        for n in (1, 2, 3, 4, 5):
            hcas.confirm_step(pid, n, hcas.run_step(pid, n, db=db), db=db)
        plan = hcas.run_step(pid, 6, db=db)
        computes = [s for s in plan["steps"] if s["action"] == "compute"]
        assert computes == [{"action": "compute", "metric": "greenspace_coverage", "layers": ["greenspace"]}]
        assert plan["steps"][-1]["action"] == "assemble_report"
