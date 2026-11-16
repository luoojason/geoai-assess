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


# ---------------------------------------------------------------- #12 upload a study area
import io
import zipfile

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, box

from backend.gis import upload


def _zip_shapefile(tmp_path, gdf, with_prj=True):
    d = tmp_path / "shp"
    d.mkdir(exist_ok=True)
    gdf.to_file(d / "area.shp")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for f in d.iterdir():
            if f.suffix == ".prj" and not with_prj:
                continue
            z.write(f, f.name)
    return buf.getvalue()


def _geojson_bytes(gdf):
    return gdf.to_json().encode()


def test_item12_reads_a_projected_shapefile_and_returns_wgs84(tmp_path):
    g = gpd.GeoDataFrame({"id": [1]}, geometry=[box(-83.02, 39.97, -82.99, 39.99)], crs="EPSG:4326").to_crs("EPSG:3735")
    out, notes = upload.read_polygon("area.zip", _zip_shapefile(tmp_path, g))
    assert str(out.crs) == "EPSG:4326" and len(out) == 1
    minx, miny, maxx, maxy = out.total_bounds
    assert -83.03 < minx < -83.01 and 39.96 < miny < 39.98 and notes == []


def test_item12_no_prj_in_degrees_is_assumed_wgs84_and_says_so(tmp_path):
    g = gpd.GeoDataFrame({"id": [1]}, geometry=[box(-83.02, 39.97, -82.99, 39.99)], crs="EPSG:4326")
    out, notes = upload.read_polygon("area.zip", _zip_shapefile(tmp_path, g, with_prj=False))
    assert any("assumed to be WGS84" in n for n in notes)
    assert out.total_bounds[0] == pytest.approx(-83.02)


def test_item12_no_prj_in_metres_is_refused(tmp_path):
    g = gpd.GeoDataFrame({"id": [1]}, geometry=[box(1.0e6, 1.0e6, 1.1e6, 1.1e6)], crs="EPSG:3735")
    with pytest.raises(upload.AreaUploadError, match="no coordinate system"):
        upload.read_polygon("area.zip", _zip_shapefile(tmp_path, g, with_prj=False))


def test_item12_geojson_with_several_polygons_is_dissolved_to_one():
    g = gpd.GeoDataFrame({"id": [1, 2]}, geometry=[box(0, 0, 1, 1), box(0.5, 0, 2, 1)], crs="EPSG:4326")
    out, notes = upload.read_polygon("two.geojson", _geojson_bytes(g))
    assert len(out) == 1 and out.geometry.iloc[0].area == pytest.approx(2.0)
    assert any("combined" in n for n in notes)


@pytest.mark.parametrize("name,content,message", [
    ("p.geojson", None, "point features, not polygons"),
    ("l.geojson", None, "linestring features, not polygons"),
    ("e.geojson", b'{"type":"FeatureCollection","features":[]}', "no features"),
    ("x.zip", b"garbage", "not a valid .zip"),
    ("x.txt", b"hello", "Upload a zipped shapefile"),
    ("x.shp", b"hello", "as a .zip"),
    ("x.zip", b"", "empty"),
])
def test_item12_clear_errors_for_bad_files(name, content, message):
    if content is None:
        geom = Point(0, 0) if name == "p.geojson" else LineString([(0, 0), (1, 1)])
        content = _geojson_bytes(gpd.GeoDataFrame({"id": [1]}, geometry=[geom], crs="EPSG:4326"))
    with pytest.raises(upload.AreaUploadError, match=message):
        upload.read_polygon(name, content)


def test_item12_zip_without_shp_and_oversize_files():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "hi")
    with pytest.raises(upload.AreaUploadError, match="no .shp file"):
        upload.read_polygon("x.zip", buf.getvalue())
    with pytest.raises(upload.AreaUploadError, match="limit is 20 MB"):
        upload.read_polygon("big.zip", b"0" * (upload.MAX_BYTES + 1))


def test_item12_county_shares_picks_the_county_holding_most_of_the_polygon():
    poly = gpd.GeoDataFrame({"n": [1]}, geometry=[box(0, 0, 4, 1)], crs="EPSG:4326")
    cty = gpd.GeoDataFrame({"STATE": ["39", "39"], "COUNTY": ["041", "049"], "NAME": ["Delaware", "Franklin"]},
                           geometry=[box(0, 0, 3, 1), box(3, 0, 6, 1)], crs="EPSG:4326")
    shares = upload.county_shares(poly, cty)
    assert [c["name"] for c in shares] == ["Delaware", "Franklin"]
    assert shares[0]["share"] == pytest.approx(0.75, abs=0.01)


def _fake_upload_env(monkeypatch, counties):
    bgs = gpd.GeoDataFrame({"GEOID": ["390490001001", "390490001002", "390490001003"], "POP100": [1.0, 2.0, 3.0]},
                           geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1), box(5, 5, 6, 6)], crs="EPSG:4326")
    monkeypatch.setattr(upload, "counties_for_polygon", lambda gdf: counties)
    monkeypatch.setattr(hcas, "_unit_features", lambda *a: bgs)


