"""HCAS — the 8-step gated assessment machine (Milestone 1 skeleton).

A code-owned coordinator, not an agent: each step drafts an artifact, a human
confirms (possibly edits) it, and only then may the next step run. Every step
row carries the audit tuple (ai_draft, human_final, edit_delta, edit_seconds)
— the dataset for the HITL-vs-autonomy ablation.

Steps 2-8 are stubs in this skeleton; they gain real runners incrementally.
"""
from __future__ import annotations

import difflib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from backend.gis.counties import build_place_name, centroid_of_county, lookup_county

DB_PATH = Path(__file__).with_name("hcas.db")

# (step, phase, title, runner kind)
STEPS = [
    (1, "scope", "Study area intake", "deterministic"),
    (2, "methodology", "Factor & metric selection", "structured"),
    (3, "data", "Data retrieval", "tool_loop"),
    (4, "methodology", "Solution framework", "structured"),
    (5, "gis", "GIS database construction", "deterministic"),
    (6, "gis", "Workflow design", "structured"),
    (7, "gis", "GIS procedure execution", "tool_loop"),
    (8, "report", "Report assembly", "structured"),
]

# The metric registry the LLM selects from — mirrors backend/assess/metrics.py.
# Steps 2 and 6 prompt against this; step 7 executes against the same names.
METRICS = {
    "building_coverage_ratio": "building footprint area / study area (0-1)",
    "greenspace_coverage": "greenspace area_m2 and ratio of study area",
    "road_density_km_per_km2": "road length km per km2",
    "far": "floor area ratio; carries levels_tagged_pct honesty flag (untagged buildings count as 1 level)",
    "parking_coverage_ratio": "parking footprint area / study area (0-1)",
    "pedestrian_density_km_per_km2": "sidewalk/path length km per km2",
    "transit_access": "transit stop count and density per km2",
    "facility_access": "community facility count and density per km2 (hospitals, clinics, pharmacies, schools, libraries, groceries)",
}

_STEP2_SYSTEM = (
    "You are the methodology engine of HCAS, a healthy-community assessment system "
    "used by urban-planning researchers. Given a study area, select the health-relevant "
    "factors to assess and map each to exactly one metric from the registry below. "
    "Choose metrics ONLY from the registry. Ground rationales in healthy-cities / "
    "social-determinants-of-health reasoning. Note: the metric set is an early subset; "
    "flag important factors the registry cannot yet measure in `notes`.\n\n"
    "METRIC REGISTRY:\n"
    + "\n".join(f"- {name}: {desc}" for name, desc in METRICS.items())
)

