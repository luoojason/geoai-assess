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
