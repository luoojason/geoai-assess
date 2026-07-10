"""Tests for the HCAS 8-step gated machine (backend/hcas.py). No network."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import hcas

FRANKLIN = ("39", "049")  # Franklin County, OH

_CANNED_LLM = (
    {
        "factors": [
            {
                "factor": "greenspace access",
                "metric": "greenspace_coverage",
                "rationale": "Green space correlates with better mental and physical health.",
            }
        ],
        "notes": "Air quality not yet measurable by the registry.",
    },
    {"input_tokens": 900, "output_tokens": 200, "cache_read_input_tokens": 0},
)


@pytest.fixture(autouse=True)
def _mock_llm(monkeypatch):
    """No test in this file talks to the real API; step-2 tests override as needed."""
    monkeypatch.setattr(hcas, "_llm_structured", lambda *a, **k: _CANNED_LLM)


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
