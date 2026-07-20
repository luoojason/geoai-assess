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
    from backend.assess import places

    monkeypatch.setattr(
        places,
        "fetch_health_outcomes",
        lambda s, c: {
            "source": "CDC PLACES (test)",
            "measures": [
                {"id": "OBESITY", "label": "Obesity", "county_pct": 30.1, "us_pct": 33.3},
                {"id": "LPA", "label": "No leisure-time physical activity",
                 "county_pct": 25.0, "us_pct": 23.7},
            ],
        },
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


class TestMapAndAudit:
    def test_step7_stores_map_and_flags_availability(self, tmp_path):
        pid, db = _project(tmp_path)
        _walk(pid, db, through=7)
        step7 = hcas.get_project(pid, db=db)["steps"][6]
        assert step7["ai_draft"]["map_available"] is True
        assert "_map_html" not in step7["ai_draft"]  # kept out of the artifact
        html = hcas.get_map(pid, db=db)
        assert "Assessment Metrics" in html

    def test_get_map_before_step7_raises(self, tmp_path):
        pid, db = _project(tmp_path)
        with pytest.raises(KeyError):
            hcas.get_map(pid, db=db)

    def test_audit_csv_covers_all_steps(self, tmp_path):
        pid, db = _project(tmp_path)
        _walk(pid, db)
        csv_text = hcas.export_audit_csv(db=db)
        lines = csv_text.strip().splitlines()
        assert len(lines) == 9  # header + 8 steps
        assert lines[0].startswith("project_id,title,step,status")
        assert f"{pid},test,2,confirmed" in csv_text
        assert "claude-opus-4-8" in csv_text  # model recorded for LLM steps


class TestLandUseMix:
    def test_even_two_way_mix_scores_one(self):
        import geopandas as gpd
        from shapely.geometry import box

        from backend.assess import compute_land_use_mix

        landuse = gpd.GeoDataFrame(
            {"landuse": ["residential", "commercial"]},
            geometry=[box(0, 0, 100, 100), box(100, 0, 200, 100)],
            crs="EPSG:5070",
        )
        study = gpd.GeoDataFrame(geometry=[box(0, 0, 200, 100)], crs="EPSG:5070")
        result = compute_land_use_mix(landuse, study)
        assert result == {"land_use_mix": 1.0, "land_use_categories": 2}

    def test_single_use_scores_zero(self):
        import geopandas as gpd
        from shapely.geometry import box

        from backend.assess import compute_land_use_mix

        landuse = gpd.GeoDataFrame(
            {"landuse": ["residential"]}, geometry=[box(0, 0, 100, 100)], crs="EPSG:5070"
        )
        study = gpd.GeoDataFrame(geometry=[box(0, 0, 200, 100)], crs="EPSG:5070")
        assert compute_land_use_mix(landuse, study)["land_use_mix"] == 0.0

    def test_empty_returns_zeros(self):
        from backend.assess import compute_land_use_mix

        study = _fake_gdf(1)
        assert compute_land_use_mix(_fake_gdf(), study) == {
            "land_use_mix": 0.0,
            "land_use_categories": 0,
        }

    def test_registry_wiring(self):
        assert "land_use_mix" in hcas.METRICS
        assert hcas.METRIC_LAYERS["land_use_mix"] == ["landuse"]


class TestHealthOutcomes:
    def test_step3_carries_outcomes_and_step8_reports_them(self, tmp_path):
        pid, db = _project(tmp_path)
        _walk(pid, db, through=8)
        proj = hcas.get_project(pid, db=db)
        s3 = next(s for s in proj["steps"] if s["step"] == 3)["ai_draft"]
        assert s3["health_outcomes"]["measures"][0]["id"] == "OBESITY"
        md = next(s for s in proj["steps"] if s["step"] == 8)["ai_draft"]["markdown"]
        assert "## Health outcomes (county-level)" in md
        assert "| Obesity | 30.1% | 33.3% |" in md
        # LPA (25.0) exceeds the US benchmark (23.7) and must be flagged
        assert "25.0% **(above US)** | 23.7%" in md

    def test_cdc_outage_degrades_gracefully(self, tmp_path, monkeypatch):
        from backend.assess import places

        def boom(s, c):
            raise RuntimeError("CDC down")

        monkeypatch.setattr(places, "fetch_health_outcomes", boom)
        pid, db = _project(tmp_path)
        _walk(pid, db, through=8)
        proj = hcas.get_project(pid, db=db)
        s3 = next(s for s in proj["steps"] if s["step"] == 3)["ai_draft"]
        assert s3["health_outcomes"]["measures"] == []
        assert "unavailable" in s3["health_outcomes"]["note"]
        md = next(s for s in proj["steps"] if s["step"] == 8)["ai_draft"]["markdown"]
        assert "unavailable this run" in md


class TestAutoMode:
    def test_run_auto_walks_all_8_with_zero_edits(self, tmp_path):
        db = tmp_path / "hcas.db"
        pid = hcas.create_project("auto arm", *FRANKLIN, gate_mode="auto", db=db)
        proj = hcas.run_auto(pid, db=db)
        assert [s["status"] for s in proj["steps"]] == ["confirmed"] * 8
        assert all(s["edit_seconds"] == 0.0 for s in proj["steps"])
        assert all(s["edit_delta"] is None for s in proj["steps"])

    def test_run_auto_refuses_gated_projects(self, tmp_path):
        pid, db = _project(tmp_path)  # default gate_mode='gated'
        with pytest.raises(ValueError, match="gate_mode='auto'"):
            hcas.run_auto(pid, db=db)

    def test_run_auto_resumes_after_partial_walk(self, tmp_path):
        db = tmp_path / "hcas.db"
        pid = hcas.create_project("auto arm", *FRANKLIN, gate_mode="auto", db=db)
        _walk(pid, db, through=3)
        proj = hcas.run_auto(pid, db=db)
        assert [s["status"] for s in proj["steps"]] == ["confirmed"] * 8


class TestChat:
    """The live per-step assistant is a side-channel: it must never write an
    artifact row, so the audit tuples stay 'LLM at steps 2 & 4 only'."""

    def test_chat_grounds_on_step_context_and_writes_nothing(self, tmp_path, monkeypatch):
        captured = {}

        def fake_text(system, prompt, model="m"):
            captured.update(system=system, prompt=prompt)
            return "Greenspace access is your only confirmed factor.", {
                "input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 0}

        monkeypatch.setattr(hcas, "_llm_text", fake_text)
        pid, db = _project(tmp_path)
        hcas.confirm_step(pid, 1, hcas.run_step(pid, 1, db=db), db=db)
        hcas.run_step(pid, 2, db=db)  # step 2 drafted (canned)

        before = hcas.get_project(pid, db=db)["steps"]
        out = hcas.chat(pid, 2, "What factors did you pick?", db=db)
        after = hcas.get_project(pid, db=db)["steps"]

        assert "Greenspace" in out["reply"]
        assert "greenspace_coverage" in captured["prompt"]      # artifact in context
        assert "Factor & metric selection" in captured["prompt"]  # step title in context
        assert before == after  # side-channel: audit rows untouched

    def test_chat_before_run_has_no_artifact(self, tmp_path, monkeypatch):
        seen = {}

        def fake_text(system, prompt, model="m"):
            seen["prompt"] = prompt
            return "Run the step first.", {"input_tokens": 1, "output_tokens": 1,
                                           "cache_read_input_tokens": 0}

        monkeypatch.setattr(hcas, "_llm_text", fake_text)
        pid, db = _project(tmp_path)
        hcas.chat(pid, 1, "what now?", db=db)
        assert "has not been run yet" in seen["prompt"]

    def test_chat_endpoint_ok_and_404(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hcas, "DB_PATH", tmp_path / "hcas.db")
        monkeypatch.setattr(
            hcas, "_llm_text",
            lambda system, prompt, model="m": ("hi", {
                "input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0}),
        )
        from backend.app import app

        client = TestClient(app)
        pid = client.post(
            "/hcas/projects",
            json={"title": "t", "state_fips": "39", "county_fips": "049"},
            headers={"X-Requested-With": "geoai"},
        ).json()["project_id"]
        r = client.post(
            f"/hcas/projects/{pid}/chat",
            json={"message": "hello", "step": 1},
            headers={"X-Requested-With": "geoai"},
        )
        assert r.status_code == 200 and r.json()["reply"] == "hi"
        assert client.post(
            "/hcas/projects/nope/chat",
            json={"message": "hi", "step": 1},
            headers={"X-Requested-With": "geoai"},
        ).status_code == 404


class TestUpstreamErrorMapping:
    """Prof. Shi's field test (2026-07-18): every LLM failure must surface as a
    plain-language 502 detail — never a blanket 502 or an unhandled 500 — and
    the step-runner and chat paths must use the same words."""

    def _fake_response(self, status, text="denied"):
        class R:
            status_code = status

            def __init__(self):
                self.text = text

            def json(self):
                return {"error": {"code": status, "message": text}}

        return R()

    def test_401_names_the_key(self, monkeypatch):
        import requests

        monkeypatch.setattr(requests, "post", lambda *a, **k: self._fake_response(401))
        with pytest.raises(RuntimeError, match="rejected the API key"):
            hcas._post_openrouter({}, "sk-or-bad", timeout=5)

    def test_402_names_credit(self, monkeypatch):
        import requests

        monkeypatch.setattr(requests, "post", lambda *a, **k: self._fake_response(402))
        with pytest.raises(RuntimeError, match="out of credit"):
            hcas._post_openrouter({}, "sk-or-bad", timeout=5)

    def test_unreachable_names_proxy(self, monkeypatch):
        import requests

        def boom(*a, **k):
            raise requests.exceptions.ProxyError("dead proxy 127.0.0.1:7890")

        monkeypatch.setattr(requests, "post", boom)
        with pytest.raises(RuntimeError, match="Cannot reach the AI provider.*proxy"):
            hcas._post_openrouter({}, "sk-or-x", timeout=5)

    def test_timeout_names_timeout(self, monkeypatch):
        import requests

        def boom(*a, **k):
            raise requests.exceptions.ConnectTimeout("slow")

        monkeypatch.setattr(requests, "post", boom)
        with pytest.raises(RuntimeError, match="did not respond in time"):
            hcas._post_openrouter({}, "sk-or-x", timeout=5)

    def test_error_envelope_in_200_body_maps_too(self, monkeypatch):
        import requests

        class R:
            status_code = 200
            text = ""

            def json(self):
                return {"error": {"code": 401, "message": "bad key"}}

        monkeypatch.setattr(requests, "post", lambda *a, **k: R())
        with pytest.raises(RuntimeError, match="rejected the API key"):
            hcas._post_openrouter({}, "sk-or-x", timeout=5)

    def _no_key_env(self, monkeypatch):
        from backend.config import settings

        monkeypatch.setattr(settings, "anthropic_api_key", None)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def test_chat_without_key_or_cli_is_502_not_crash(self, tmp_path, monkeypatch):
        """The exact Windows failure: empty key + no claude CLI used to raise
        FileNotFoundError -> 'Exception in ASGI application'."""
        import shutil

        self._no_key_env(monkeypatch)
        monkeypatch.setattr(shutil, "which", lambda name: None)
        monkeypatch.setattr(hcas, "DB_PATH", tmp_path / "hcas.db")
        from backend.app import app

        client = TestClient(app)
        pid = client.post(
            "/hcas/projects",
            json={"title": "t", "state_fips": "39", "county_fips": "049"},
            headers={"X-Requested-With": "geoai"},
        ).json()["project_id"]
        r = client.post(
            f"/hcas/projects/{pid}/chat",
            json={"message": "hello", "step": 1},
            headers={"X-Requested-With": "geoai"},
        )
        assert r.status_code == 502
        assert "No API key is configured" in r.json()["detail"]

    def test_step_run_upstream_failure_shares_the_same_words(self, tmp_path, monkeypatch):
        """End-to-end: OpenRouter unreachable during step 2 -> 502 whose detail
        is the same friendly copy the chat path uses."""
        import requests

        self._no_key_env(monkeypatch)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

        def boom(*a, **k):
            raise requests.exceptions.ConnectionError("refused")

        monkeypatch.setattr(requests, "post", boom)
        monkeypatch.setattr(hcas, "DB_PATH", tmp_path / "hcas.db")
        # the autouse fixture cans _llm_structured; restore the real one
        monkeypatch.setattr(hcas, "_llm_structured", _REAL_LLM_STRUCTURED)
        from backend.app import app

        client = TestClient(app)
        h = {"X-Requested-With": "geoai"}
        pid = client.post(
            "/hcas/projects",
            json={"title": "t", "state_fips": "39", "county_fips": "049"},
            headers=h,
        ).json()["project_id"]
        draft = client.post(f"/hcas/projects/{pid}/steps/1/run", headers=h).json()["ai_draft"]
        client.post(f"/hcas/projects/{pid}/steps/1/confirm",
                    json={"human_final": draft}, headers=h)
        r = client.post(f"/hcas/projects/{pid}/steps/2/run", headers=h)
        assert r.status_code == 502
        assert "Cannot reach the AI provider" in r.json()["detail"]

    def test_chat_unexpected_exception_is_502_with_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hcas, "DB_PATH", tmp_path / "hcas.db")

        def boom(pid, step, message, history=None):
            raise TypeError("surprise")

        monkeypatch.setattr(hcas, "chat", boom)
        from backend.app import app

        client = TestClient(app)
        h = {"X-Requested-With": "geoai"}
        pid = client.post(
            "/hcas/projects",
            json={"title": "t", "state_fips": "39", "county_fips": "049"},
            headers=h,
        ).json()["project_id"]
        r = client.post(f"/hcas/projects/{pid}/chat",
                        json={"message": "hi", "step": 1}, headers=h)
        assert r.status_code == 502 and "TypeError" in r.json()["detail"]

    def test_favicon_served(self):
        from backend.app import app

        r = TestClient(app).get("/favicon.ico")
        assert r.status_code == 200 and "image" in r.headers["content-type"]


_REAL_LLM_STRUCTURED = hcas._llm_structured


class TestFetchErrorSurfacing:
    """Prof. Shi's 2026-07-20 report: a blocked network produced silent empty
    layers and a featureless map. Fetch failures must surface in the artifacts
    with plain-language reasons, and Overpass must fail over to its mirror."""

    def _failing_greenspace(self):
        from backend.assess.netdiag import record_fetch_error

        def fetch_greenspace(bbox):
            record_fetch_error(
                "fetch_greenspace", OSError("[Errno 11001] getaddrinfo failed"))
            return _fake_gdf()

        return fetch_greenspace

    def test_step3_carries_per_layer_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            hcas, "_layer_fetchers", lambda: {"greenspace": self._failing_greenspace()})
        pid, db = _project(tmp_path)
        _walk(pid, db, through=2)
        draft = hcas.run_step(pid, 3, db=db)
        (layer,) = draft["layers"]
        assert layer["layer"] == "greenspace"
        assert "DNS lookup failed" in layer["error"]

    def test_step7_reports_fetch_errors_and_empty_map_note(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            hcas, "_layer_fetchers", lambda: {"greenspace": self._failing_greenspace()})
        pid, db = _project(tmp_path)
        _walk(pid, db, through=6)
        draft = hcas.run_step(pid, 7, db=db)
        assert "DNS lookup failed" in draft["fetch_errors"]["greenspace"]
        assert "empty" in draft["map_note"]

    def test_healthy_step3_has_no_error_keys(self, tmp_path):
        pid, db = _project(tmp_path)
        _walk(pid, db, through=2)
        draft = hcas.run_step(pid, 3, db=db)
        assert all("error" not in l for l in draft["layers"])

    def test_netdiag_classifies_dns_and_unreachable(self):
        from backend.assess.netdiag import friendly_net_reason

        assert "DNS lookup failed" in friendly_net_reason(
            OSError("HTTPSConnectionPool(host='data.cdc.gov'): getaddrinfo failed"))
        assert "unreachable" in friendly_net_reason(
            OSError("[WinError 10065] A socket operation was attempted to an unreachable host"))

    def test_overpass_failover_switches_to_mirror_once(self, monkeypatch):
        import osmnx as ox

        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_failed_over", False)
        old_url = ox.settings.overpass_url
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("[WinError 10065] unreachable host")
            return "ok"

        try:
            assert ol._with_overpass_failover(flaky) == "ok"
            assert ox.settings.overpass_url == ol._OVERPASS_MIRROR
        finally:
            ox.settings.overpass_url = old_url
            ol._failed_over = False
        assert calls["n"] == 2

    def test_overpass_empty_result_is_not_failover(self, monkeypatch):
        from osmnx._errors import InsufficientResponseError

        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_failed_over", False)

        def empty():
            raise InsufficientResponseError("No matching features")

        with pytest.raises(InsufficientResponseError):
            ol._with_overpass_failover(empty)
        assert ol._failed_over is False
