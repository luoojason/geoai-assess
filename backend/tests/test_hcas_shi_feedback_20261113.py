"""Prof. Shi's 2026-11-13 quick UI fixes to the HCAS wizard.

The page is a single template, so — like the 2026-09-16 feedback tests — these pin
the structure of ``frontend/templates/hcas.html``.
"""
import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "frontend" / "templates" / "hcas.html"
HTML = TEMPLATE.read_text(encoding="utf-8")


def _fn(name: str) -> str:
    """Source of one top-level function."""
    m = re.search(r"^(?:async )?function %s\(.*?^}" % re.escape(name), HTML, re.S | re.M)
    assert m, name
    return m.group(0)


def test_rail_keeps_the_step_number_and_puts_the_check_after_the_name():
    rail = _fn("renderRail")
    assert '<span class="pdot ${st}">${n}</span>' in rail
    assert 'class="plabel">${STEP_TITLES[n-1]}${tick}' in rail
    assert "✓" in rail  # the check mark itself still exists


def test_no_step_n_of_8_line_on_any_step():
    assert "of 8 ·" not in _fn("canvasHead")
    assert "Step ${n} of" not in HTML


def test_step1_wording_and_removed_lines():
    assert '"Set the task and study area for the assessment."' in HTML
    assert "Set the scope and study area" not in HTML
    form = _fn("defineForm")
    assert "Project setup" not in form
    assert "<label>Project task</label>" in form
    assert "<label>Notes</label>" not in form


def test_step1_layout_location_on_top_compare_at_bottom_of_project_panel():
    area = _fn("areaEditorHtml")
    area = area[area.rindex("return `"):]   # the assembled panel, not the pieces above it
    pos = {k: area.index(k) for k in
           ('${where}', 'id="${w}_state"', 'id="${w}_unit"',
            '${updateBtn}', 'id="${w}_map"', 'id="${w}_sum"', '${compare}')}
    assert pos['${where}'] < pos['id="${w}_state"'] < pos['id="${w}_unit"']
    # the (old) Run step button now sits on top of the map; compare is last
    assert pos['${updateBtn}'] < pos['id="${w}_map"'] < pos['id="${w}_sum"'] < pos['${compare}']
    # location + compare moved out of the outer form
    form = _fn("defineForm")
    assert 'id="f_where"' not in form and 'id="f_compare"' not in form
    # Project task takes the place the location box used to have (before the area panel)
    assert form.index("Project task") < form.index('id="ap_project"')


def test_map_is_taller():
    m = re.search(r"\.areamap\{height:(\d+)px", HTML)
    assert m and int(m.group(1)) >= 500


def test_step1_has_no_separate_results_page_and_run_moves_to_update_map():
    # Step 1 is always the form (except while running); Confirm does the work.
    assert "function step1Form(){ return S.current===1 && S.running!==1; }" in HTML
    assert "n===1 && S.running!==1" in _fn("canvasHead")
    assert "n===1 && S.running!==1" in _fn("renderCanvas")
    cmd = _fn("renderCmd")
    assert 'runbtn").style.display = hideRun' in cmd and "form ||" in cmd
    assert "async function updateMap()" in HTML
    confirm = _fn("confirmStep")
    assert "commitStep1" in confirm
    commit = _fn("commitStep1")
    assert "await runStep()" in commit and 'status==="confirmed"' in commit


def test_ai_suggestion_button_rename_greying_and_instruction_line():
    assert 'const SUGGEST_LABEL = "Generate/Update AI\'s suggestion";' in HTML
    assert "Suggest block groups with AI" not in HTML
    scope = _fn("renderScope")
    assert "Click to add or remove a ${one} to modify the" in scope
    assert "suggestIsStale(a)" in scope                       # disabled unless the text is new
    assert "updateSuggestBtn('${w}')" in scope                # re-enabled when the input changes
    sug = _fn("suggestBlockGroups")
    assert "a.suggestedText" in sug
    assert 'id="${w}_maphint"' in _fn("areaEditorHtml")


def test_ai_failure_line_above_manual_radios():
    scope = _fn("renderScope")
    msg = "AI could not define the specified area. Please manually define it on the map."
    assert msg in scope
    assert scope.index(msg) < scope.index('type="radio"')
    assert "a.aiFailed = true" in _fn("resolveArea") and "aiFailed = true" in _fn("resolveFailed")


def test_step2_rerun_is_under_the_notes_box_in_the_panel():
    f = _fn("viewFactors")
    assert f.index("<h4>Notes</h4>") < f.index('id="f_rerun"')
    assert 'id="f_rerun" onclick="runStep()"' in f
    # the command-bar copy is hidden only when step 2 has a result to show
    assert "S.current===2 && !!row" in _fn("renderCmd")