def test_item12_upload_selects_units_mostly_inside_and_sets_the_county(monkeypatch):
    _fake_upload_env(monkeypatch, [{"state_fips": "39", "county_fips": "049", "name": "Franklin", "share": 1.0}])
    g = gpd.GeoDataFrame({"id": [1]}, geometry=[box(-0.5, -0.5, 1.2, 1.5)], crs="EPSG:4326")
    r = hcas.upload_study_area("area.geojson", _geojson_bytes(g))
    assert (r["state_fips"], r["county_fips"]) == ("39", "049")
    assert r["geoids"] == ["390490001001"]          # the second block group is only 20% inside
    assert r["county_name"].startswith("Franklin") and r["state_name"] == "Ohio"
    assert r["polygon"]["geometry"]["type"] == "Polygon" and r["caveat"] == ""


def test_item12_polygon_over_two_counties_says_so_and_uses_the_larger(monkeypatch):
    _fake_upload_env(monkeypatch, [{"state_fips": "39", "county_fips": "049", "name": "Franklin", "share": 0.62},
                                   {"state_fips": "39", "county_fips": "041", "name": "Delaware", "share": 0.38}])
    g = gpd.GeoDataFrame({"id": [1]}, geometry=[box(0, 0, 1, 1)], crs="EPSG:4326")
    r = hcas.upload_study_area("x.geojson", _geojson_bytes(g))
    assert r["county_fips"] == "049"
    assert "crosses county lines" in r["caveat"] and "Franklin 62%" in r["caveat"] and "Delaware 38%" in r["caveat"]


def test_item12_polygon_smaller_than_any_unit_selects_nothing_and_says_so(monkeypatch):
    _fake_upload_env(monkeypatch, [{"state_fips": "39", "county_fips": "049", "name": "Franklin", "share": 1.0}])
    g = gpd.GeoDataFrame({"id": [1]}, geometry=[box(0.4, 0.4, 0.5, 0.5)], crs="EPSG:4326")
    r = hcas.upload_study_area("tiny.geojson", _geojson_bytes(g))
    assert r["geoids"] == [] and "nothing was selected" in r["caveat"]


def test_item12_outline_is_saved_with_the_project_and_drawn_on_the_maps(tmp_path):
    db = tmp_path / "h.db"
    feat = {"type": "Feature", "properties": {"name": "my area"},
            "geometry": {"type": "Polygon", "coordinates": [[[-83, 40], [-82.9, 40], [-82.9, 40.1], [-83, 40]]]}}
    pid = hcas.create_project("p", *FRANKLIN, db=db, areal_unit="block_group",
                              area_geoids=["390490001001"], area_polygon=feat)
    meta = hcas.get_project(pid, db=db)["meta"]
    assert meta["area_polygon"]["type"] == "Polygon" and meta["area_polygon_name"] == "my area"
    # absent keeps it, null removes it
    hcas.update_project_scope(pid, db=db, notes="n")
    assert "area_polygon" in hcas.get_project(pid, db=db)["meta"]
    hcas.update_project_scope(pid, db=db, area_polygon=None)
    assert "area_polygon" not in hcas.get_project(pid, db=db)["meta"]
    with pytest.raises(ValueError, match="polygon"):
        hcas.create_project("p", *FRANKLIN, db=db, area_polygon={"type": "Point", "coordinates": [0, 0]})
    # the renderers draw it as a separate dashed outline
    from backend.assess.renderer import render_base_map

    sq = gpd.GeoDataFrame({"n": [1]}, geometry=[box(-83, 40, -82.9, 40.1)], crs="EPSG:4326")
    html = render_base_map(sq, "x", outline=sq)
    assert "Uploaded study area" in html and "10 7" in html
    assert "Uploaded study area" not in render_base_map(sq, "x")


def test_item12_upload_endpoint_returns_plain_errors():
    from fastapi.testclient import TestClient
    from backend.app import app

    c = TestClient(app)
    h = {"X-Requested-With": "geoai"}
    r = c.post("/hcas/upload-area?filename=p.geojson", content=_geojson_bytes(
        gpd.GeoDataFrame({"id": [1]}, geometry=[Point(0, 0)], crs="EPSG:4326")), headers=h)
    assert r.status_code == 400 and "not polygons" in r.json()["detail"]
    r = c.post("/hcas/upload-area?filename=big.zip", content=b"0" * (upload.MAX_BYTES + 1), headers=h)
    assert r.status_code == 413 and "20 MB" in r.json()["detail"]


def test_item12_page_has_the_button_the_outline_and_saves_the_polygon():
    assert "Upload study area" in _fn("areaEditorHtml")
    assert "/hcas/upload-area" in _fn("uploadArea")
    assert "dashArray" in _fn("drawAreaMap") and "a.polygon" in _fn("drawAreaMap")
    assert "area_polygon" in _fn("areaPayload")
