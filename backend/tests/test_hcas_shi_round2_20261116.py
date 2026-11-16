"""Prof. Shi's 2026-11-13 feedback, round 2 (items #2, #3, ...).

Same approach as test_hcas_shi_feedback_20261113: pin the structure of the single
template, plus real backend behaviour where there is any.
"""
import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "frontend" / "templates" / "hcas.html"
HTML = TEMPLATE.read_text(encoding="utf-8")


def _fn(name: str) -> str:
    m = re.search(r"^(?:async )?function %s\(.*?^}" % re.escape(name), HTML, re.S | re.M)
    assert m, name
    return m.group(0)


def test_item2_rail_click_goes_through_reachable_gate():
    assert 'onclick="railClick(${n})"' in _fn("renderRail")
    assert "selectStep(n)" in _fn("railClick") and "reachable(n)" in _fn("railClick")
    r = _fn("reachable")
    assert 'status!=="confirmed"' in r      # every earlier step must be confirmed
    assert "stepDirty(k)" in r              # item 3: a changed earlier step locks the rest


def test_item3_dirty_compares_screen_with_confirmed_state():
    d = _fn("stepDirty")
    assert 'row.status!=="confirmed"' in d
    assert "step1Changed()" in d            # step 1: form / map selection
    assert "sortKeys(S.edited)" in d and "human_final" in d   # others: edited artifact vs confirmed
    assert "setInterval" in HTML and "railSig" in HTML        # rail re-evaluates live


# ---------------------------------------------------------------- #15 / #4a (backend)
import json
import sqlite3

from backend import hcas

FRANKLIN = ("39", "049")


def test_item15_saved_custom_project_opens_as_block_group(tmp_path):
    db = tmp_path / "h.db"
    pid = hcas.create_project("old", *FRANKLIN, db=db, areal_unit="block_group",
                              area_geoids=["390490001001", "390490001002"], area_name="Short North")
    # make the stored row look like one saved before "Customized" was retired
    with sqlite3.connect(db) as c:
        meta = json.loads(c.execute("SELECT meta_json FROM projects WHERE id=?", (pid,)).fetchone()[0])
        meta["areal_unit"] = "custom"
        c.execute("UPDATE projects SET meta_json=? WHERE id=?", (json.dumps(meta), pid))
    p = hcas.get_project(pid, db=db)
    assert p["meta"]["areal_unit"] == "block_group"
    assert p["meta"]["area_geoids"] == ["390490001001", "390490001002"]
    assert p["meta"]["area_name"] == "Short North"
    # redefining it must not read as a change of unit (which would drop the selection)
    out = hcas.update_project_scope(pid, db=db, areal_unit="block_group", notes="x")
    assert out["scope_changed"] is False


def test_item15_html_has_no_customized_option_and_has_the_hint():
    assert "Customized (community" not in HTML
    assert "The building block of your project area. Results are reported for each one." in HTML
    assert 'community:"block_group"' in HTML          # Find with AI: a neighborhood -> block groups
    assert "suggestBlockGroups" in HTML               # the AI still suggests which block groups


def test_item4a_ai_note_is_saved_with_the_project(tmp_path):
    db = tmp_path / "h.db"
    pid = hcas.create_project("p", *FRANKLIN, db=db, areal_unit="county", ai_note="Franklin County, Ohio — the whole county.")
    assert hcas.get_project(pid, db=db)["meta"]["ai_note"].startswith("Franklin County")
    out = hcas.update_project_scope(pid, db=db, ai_note="changed")
    assert out["scope_changed"] is False                # a note is not a scope change
    assert hcas.get_project(pid, db=db)["meta"]["ai_note"] == "changed"


def test_item4a_form_restore_reads_the_project_after_it_loaded():
    ls = _fn("loadStates")
    assert ls.index("await api(\"/states\")") < ls.index("const m = S.project?.meta")
    assert "if(S.pid && !m) return;" in ls
    assert "fillSeq" in _fn("fillAreaSelects")        # overlapping fills no longer overwrite the county


