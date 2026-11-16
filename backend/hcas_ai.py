"""The project's AI interactions, saved (Prof. Shi 2026-11-13, #4).

"[A project] should save two major components: 1) the interactions with AI, so that if
needed, those operations can be repeated (like a 'skill'); 2) the current status of
the project, so that the user can see all the current data and settings."

Every AI interaction is one row of ``ai_history``: the step, the kind, the user's input,
the AI's output (or a structured summary of a big one), a timestamp and the context
(area, reporting unit, model) that a replay would need. Nothing here replays anything —
what "repeated like a skill" should mean is an open question for Prof. Shi; the rows
just carry enough to do it later.

An interaction made before the project exists (Find with AI on a blank form) cannot be
stored yet. The server hands the record back with the reply (``ai_record``) and the page
sends it in once the project has been created (``import_records``).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

KINDS = {
    "find_area": "Find with AI",
    "suggest_area": "Suggested block groups",
    "place_area": "City boundary lookup",
    "chat": "Assistant chat",
    "step_draft": "AI draft of the step",
    "revise_factors": "AI revision of the factors",
}


def _hcas():
    from backend import hcas

    return hcas


def context_for(project: dict | None) -> dict:
    """What a replay of this interaction would need to know about the project."""
    if not project:
        return {}
    m = project.get("meta") or {}
    return {"state_fips": m.get("state_fips"), "county_fips": m.get("county_fips"),
            "areal_unit": m.get("areal_unit"), "area_units": len(m.get("area_geoids") or []),
            "area_name": m.get("area_name") or "", "title": m.get("title")}


def make_record(step: int | None, kind: str, user_input, ai_output, context: dict | None = None) -> dict:
    return {"step": step, "kind": kind, "user_input": user_input, "ai_output": ai_output,
            "context": context or {}, "created_at": time.time()}


def _insert(conn, pid: str, r: dict) -> None:
    conn.execute(
        "INSERT INTO ai_history (project_id, step, kind, user_input, ai_output, context, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pid, r.get("step"), str(r.get("kind") or "")[:40],
         json.dumps(r.get("user_input"), default=str), json.dumps(r.get("ai_output"), default=str),
         json.dumps(r.get("context") or {}, default=str), float(r.get("created_at") or time.time())))


def record(pid: str | None, step: int | None, kind: str, user_input, ai_output,
           context: dict | None = None, db: Path | None = None) -> bool:
    """Store one interaction. Never raises: losing a history row must not fail the
    interaction it describes."""
    if not pid:
        return False
    h = _hcas()
    try:
        with h._conn(db or h.DB_PATH) as conn:
            if conn.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone() is None:
                return False
            _insert(conn, pid, make_record(step, kind, user_input, ai_output, context))
        return True
    except Exception as exc:
        logger.warning("ai_history write failed: %r", exc)
        return False


def import_records(pid: str, records: list, db: Path | None = None) -> int:
    """Store interactions the page held back until the project existed."""
    h = _hcas()
    n = 0
    with h._conn(db or h.DB_PATH) as conn:
        if conn.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone() is None:
            raise KeyError(pid)
        for r in (records or [])[:200]:
            if not isinstance(r, dict) or r.get("kind") not in KINDS:
                continue
            r = dict(r)
            ctx = r.get("context") if isinstance(r.get("context"), dict) else {}
            ctx["relayed_by_page"] = True              # made before the project existed
            r["context"] = ctx
            _insert(conn, pid, r)
            n += 1
    return n


def list_history(pid: str, db: Path | None = None) -> list[dict]:
    h = _hcas()
    with h._conn(db or h.DB_PATH) as conn:
        if conn.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone() is None:
            raise KeyError(pid)
        rows = conn.execute(
            "SELECT id, step, kind, user_input, ai_output, context, created_at FROM ai_history"
            " WHERE project_id = ? ORDER BY created_at, id", (pid,)).fetchall()

    def load(v):
        try:
            return json.loads(v) if v is not None else None
        except ValueError:
            return v

    return [{"id": r["id"], "step": r["step"], "kind": r["kind"], "label": KINDS.get(r["kind"], r["kind"]),
             "user_input": load(r["user_input"]), "ai_output": load(r["ai_output"]),
             "context": load(r["context"]) or {}, "created_at": r["created_at"]} for r in rows]


def summarize_draft(step: int, draft: dict, model: str | None = None, usage: dict | None = None) -> dict:
    """A step's AI draft as stored in the history: the whole draft for the small
    LLM-written steps (2, 4, 6), a structured summary for the data-heavy ones."""
    if step in (2, 4, 6):
        return {"draft": draft, "model": model, "usage": usage}
    summary: dict = {"kind": draft.get("kind"), "keys": sorted(k for k in draft if not k.startswith("_"))}
    for k in ("layers", "tables", "steps", "factors"):
        if isinstance(draft.get(k), list):
            summary[f"{k}_count"] = len(draft[k])
    return {"summary": summary, "model": model, "usage": usage}


def export_project(pid: str, db: Path | None = None) -> dict:
    """One JSON document: settings, every step's content and status, the AI history."""
    h = _hcas()
    project = h.get_project(pid, db=db)
    return {
        "format": "hcas-project-export/1",
        "exported_at": time.time(),
        "project_id": pid,
        "settings": project["meta"],
        "steps": [
            {"step": s["step"], "title": h.STEPS[s["step"] - 1][2] if 1 <= s["step"] <= len(h.STEPS) else None,
             "status": s["status"], "drafted_at": s.get("drafted_at"), "confirmed_at": s.get("confirmed_at"),
             "ai_draft": s.get("ai_draft"), "human_final": s.get("human_final"),
             "edit_delta": s.get("edit_delta"), "edit_seconds": s.get("edit_seconds"),
             "model": s.get("model")}
            for s in project["steps"]
        ],
        "ai_history": list_history(pid, db),
    }
