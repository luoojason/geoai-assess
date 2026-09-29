"""Fixes from the 2026-09-29 QA pass on the hosted site (two testers).

Each test pins one backend fix. None of them touch the network: the step
runners are stubbed wherever a run happens.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from backend import hcas

H = {"X-Requested-With": "geoai"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(hcas, "DB_PATH", tmp_path / "hcas.db")
    from backend.app import app

    return TestClient(app)


def _new_project(client):
    return client.post(
        "/hcas/projects",
        json={"title": "t", "state_fips": "39", "county_fips": "049"},
        headers=H,
    ).json()["project_id"]


# --- bug 4 / 8: a reloaded page must find a running re-run and its clock -----

class TestRunningProgress:
    def test_idle_poll_is_unchanged(self, client):
        pid = _new_project(client)
        r = client.get(f"/hcas/projects/{pid}/steps/4/progress", headers=H)
        assert r.json() == {"pct": 0, "label": ""}

    def test_running_step_reports_running_and_elapsed(self, client):
        pid = _new_project(client)
        hcas.mark_started(pid, 4)
        try:
            # A step that has not reported any percentage yet still says it runs.
            b = client.get(f"/hcas/projects/{pid}/steps/4/progress", headers=H).json()
            assert b["running"] is True and b["pct"] == 0
            assert 0 <= b["elapsed_s"] < 5
        finally:
            hcas.clear_progress(pid, 4)
        b = client.get(f"/hcas/projects/{pid}/steps/4/progress", headers=H).json()
        assert b == {"pct": 0, "label": ""}

    def test_elapsed_comes_from_the_server_start_time(self, client, monkeypatch):
        pid = _new_project(client)
        hcas.mark_started(pid, 2)
        try:
            with hcas._progress_lock:
                hcas._progress_started[(pid, 2)] = time.time() - 125
            b = client.get(f"/hcas/projects/{pid}/steps/2/progress", headers=H).json()
            assert 124 <= b["elapsed_s"] <= 130
        finally:
            hcas.clear_progress(pid, 2)

    def test_run_step_marks_every_step_running_then_clears(self, tmp_path, monkeypatch):
        db = tmp_path / "hcas.db"
        monkeypatch.setattr(hcas, "DB_PATH", db)
        pid = hcas.create_project("t", "39", "049", db=db)
        seen = {}

        def fake_draft(project, step):
            seen["started"] = hcas.get_started(project["id"], step)
            return {"place_name": "x"}, "m", {}

        monkeypatch.setattr(hcas, "_draft", fake_draft)
        hcas.run_step(pid, 1, db=db)
        assert seen["started"] is not None
        assert hcas.get_started(pid, 1) is None


# --- bug 2: the step assistant talks about the page, not the data ------------

class TestChatPlainWords:
    def test_chat_prompt_forbids_data_editing_and_ids(self, tmp_path, monkeypatch):
        db = tmp_path / "hcas.db"
        pid = hcas.create_project("t", "39", "049", db=db)
        seen = {}

        def fake_llm_text(system, prompt, model="m"):
            seen["system"], seen["prompt"] = system, prompt
            return "ok", {}

        monkeypatch.setattr(hcas, "_llm_text", fake_llm_text)
        hcas.chat(pid, 2, "how do I add a factor?", db=db)
        sys_ = seen["system"]
        # The old wording that sent testers to "the factors array".
        assert "which field to edit" not in sys_
        assert "Never tell the analyst to edit data" in sys_
        assert "ids" in sys_ and "JSON" in sys_
        # It points at controls the page really has.
        assert "Apply change" in sys_ and "Re-run" in sys_
        # The same plain-words rule the step prompts carry.
        assert hcas._PLAIN_WORDS_RULE in sys_

    def test_chat_buttons_named_in_prompt_exist_on_the_page(self):
        from pathlib import Path

        html = (Path(hcas.__file__).parent.parent / "frontend" / "templates" / "hcas.html").read_text(encoding="utf-8")
        for label in ("Apply change", "Change the list", "Re-run", "Confirm", "Back"):
            assert label in html, label


# --- bug 3: the report keeps knowledgebase references -------------------------

class TestReportReferences:
    @staticmethod
    def _cited():
        from backend.assess.references import REFERENCES

        return {r["title"]: r for refs in REFERENCES.values() for r in refs}

    def test_kb_references_are_listed_with_grounding_note(self):
        cited = self._cited()
        builtin = next(iter(cited))
        kb_title = "Walkability and obesity in Shanghai — Results"
        framework = {"knowledgebase": {"version": "v2", "method": "local",
                                       "source_documents": [kb_title, "Other paper"]}}
        lines = hcas._report_references(framework, [builtin, kb_title, "Invented title"], cited)
        text = "\n".join(lines)
        assert "grounded in the project knowledgebase (2 sources)" in text
        assert builtin in text and kb_title in text
        # A title neither built in nor returned by the corpus is still dropped.
        assert "Invented title" not in text
        assert sum(1 for l in lines if l.startswith("- ")) == 2

    def test_single_source_is_singular(self):
        lines = hcas._report_references({"knowledgebase": {"source_documents": ["A"]}}, ["A"], {})
        assert "(1 source)" in lines[0]

    def test_ungrounded_report_says_nothing_about_grounding(self):
        cited = self._cited()
        builtin = next(iter(cited))
        lines = hcas._report_references({}, [builtin], cited)
        assert len(lines) == 1 and "knowledgebase" not in lines[0]


# --- bug 5: a hosted visitor is not told to edit the server's .env -----------

class TestCensusKeyMessage:
    @staticmethod
    def _profile(monkeypatch):
        from backend.gis import profile as p

        monkeypatch.setattr(p, "_county_attributes", lambda s, c: {
            "NAME": "Grafton County", "AREALAND": "4432000000", "AREAWATER": "70000000"})
        return p.county_profile("33", "009", population=91118, census_api_key=None)

    def test_hosted_message_is_plain_and_names_the_administrator(self, monkeypatch):
        monkeypatch.setenv("HCAS_HOSTED", "1")
        msg = self._profile(monkeypatch)["unavailable"]["acs"]
        assert "site administrator" in msg and "income" in msg
        for local_only in (".env", "START-HCAS", "CENSUS_API_KEY"):
            assert local_only not in msg

    def test_local_install_keeps_the_setup_hint(self, monkeypatch):
        monkeypatch.delenv("HCAS_HOSTED", raising=False)
        msg = self._profile(monkeypatch)["unavailable"]["acs"]
        assert "CENSUS_API_KEY" in msg and "START-HCAS" in msg

    @pytest.mark.parametrize("val,expected", [("1", True), ("true", True), ("", False), ("0", False)])
    def test_is_hosted_flag(self, monkeypatch, val, expected):
        from backend.config import is_hosted

        monkeypatch.setenv("HCAS_HOSTED", val)
        assert is_hosted() is expected


# --- bug 6: the base column is labelled as the base area ----------------------

class TestBaseAreaLabels:
    def test_step5_base_database_says_base_area(self):
        inv = {"bbox": [0, 0, 1, 1], "layers": [],
               "study_area": {"kind": "units", "name": "Project area", "units": 19, "area_km2": 3.4},
               "base": {"bbox": [0, 0, 1, 1], "layers": [],
                        "study_area": {"kind": "units", "name": "Project area",
                                       "units": 19, "area_km2": 3.4}}}
        project = {"steps": [{"step": 3, "status": "confirmed", "human_final": inv}]}
        out, _, _ = hcas._run_step_5(project)
        assert out["study_area"].startswith("Project area")
        assert out["base"]["study_area"].startswith("Base area")

    def test_whole_county_base_map_is_captioned_base_area(self, monkeypatch):
        seen = []
        monkeypatch.setattr(hcas, "lookup_county", lambda s, c: {"name": "x"})
        monkeypatch.setattr(hcas, "_county_geometry", lambda s, c: _NonEmpty())
        monkeypatch.setattr(hcas, "_basemap_cache", {})
        from backend.assess import renderer

        def fake_render(geom, place, subtitle="Study area · full county boundary"):
            seen.append(subtitle)
            return subtitle

        monkeypatch.setattr(renderer, "render_base_map", fake_render)
        assert hcas.basemap_for_county("33", "009") == "Study area · full county boundary"
        assert hcas.basemap_for_county("33", "009", subtitle="Base area · full county boundary") \
            == "Base area · full county boundary"


class _NonEmpty:
    empty = False


# --- bug 8: a unit is never said twice ----------------------------------------

class TestUnitSuffix:
    def test_social_vulnerability_label_is_deduplicated(self):
        label = hcas._result_label("social_vulnerability")
        assert label == "Social vulnerability (percentile, 0–1)"
        assert label.count("percentile") == 1

    @pytest.mark.parametrize("label,unit,expected", [
        ("Poverty", "%", "Poverty (%)"),
        ("Injury deaths", "per 100,000 people", "Injury deaths (per 100,000 people)"),
        ("Median AQI (index)", "index", "Median AQI (index)"),
        ("Primary care physicians", None, "Primary care physicians"),
    ])
    def test_with_unit(self, label, unit, expected):
        assert hcas.with_unit(label, unit) == expected


# --- bug 8: layer ids never reach the page -----------------------------------

def test_frontend_layer_names_match_layer_meta():
    import re
    from pathlib import Path

    html = (Path(hcas.__file__).parent.parent / "frontend" / "templates" / "hcas.html").read_text(encoding="utf-8")
    block = re.search(r"const LAYER_NAMES = \{(.*?)\};", html, re.S).group(1)
    names = dict(re.findall(r'(\w+):"([^"]+)"', block))
    assert names == {k: v[0] for k, v in hcas.LAYER_META.items()}