def test_page_script_parses():
    """A stray newline in a JS string once blanked the whole wizard; parse the page script."""
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not installed")
    script = re.findall(r"<script>(.*?)</script>", HTML, re.S)[-1]
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "page.js"
        f.write_text(script, encoding="utf-8")
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------- #17
def test_item17_interface_note_lists_the_real_options_only():
    note = hcas.study_area_interface_note()
    from backend.gis.profile import AREAL_UNITS

    for k, v in AREAL_UNITS.items():
        if k != "custom":
            assert v["label"] in note
    assert "Customized" not in note.replace("or 'customized'", "")   # only named as a non-option
    assert "NO choice called 'named town/place'" in note
    assert "Find with AI" in note and "Upload study area" in note and "base area" in note


def test_item17_both_step1_chats_get_the_interface_in_their_system_prompt(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(hcas, "_llm_text", lambda system, prompt, model="m": (seen.append(system) or ("ok", {})))
    hcas.chat_intake("where is Gray city in Indiana?", [])
    db = tmp_path / "h.db"
    pid = hcas.create_project("t", *FRANKLIN, db=db, areal_unit="county")
    hcas.chat(pid, 1, "how do I define Gary?", [], db=db)
    hcas.chat(pid, 3, "what is this?", [], db=db)
    assert "THE STEP-1 SCREEN" in seen[0] and "THE STEP-1 SCREEN" in seen[1]
    assert "THE STEP-1 SCREEN" not in seen[2]          # other steps are not about the area form


def test_item17_find_with_ai_knows_a_city_is_a_place(monkeypatch):
    monkeypatch.setattr(hcas, "_llm_structured", lambda *a, **k: (
        {"county": "Lake", "state": "Indiana", "interpretation": "City of Gary", "caveat": "",
         "scale": "place", "zip": "", "town": "Gary"}, {}))
    r = hcas.resolve_area("Gary, Indiana")
    assert r["resolved"] and r["scale"] == "place" and r["town"] == "Gary"
    assert r["county_name"].startswith("Lake")
    assert "place" in hcas._AREA_SCHEMA["properties"]["scale"]["enum"]


def test_item17_units_mostly_inside_uses_more_than_half_the_unit_area():
    import geopandas as gpd
    from shapely.geometry import box
    from backend.gis import places

    # 3 side-by-side squares in lon/lat; the place covers the first fully, the second 40%, the third 80%
    units = gpd.GeoDataFrame({"GEOID": ["a", "b", "c"]},
                             geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1), box(2, 0, 3, 1)], crs="EPSG:4326")
    place = box(-1, -1, 1.4, 2).union(box(2.2, -1, 3.5, 2))
    assert places.units_mostly_inside(units, place) == ["a", "c"]


def test_item17_place_area_selects_block_groups_inside_the_city(monkeypatch):
    import geopandas as gpd
    from shapely.geometry import box
    from backend.gis import places

    bgs = gpd.GeoDataFrame({"GEOID": ["181270001001", "181270001002"], "POP100": [100.0, 200.0]},
                           geometry=[box(0, 0, 1, 1), box(5, 5, 6, 6)], crs="EPSG:4326")
    city = gpd.GeoDataFrame({"GEOID": ["1827000"], "NAME": ["Gary city"], "kind": ["incorporated place"]},
                            geometry=[box(-1, -1, 2, 2)], crs="EPSG:4326")
    monkeypatch.setattr(places, "fetch_place", lambda state, name: city)
    monkeypatch.setattr(hcas, "_unit_features", lambda *a: bgs)
    out = hcas.place_area("Gary", "18", "089")
    assert out["geoids"] == ["181270001001"] and out["place"] == "Gary city"
    assert out["population"] == 100 and out["area_km2"] > 0
    monkeypatch.setattr(places, "fetch_place", lambda state, name: None)
    miss = hcas.place_area("Nowhere", "18", "089")
    assert miss["geoids"] == [] and "Nowhere" in miss["caveat"]


def test_item17_place_name_matching():
    from backend.gis import places

    assert places.base_name("Gary city") == "Gary"
    assert places.base_name("City of Gary") == "Gary"
    assert places.base_name("Bloomington") == "Bloomington"


def test_item17_failure_message_shows_for_every_unit_and_city_path_is_wired():
    sc = _fn("renderScope")
    assert sc.count("AI could not define the specified area") == 2   # county branch and the others
    ra = _fn("resolveArea")
    assert "cityPlace" in ra and "/hcas/place-area" in _fn("suggestBlockGroups")
