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
