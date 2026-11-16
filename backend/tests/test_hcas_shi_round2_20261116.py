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


# ---------------------------------------------------------------- step 6 flowchart
def _flowchart(workflow: dict) -> str:
    """Run the page's own flowchartSvg() under node with the page's helpers stubbed."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    m = re.search(r"^function flowWrap\(.*?^function flowchartSvg\(.*?^}", HTML, re.S | re.M)
    assert m
    import tempfile

    js = ("const esc = s => String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/\"/g,'&quot;');"
          "const layerName = x => 'L-' + x; const measureLabel = x => 'M-' + x;"
          + m.group(0) + "console.log(flowchartSvg(JSON.parse(require('fs').readFileSync(0, 'utf8'))));")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "flow.js"
        f.write_text(js, encoding="utf-8")
        r = subprocess.run([node, str(f)], input=json.dumps(workflow), capture_output=True, text=True,
                           encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout


WF = {"kind": "workflow", "steps": [
    {"action": "compute", "metric": "greenspace_coverage_ratio", "layers": ["greenspace"]},
    {"action": "compute", "metric": "pedestrian_density", "layers": ["sidewalks", "roads"]},
    {"action": "compute", "metric": "obesity", "source": "CDC PLACES"},
    {"action": "awaiting_data", "factor": "Air quality", "data_needed": "monitor readings"},
    {"action": "compare_with_base"},
    {"action": "generate_interventions"},
    {"action": "assemble_report"},
]}


def test_step6_flowchart_has_the_four_columns_and_every_node():
    svg = _flowchart(WF)
    assert svg.startswith("<svg") and "viewBox" in svg and 'width="100%"' in svg
    for head in ("INPUT DATA", "PROCESSING", "METRICS", "OUTPUTS"):
        assert head in svg
    # layers, source, three operations, three metrics, one awaiting-data box and three outputs
    assert svg.count("<rect") == 3 + 1 + 3 + 3 + 1 + 3
    for label in ("L-greenspace", "L-sidewalks", "L-roads", "CDC PLACES", "M-greenspace_coverage_ratio",
                  "Air quality", "interventions", "base area", "report"):
        assert label in svg
    assert 'stroke-dasharray="6 4"' in svg                 # awaiting data is drawn dashed
    # one arrow per edge: layer->op (4), op->metric (3), metric->interventions (3), ->compare (3), compare->report, interventions->report
    assert svg.count("marker-end") == 4 + 3 + 3 + 3 + 2


def test_step6_flowchart_follows_the_artifact_and_boxes_never_overlap():
    short = _flowchart({"steps": [{"action": "compute", "metric": "a", "layers": ["x"]},
                                  {"action": "generate_interventions"}, {"action": "assemble_report"}]})
    assert "L-x" in short and "base area" not in short and "Air quality" not in short
    svg = _flowchart(WF)
    rects = [tuple(float(v) for v in m) for m in re.findall(
        r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"', svg)]
    for i, (x1, y1, w1, h1) in enumerate(rects):
        for x2, y2, w2, h2 in rects[i + 1:]:
            apart = x1 + w1 <= x2 or x2 + w2 <= x1 or y1 + h1 <= y2 or y2 + h2 <= y1
            assert apart, ((x1, y1), (x2, y2))
    width = float(re.search(r'viewBox="0 0 ([\d.]+) ', svg).group(1))
    assert all(x + w <= width for x, y, w, h in rects)      # nothing runs off the drawing


def test_step6_flowchart_is_in_the_step6_page_and_the_report():
    assert "flowchartSvg(a)" in _fn("viewWorkflow")
    vr = _fn("viewReport")
    assert "flowchartSvg(" in vr and "included in the report" in vr
    assert "flowBlock" in _fn("downloadHtmlReport") and "flowchartSvg(" in _fn("downloadHtmlReport")
    assert "https://" not in re.search(r"^function flowchartSvg\(.*?^}", HTML, re.S | re.M).group(0).replace(
        "http://www.w3.org/2000/svg", "")        # no library, no CDN


def test_step6_workflow_is_a_section_of_the_markdown_report():
    project = {"steps": [{"step": 6, "ai_draft": WF}]}
    text = "\n".join(hcas._workflow_lines(project))
    assert "## Workflow" in text and "Compute `greenspace_coverage_ratio`" in text
    assert "Awaiting data: Air quality (needs monitor readings)" in text
    assert "Compare every result with the base area" in text
    assert hcas._workflow_lines({"steps": []}) == []


# ---------------------------------------------------------------- step 5: Download and save the database?
def _db_project(tmp_path, monkeypatch):
    """A project with two cached layers and a user-uploaded layer, in an isolated cache dir."""
    from backend import hcas_db  # noqa: F401  (imported so a syntax error fails here)

    monkeypatch.setattr(hcas, "LAYER_CACHE_DIR", tmp_path / "cache")
    db = tmp_path / "h.db"
    pid = hcas.create_project("Pkg test", *FRANKLIN, db=db, areal_unit="block_group",
                              area_geoids=["390490001001"])
    sq = gpd.GeoDataFrame({"name": ["a"]}, geometry=[box(-83, 40, -82.99, 40.01)], crs="EPSG:4326")
    cache = hcas._project_cache_dir(pid)
    sq.to_file(cache / "greenspace.k1.geojson", driver="GeoJSON")
    sq.to_file(hcas._user_layer_path(pid, "facilities"), driver="GeoJSON")
    hcas._write_manifest(pid, {"layers": {
        "greenspace": {"kind": "fetched", "feature_count": 1, "crs": "EPSG:4326", "at": 1.7e9, "bbox_key": "k1"},
        "facilities": {"kind": "user", "feature_count": 1, "crs": "EPSG:4326", "at": 1.7e9,
                       "original_filename": "my_clinics.geojson"},
    }})
    area = gpd.GeoDataFrame({"n": [1]}, geometry=[box(-83, 40, -82.9, 40.1)], crs="EPSG:4326").to_crs("EPSG:5070")
    monkeypatch.setattr(hcas, "_load_area", lambda project, which: area)
    return pid, db


def test_step5_package_holds_layers_user_data_area_and_a_metadata_table(tmp_path, monkeypatch):
    import sqlite3

    from backend import hcas_db

    pid, db = _db_project(tmp_path, monkeypatch)
    out = hcas_db.build_package(pid, db=db)
    assert out["filename"] == "pkg-test-database.zip" and out["bytes"] > 0
    with zipfile.ZipFile(hcas_db.latest_path(pid)) as zf:
        assert sorted(zf.namelist()) == ["README.txt", "database.gpkg", "metadata.csv"]
        gpkg = tmp_path / "x.gpkg"
        gpkg.write_bytes(zf.read("database.gpkg"))
        readme = zf.read("README.txt").decode()
        csv_text = zf.read("metadata.csv").decode()
    layers = set(gpd.list_layers(gpkg)["name"])
    assert {"project_area", "greenspace", "facilities"} <= layers
    con = sqlite3.connect(gpkg)
    rows = {r[0]: r for r in con.execute(
        "SELECT layer, provider, vintage, fetched_at, user_provided FROM package_metadata")}
    assert rows["greenspace"][4] == "0" and "OpenStreetMap" in rows["greenspace"][1] and rows["greenspace"][3]
    assert rows["facilities"][4] == "1" and rows["facilities"][1] == "User-provided: my_clinics.geojson"
    assert "project_area" in rows
    assert "User-provided: my_clinics.geojson" in csv_text and "my_clinics" in readme


def test_step5_question_is_asked_only_when_the_database_changed(tmp_path, monkeypatch):
    from backend import hcas_db

    pid, db = _db_project(tmp_path, monkeypatch)
    assert hcas_db.status(pid, db=db)["ask"] is True            # never saved
    hcas_db.build_package(pid, db=db)
    st = hcas_db.status(pid, db=db)
    assert st["ask"] is False and st["has_copy"] is True        # unchanged since the save
    manifest = hcas._read_manifest(pid)
    manifest["layers"]["greenspace"]["at"] = 1.8e9              # a layer was fetched again
    hcas._write_manifest(pid, manifest)
    assert hcas_db.status(pid, db=db)["ask"] is True
    hcas_db.build_package(pid, db=db)
    assert hcas_db.status(pid, db=db)["ask"] is False
    hcas.update_project_scope(pid, db=db, area_geoids=["390490001002"])   # the area changed
    assert hcas_db.status(pid, db=db)["ask"] is True


def test_step5_routes_status_save_and_download(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from backend import hcas_db
    from backend.app import app

    pid, db = _db_project(tmp_path, monkeypatch)
    monkeypatch.setattr(hcas, "DB_PATH", db)
    c = TestClient(app)
    h = {"X-Requested-With": "geoai"}
    assert c.get(f"/hcas/projects/{pid}/database.zip", headers=h).status_code == 404   # nothing saved yet
    assert c.get(f"/hcas/projects/{pid}/database/status", headers=h).json()["ask"] is True
    assert c.post(f"/hcas/projects/{pid}/database/save", headers=h).status_code == 200
    r = c.get(f"/hcas/projects/{pid}/database.zip", headers=h)
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert c.get(f"/hcas/projects/{pid}/database/status", headers=h).json()["ask"] is False
    assert c.get("/hcas/projects/nope/database/status", headers=h).status_code == 404


def test_step5_confirm_shows_an_in_page_dialog_not_a_browser_confirm():
    cs = _fn("confirmStep")
    assert "S.current===5" in cs and "askSaveDatabase()" in cs
    dlg = _fn("askSaveDatabase")
    assert "Download and save the database?" in dlg and "modal(" in dlg
    assert "confirm(" not in dlg.replace("Confirm", "")
    assert "/database/status" in dlg and "if(!st.ask) return;" in dlg      # unchanged: no question
    assert "/database/save" in dlg and "database.zip" in dlg


# ---------------------------------------------------------------- #4b AI history + export
def _proj(tmp_path, monkeypatch):
    db = tmp_path / "h.db"
    monkeypatch.setattr(hcas, "DB_PATH", db)
    pid = hcas.create_project("hist", *FRANKLIN, db=db, areal_unit="block_group",
                              area_geoids=["390490001001", "390490001002"])
    return pid, db


def test_item4b_records_carry_step_kind_input_output_time_and_replay_context(tmp_path, monkeypatch):
    from backend import hcas_ai

    pid, db = _proj(tmp_path, monkeypatch)
    ctx = hcas_ai.context_for(hcas.get_project(pid, db=db))
    assert hcas_ai.record(pid, 1, "find_area", {"text": "Gary, Indiana"}, {"scale": "place"}, ctx, db=db)
    assert hcas_ai.record(pid, 2, "revise_factors", {"instruction": "add air"}, {"added": ["Air"]}, ctx, db=db)
    h = hcas_ai.list_history(pid, db=db)
    assert [x["kind"] for x in h] == ["find_area", "revise_factors"]
    first = h[0]
    assert first["step"] == 1 and first["user_input"] == {"text": "Gary, Indiana"}
    assert first["ai_output"] == {"scale": "place"} and first["created_at"] > 1.7e9
    # enough to replay: which county, which unit, how many units were selected
    assert first["context"]["county_fips"] == "049" and first["context"]["areal_unit"] == "block_group"
    assert first["context"]["area_units"] == 2
    assert hcas_ai.record("no-such-project", 1, "chat", {}, {}) is False      # never raises


def test_item4b_chat_and_step_drafts_are_recorded_by_the_pipeline(tmp_path, monkeypatch):
    from backend import hcas_ai

    pid, db = _proj(tmp_path, monkeypatch)
    monkeypatch.setattr(hcas, "_llm_text", lambda system, prompt, model="m": ("It is a block group.", {"output_tokens": 5}))
    hcas.chat(pid, 1, "what is a block group?", [{"role": "user", "content": "hi"}], db=db)
    monkeypatch.setattr(hcas, "_draft", lambda project, step: ({"kind": "us_sub_county", "layers": [1, 2]}, "m1", {"in": 1}))
    hcas.run_step(pid, 1, db=db)
    h = hcas_ai.list_history(pid, db=db)
    chat, draft = h[0], h[1]
    assert chat["kind"] == "chat" and chat["user_input"]["message"] == "what is a block group?"
    assert chat["ai_output"]["reply"] == "It is a block group."
    assert draft["kind"] == "step_draft" and draft["step"] == 1
    assert draft["ai_output"]["summary"]["layers_count"] == 2 and draft["ai_output"]["model"] == "m1"


def test_item4b_before_a_project_exists_the_record_rides_with_the_reply_then_is_imported(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from backend import hcas_ai
    from backend.app import app

    pid, db = _proj(tmp_path, monkeypatch)
    monkeypatch.setattr(hcas, "resolve_area", lambda text: {"resolved": True, "county_name": "Lake County", "scale": "place"})
    c = TestClient(app)
    h = {"X-Requested-With": "geoai"}
    r = c.post("/hcas/resolve-area", json={"text": "Gary, Indiana"}, headers=h).json()
    assert r["ai_record"]["kind"] == "find_area" and r["ai_record"]["user_input"] == {"text": "Gary, Indiana"}
    saved = c.post(f"/hcas/projects/{pid}/ai-history", json={"records": [r["ai_record"], {"kind": "bogus"}]}, headers=h)
    assert saved.json() == {"saved": 1}
    got = c.get(f"/hcas/projects/{pid}/ai-history", headers=h).json()["history"]
    assert got[0]["kind"] == "find_area" and got[0]["context"]["relayed_by_page"] is True
    # with a project id the server stores it itself and does not hand a record back
    r2 = c.post("/hcas/resolve-area", json={"text": "Gary, Indiana", "project_id": pid}, headers=h).json()
    assert "ai_record" not in r2
    assert len(hcas_ai.list_history(pid, db=db)) == 2


def test_item4b_export_is_one_json_with_settings_steps_and_history(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from backend import hcas_ai
    from backend.app import app

    pid, db = _proj(tmp_path, monkeypatch)
    monkeypatch.setattr(hcas, "_draft", lambda project, step: ({"kind": "us_sub_county"}, "m", {}))
    hcas.run_step(pid, 1, db=db)
    hcas.confirm_step(pid, 1, {"kind": "us_sub_county", "edited": True}, 3.0, db=db)
    hcas_ai.record(pid, 1, "chat", {"message": "q"}, {"reply": "a"}, db=db)
    c = TestClient(app)
    r = c.get(f"/hcas/projects/{pid}/export.json", headers={"X-Requested-With": "geoai"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    doc = r.json()
    assert doc["format"] == "hcas-project-export/1" and doc["settings"]["title"] == "hist"
    assert doc["settings"]["area_geoids"] == ["390490001001", "390490001002"]
    s1 = doc["steps"][0]
    assert s1["step"] == 1 and s1["status"] == "confirmed" and s1["human_final"]["edited"] is True
    assert s1["ai_draft"]["kind"] == "us_sub_county" and s1["edit_seconds"] == 3.0
    assert [h["kind"] for h in doc["ai_history"]] == ["step_draft", "chat"]
    assert c.get("/hcas/projects/nope/export.json", headers={"X-Requested-With": "geoai"}).status_code == 404


def test_item4b_page_sends_the_project_id_relays_records_and_has_the_views():
    for fn, needle in (("resolveArea", "project_id:S.pid"), ("suggestBlockGroups", "project_id:S.pid"),
                       ("sendChat", "relayAi(res)"), ("reviseFactors", "project_id:S.pid")):
        assert needle in _fn(fn), fn
    assert "flushPendingAi()" in _fn("ensureProject")
    assert "/ai-history" in _fn("showAiHistory") and "/export.json" in _fn("exportProject")
    assert 'onclick="showAiHistory()"' in HTML and 'onclick="exportProject()"' in HTML


# ---------------------------------------------------------------- step 3: own data or fetch, per factor
BG1, BG2, BG3 = "390490001001", "390490001002", "390490002001"


def _s3_project(tmp_path, monkeypatch, geoids=(BG1, BG2)):
    monkeypatch.setattr(hcas, "LAYER_CACHE_DIR", tmp_path / "cache")
    db = tmp_path / "h.db"
    monkeypatch.setattr(hcas, "DB_PATH", db)
    pid = hcas.create_project("s3", *FRANKLIN, db=db, areal_unit="block_group", area_geoids=list(geoids))
    factors = {"factors": [
        {"factor": "Green space", "metric": "greenspace_coverage", "rationale": "r"},
        {"factor": "Transit", "metric": "transit_access", "rationale": "r"},
        {"factor": "Poverty", "metric": "poverty_rate", "rationale": "r"},
        {"factor": "Mix", "metric": "near_major_road_exposure", "rationale": "r"},
        {"factor": "Air", "metric": "needs_data", "rationale": "r", "data_needed": "monitor readings"},
    ], "notes": "n"}
    monkeypatch.setattr(hcas, "_draft", lambda project, step: (factors, "m", {}))
    hcas.run_step(pid, 1, db=db)
    hcas.confirm_step(pid, 1, {"kind": "us_sub_county"}, db=db)
    hcas.run_step(pid, 2, db=db)
    hcas.confirm_step(pid, 2, factors, db=db)
    return pid, db


def _csv(rows, header=("GEOID", "value")):
    return ("\n".join([",".join(header)] + [",".join(str(c) for c in r) for r in rows]) + "\n").encode()


def test_step3_plan_lists_every_factor_with_its_source_and_defaults_to_fetch(tmp_path, monkeypatch):
    from backend import hcas_userdata

    pid, db = _s3_project(tmp_path, monkeypatch)
    plan = hcas_userdata.plan(pid, db=db)["factors"]
    assert [f["factor"] for f in plan] == ["Green space", "Transit", "Poverty", "Mix", "Air"]
    green, transit, poverty, mix, air = plan
    assert green["choice"] == "fetch" and green["source"]["provider"].startswith("OpenStreetMap")
    assert green["source"]["link"].startswith("https://") and green["source"]["vintage"]
    assert poverty["source"]["link"].startswith("https://www.atsdr.cdc.gov") and "2022" in poverty["source"]["vintage"]
    assert green["can_use_features"] is True and mix["can_use_features"] is False      # near_major_road: 2 layers
    assert air["awaiting_data"] is True and air["key"].startswith("needs_data:")


def test_step3_nothing_runs_until_the_analyst_presses_gather_data():
    cs = _fn("confirmStep")
    assert "S.current !== 3" in cs                              # confirming step 2 no longer auto-runs step 3
    assert "step3PlanShell()" in _fn("renderCanvas") and "loadStep3Plan()" in _fn("renderCanvas")
    assert "Gather data" in _fn("renderCmd") and "Gather data" in _fn("step3PlanShell")
    assert "Fetch automatically" in _fn("renderStep3Plan") and "Upload my own data" in _fn("renderStep3Plan")
    assert "step3Missing()" in _fn("runStep")


def test_step3_csv_with_geoid_and_one_value_column_is_used_directly(tmp_path, monkeypatch):
    from backend import hcas_userdata

    pid, db = _s3_project(tmp_path, monkeypatch)
    e = hcas_userdata.add_upload(pid, "poverty_rate", "pov.csv", _csv([(BG1, 10), (BG2, 20)]), "Poverty", db=db)
    assert e["kind"] == "table" and e["value_col"] == "value" and e["used"] is True and e["geoid_len"] == 12
    assert hcas_userdata.get_choices(pid)["poverty_rate"] == "upload"
    project = hcas.get_project(pid, db=db)
    vals, det = hcas_userdata.apply_tables(project, "project", ["poverty_rate"])
    assert vals == {"poverty_rate": 15.0}
    assert det["poverty_rate"]["source"] == "User-provided: pov.csv" and "2 of 2 units found" in det["poverty_rate"]["level"]


def test_step3_csv_with_several_value_columns_asks_which_and_partial_match_is_stated(tmp_path, monkeypatch):
    from backend import hcas_userdata

    pid, db = _s3_project(tmp_path, monkeypatch)
    e = hcas_userdata.add_upload(pid, "poverty_rate", "two.csv",
                                 _csv([(BG1, 1, 100), (BG3, 3, 300)], header=("GEOID", "a", "b")), "Poverty", db=db)
    assert e["needs_value_column"] is True and e["value_col"] is None and e["numeric_columns"] == ["a", "b"]
    project = hcas.get_project(pid, db=db)
    assert hcas_userdata.apply_tables(project, "project", ["poverty_rate"]) == ({}, {})   # not usable until chosen
    with pytest.raises(hcas_userdata.UserDataError):
        hcas_userdata.set_value_column(pid, e["id"], "nope")
    hcas_userdata.set_value_column(pid, e["id"], "b")
    vals, det = hcas_userdata.apply_tables(project, "project", ["poverty_rate"])
    assert vals == {"poverty_rate": 100.0}                       # only BG1 of the two selected units is in the file
    assert "1 of 2 units found" in det["poverty_rate"]["level"]


def test_step3_tract_level_table_serves_block_group_units_and_unmatched_is_not_measured(tmp_path, monkeypatch):
    from backend import hcas_userdata

    pid, db = _s3_project(tmp_path, monkeypatch)
    hcas_userdata.add_upload(pid, "poverty_rate", "tracts.csv", _csv([(BG1[:11], 8.0)]), db=db)
    project = hcas.get_project(pid, db=db)
    vals, _ = hcas_userdata.apply_tables(project, "project", ["poverty_rate"])
    assert vals == {"poverty_rate": 8.0}                          # both block groups sit in that tract
    hcas_userdata.remove(pid, hcas_userdata.list_entries(pid)[0]["id"])
    hcas_userdata.add_upload(pid, "poverty_rate", "other.csv", _csv([("390490099001", 5)]), db=db)
    vals, det = hcas_userdata.apply_tables(project, "project", ["poverty_rate"])
    assert vals == {"poverty_rate": None} and "None of the" in det["poverty_rate"]["not_measured"]


@pytest.mark.parametrize("content,message", [
    (b"name,value\nA,1\n", "No Census GEOID column"),
    (b"GEOID,name\n390490001001,a\n", "No numeric value column"),
    (b"GEOID\n390490001001\n", "at least one value column"),
    (b"GEOID,v\nabc,1\ndef,2\n", "must hold 11-digit tract or 12-digit"),
])
def test_step3_bad_csv_files_get_plain_errors(tmp_path, monkeypatch, content, message):
    from backend import hcas_userdata

    pid, db = _s3_project(tmp_path, monkeypatch)
    with pytest.raises(hcas_userdata.UserDataError, match=message):
        hcas_userdata.add_upload(pid, "poverty_rate", "bad.csv", content, db=db)


def test_step3_feature_upload_replaces_a_single_layer_or_is_stored_and_flagged_unused(tmp_path, monkeypatch):
    from backend import hcas_userdata

    pid, db = _s3_project(tmp_path, monkeypatch)
    pts = gpd.GeoDataFrame({"n": [1, 2]}, geometry=[Point(-83, 40), Point(-83.001, 40)], crs="EPSG:4326")
    e = hcas_userdata.add_upload(pid, "transit_access", "stops.geojson", _geojson_bytes(pts), "Transit", db=db)
    assert e["kind"] == "layer" and e["used"] is True and e["layer"] == "transit_stops"
    mf = hcas._read_manifest(pid)["layers"]["transit_stops"]
    assert mf["kind"] == "user" and mf["original_filename"] == "stops.geojson"      # the existing override, so step 3/7 use it
    assert hcas._user_layer_path(pid, "transit_stops").exists()
    e2 = hcas_userdata.add_upload(pid, "near_major_road_exposure", "bldg.geojson", _geojson_bytes(pts), "Mix", db=db)
    assert e2["used"] is False and "several layers" in e2["note"]
    assert "near_major_road_exposure" not in {k for k in hcas._read_manifest(pid)["layers"]}
    hcas_userdata.remove(pid, e["id"])
    assert "transit_stops" not in hcas._read_manifest(pid).get("layers", {})
    assert not hcas._user_layer_path(pid, "transit_stops").exists()
    with pytest.raises(hcas_userdata.UserDataError):
        hcas_userdata.add_upload(pid, "transit_access", "x.txt", b"hello", db=db)


def test_step3_report_cites_user_provided_files_and_says_when_unused(tmp_path, monkeypatch):
    from backend import hcas_userdata

    pid, db = _s3_project(tmp_path, monkeypatch)
    pts = gpd.GeoDataFrame({"n": [1]}, geometry=[Point(-83, 40)], crs="EPSG:4326")
    hcas_userdata.add_upload(pid, "poverty_rate", "pov.csv", _csv([(BG1, 10)]), "Poverty", db=db)
    hcas_userdata.add_upload(pid, "near_major_road_exposure", "bldg.geojson", _geojson_bytes(pts), "Mix", db=db)
    text = "\n".join(hcas_userdata.report_lines(hcas.get_project(pid, db=db)))
    assert "## User-provided data" in text
    assert "User-provided: pov.csv" in text and "used in the metric" in text
    assert "User-provided: bldg.geojson" in text and "**not used in the metric**" in text
    assert hcas_userdata.report_lines({"id": "nothing-here"}) == []


def test_step3_user_data_goes_into_the_step5_package_and_changes_its_fingerprint(tmp_path, monkeypatch):
    import sqlite3

    from backend import hcas_db, hcas_userdata

    pid, db = _s3_project(tmp_path, monkeypatch)
    area = gpd.GeoDataFrame({"n": [1]}, geometry=[box(-83, 40, -82.9, 40.1)], crs="EPSG:4326").to_crs("EPSG:5070")
    monkeypatch.setattr(hcas, "_load_area", lambda project, which: area)
    before = hcas_db.fingerprint(pid, db=db)
    pts = gpd.GeoDataFrame({"n": [1]}, geometry=[Point(-83, 40)], crs="EPSG:4326")
    hcas_userdata.add_upload(pid, "poverty_rate", "pov.csv", _csv([(BG1, 10)]), "Poverty", db=db)
    hcas_userdata.add_upload(pid, "near_major_road_exposure", "bldg.geojson", _geojson_bytes(pts), "Mix", db=db)
    assert hcas_db.fingerprint(pid, db=db) != before
    hcas_db.build_package(pid, db=db)
    with zipfile.ZipFile(hcas_db.latest_path(pid)) as zf:
        gpkg = tmp_path / "p.gpkg"
        gpkg.write_bytes(zf.read("database.gpkg"))
        csv_text = zf.read("metadata.csv").decode()
    con = sqlite3.connect(gpkg)
    assert con.execute("SELECT COUNT(*) FROM user_pov").fetchone()[0] == 1          # the GEOID table
    assert "bldg" in set(gpd.list_layers(gpkg)["name"]) or "bldg_geojson" in set(gpd.list_layers(gpkg)["name"]) \
        or any("bldg" in n for n in gpd.list_layers(gpkg)["name"])                  # the feature file
    assert "User-provided: pov.csv" in csv_text and "User-provided: bldg.geojson" in csv_text


def test_step3_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.app import app

    pid, db = _s3_project(tmp_path, monkeypatch)
    c = TestClient(app)
    h = {"X-Requested-With": "geoai"}
    assert len(c.get(f"/hcas/projects/{pid}/step3/plan", headers=h).json()["factors"]) == 5
    r = c.post(f"/hcas/projects/{pid}/step3/upload?key=poverty_rate&filename=p.csv&factor=Poverty",
               content=_csv([(BG1, 1), (BG2, 2)]), headers=h)
    assert r.status_code == 200 and r.json()["used"] is True
    bad = c.post(f"/hcas/projects/{pid}/step3/upload?key=poverty_rate&filename=b.csv", content=b"x,y\n1,2\n", headers=h)
    assert bad.status_code == 400 and "GEOID" in bad.json()["detail"]
    assert c.post(f"/hcas/projects/{pid}/step3/choice", json={"key": "poverty_rate", "choice": "fetch"}, headers=h).status_code == 200
    assert c.post(f"/hcas/projects/{pid}/step3/choice", json={"key": "poverty_rate", "choice": "maybe"}, headers=h).status_code == 400
    plan = c.get(f"/hcas/projects/{pid}/step3/plan", headers=h).json()["factors"]
    assert plan[2]["choice"] == "fetch" and len(plan[2]["uploads"]) == 1
    eid = r.json()["id"]
    assert c.delete(f"/hcas/projects/{pid}/step3/userdata/{eid}", headers=h).status_code == 200
    assert c.delete(f"/hcas/projects/{pid}/step3/userdata/{eid}", headers=h).status_code == 404
    # a project whose step 2 is not confirmed has no plan yet
    other = hcas.create_project("o", *FRANKLIN, db=db, areal_unit="county")
    assert c.get(f"/hcas/projects/{other}/step3/plan", headers=h).status_code == 404