_STEP2_SCHEMA = {
    "type": "object",
    "properties": {
        "factors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "factor": {"type": "string"},
                    "metric": {"type": "string", "enum": list(METRICS)},
                    "rationale": {"type": "string"},
                },
                "required": ["factor", "metric", "rationale"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["factors", "notes"],
    "additionalProperties": False,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    meta_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    project_id TEXT NOT NULL,
    step INTEGER NOT NULL,
    data_json TEXT NOT NULL,
    status TEXT NOT NULL,          -- drafted | confirmed | stale
    drafted_at REAL,
    confirmed_at REAL,
    PRIMARY KEY (project_id, step)
);
"""


@contextmanager
def _conn(db: Path):
    conn = sqlite3.connect(db)
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Step runners — step 1 is real, 2-8 are stubs until their week lands
# ---------------------------------------------------------------------------

def _run_step_1(meta: dict) -> dict:
    """Typed study-area artifact. bbox-first union so a China pivot is a new
    kind, not a rewrite (ponytail: only us_county implemented)."""
    state, county = meta["state_fips"], meta["county_fips"]
    lat, lon = centroid_of_county(state, county)
    return {
        "kind": "us_county",
        "state_fips": state,
        "county_fips": county,
        "place_name": build_place_name(state, county),
        "centroid": [lat, lon],
    }


def _llm_structured(
    system: str,
    prompt: str,
    schema: dict,
    model: str = "claude-opus-4-8",
) -> tuple[dict, dict]:
    """One structured-output call. Returns (parsed_json, usage). Raises RuntimeError
    on refusal/truncation so the step surfaces a clear error instead of a bad draft."""
    import anthropic

    from backend.config import settings

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=system,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason not in ("end_turn", "stop_sequence"):
        raise RuntimeError(f"LLM stopped with {resp.stop_reason}")
    text = next(b.text for b in resp.content if b.type == "text")
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_read_input_tokens": resp.usage.cache_read_input_tokens,
    }
    return json.loads(text), usage


def _run_step_2(project: dict) -> tuple[dict, str | None, dict | None]:
    area = next(s for s in project["steps"] if s["step"] == 1)
    artifact = area.get("human_final") or area["ai_draft"]
    place = artifact.get("place_name", "the study area")
    model = "claude-opus-4-8"
    prompt = (
        f"Study area: {place}.\n"
        f"Assessment project: {project['meta']['title']}.\n"
        "Select the factors and metrics for a healthy-community assessment of this area."
    )
    draft, usage = _llm_structured(_STEP2_SYSTEM, prompt, _STEP2_SCHEMA, model=model)
    return draft, model, usage


def _draft(project: dict, step: int) -> tuple[dict, str | None, dict | None]:
    if step == 1:
        return _run_step_1(project["meta"]), None, None
    if step == 2:
        return _run_step_2(project)
    n, phase, title, kind = STEPS[step - 1]
    # ponytail: stub artifact; real runners land per the build plan (W2: 3-4, W3: 5-8)
    return {"stub": True, "step": n, "phase": phase, "title": title, "runner": kind}, None, None


# ---------------------------------------------------------------------------
# Machine
# ---------------------------------------------------------------------------

def create_project(
    title: str,
    state_fips: str,
    county_fips: str,
    gate_mode: str = "gated",
    db: Path | None = None,
) -> str:
    db = db or DB_PATH
    if lookup_county(state_fips, county_fips) is None:
        raise ValueError(f"Unknown county FIPS {state_fips}-{county_fips}")
    if gate_mode not in ("gated", "auto"):
        raise ValueError("gate_mode must be 'gated' or 'auto'")
    # ponytail: gate_mode is stored only; the auto path lands with the M2 ablation
    pid = uuid.uuid4().hex[:12]
    meta = {
        "title": title,
        "state_fips": state_fips,
        "county_fips": county_fips,
        "gate_mode": gate_mode,
        "current_step": 1,
        "created_at": time.time(),
    }
    with _conn(db) as conn:
        conn.execute("INSERT INTO projects VALUES (?, ?)", (pid, json.dumps(meta)))
    return pid


def get_project(pid: str, db: Path | None = None) -> dict:
    db = db or DB_PATH
    with _conn(db) as conn:
        row = conn.execute("SELECT meta_json FROM projects WHERE id = ?", (pid,)).fetchone()
        if row is None:
            raise KeyError(pid)
        arts = conn.execute(
            "SELECT step, data_json, status, drafted_at, confirmed_at"
            " FROM artifacts WHERE project_id = ? ORDER BY step",
            (pid,),
        ).fetchall()
    return {
        "id": pid,
        "meta": json.loads(row["meta_json"]),
        "steps": [
            {
                "step": a["step"],
                "status": a["status"],
                "drafted_at": a["drafted_at"],
                "confirmed_at": a["confirmed_at"],
                **json.loads(a["data_json"]),
            }
            for a in arts
        ],
    }


def run_step(pid: str, step: int, db: Path | None = None) -> dict:
    """Draft step `step`. The gate: every earlier step must be confirmed."""
    db = db or DB_PATH
    if not 1 <= step <= 8:
        raise ValueError(f"step must be 1-8, got {step}")
    project = get_project(pid, db)
    confirmed = {s["step"] for s in project["steps"] if s["status"] == "confirmed"}
    missing = [n for n in range(1, step) if n not in confirmed]
    if missing:
        raise ValueError(f"cannot run step {step}: steps {missing} not confirmed")

    ai_draft, model, usage = _draft(project, step)
    data = {
        "ai_draft": ai_draft,
        "human_final": None,
        "edit_delta": None,
        "edit_seconds": None,
        "model": model,
        "usage": usage,
    }
    with _conn(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO artifacts VALUES (?, ?, ?, 'drafted', ?, NULL)",
            (pid, step, json.dumps(data), time.time()),
        )
    return data["ai_draft"]


def confirm_step(
    pid: str,
    step: int,
    human_final: Any,
    edit_seconds: float | None = None,
    db: Path | None = None,
) -> dict:
    """Confirm (and possibly edit) a drafted artifact; stale-flag downstream.

    ponytail: stale downstream steps must be re-confirmed (or re-run) by hand —
    no automatic cascade re-execution until trials show users backtrack.
    """
    db = db or DB_PATH
    with _conn(db) as conn:
        row = conn.execute(
            "SELECT data_json FROM artifacts WHERE project_id = ? AND step = ?",
            (pid, step),
        ).fetchone()
        if row is None:
            raise KeyError(f"step {step} has no draft to confirm")
        data = json.loads(row["data_json"])

        a = json.dumps(data["ai_draft"], indent=1, sort_keys=True).splitlines()
        b = json.dumps(human_final, indent=1, sort_keys=True).splitlines()
        delta = "\n".join(difflib.unified_diff(a, b, "ai_draft", "human_final", lineterm=""))
        data.update(
            human_final=human_final,
            edit_delta=delta or None,
            edit_seconds=edit_seconds,
        )
        conn.execute(
            "UPDATE artifacts SET data_json = ?, status = 'confirmed', confirmed_at = ?"
            " WHERE project_id = ? AND step = ?",
            (json.dumps(data), time.time(), pid, step),
        )
        stale = conn.execute(
            "UPDATE artifacts SET status = 'stale' WHERE project_id = ? AND step > ?",
            (pid, step),
        ).rowcount
    return {"step": step, "status": "confirmed", "stale_downstream": stale}
