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

from backend.assess.references import REFERENCES
from backend.gis.counties import build_place_name, centroid_of_county, lookup_county

DB_PATH = Path(__file__).with_name("hcas.db")

# (step, phase, title, runner kind)
STEPS = [
    (1, "scope", "Study area intake", "deterministic"),
    (2, "methodology", "Factor & metric selection", "structured"),
    # ponytail: step 3 is deterministic, not an LLM tool loop — confirmed metrics
    # imply their layers via a static dict; add an LLM loop when sources become
    # open-ended (ACS/EJScreen handlers, arbitrary data requests)
    (3, "data", "Data retrieval", "deterministic"),
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

# Which OSM layers each metric needs (step 3 fetches the union).
METRIC_LAYERS = {
    "building_coverage_ratio": ["buildings"],
    "greenspace_coverage": ["greenspace"],
    "road_density_km_per_km2": ["roads"],
    "far": ["buildings"],
    "parking_coverage_ratio": ["parking"],
    "pedestrian_density_km_per_km2": ["sidewalks"],
    "transit_access": ["transit_stops"],
    "facility_access": ["facilities"],
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


def _confirmed(project: dict, step: int) -> dict:
    """The human-approved artifact for a step (edited version wins)."""
    row = next(s for s in project["steps"] if s["step"] == step)
    return row.get("human_final") or row["ai_draft"]


def _run_step_2(project: dict) -> tuple[dict, str | None, dict | None]:
    place = _confirmed(project, 1).get("place_name", "the study area")
    model = "claude-opus-4-8"
    prompt = (
        f"Study area: {place}.\n"
        f"Assessment project: {project['meta']['title']}.\n"
        "Select the factors and metrics for a healthy-community assessment of this area."
    )
    draft, usage = _llm_structured(_STEP2_SYSTEM, prompt, _STEP2_SCHEMA, model=model)
    return draft, model, usage


# Step 3 — fetch the union of layers the confirmed metrics need. Fetchers are in
# a patchable dict; each returns an empty GeoDataFrame on failure (never raises),
# so a zero feature_count in the inventory is the human's cue at the gate.
def _layer_fetchers() -> dict:
    from backend.assess import (
        fetch_buildings,
        fetch_facilities,
        fetch_greenspace,
        fetch_parking,
        fetch_road_network,
        fetch_sidewalks,
        fetch_transit_stops,
    )

    return {
        "buildings": fetch_buildings,
        "greenspace": fetch_greenspace,
        "roads": fetch_road_network,
        "parking": fetch_parking,
        "sidewalks": fetch_sidewalks,
        "transit_stops": fetch_transit_stops,
        "facilities": fetch_facilities,
    }


def _bbox_from_centroid(lat: float, lon: float, radius_m: float = 5000) -> list[float]:
    import geopandas as gpd
    from shapely.geometry import Point

    buf = (
        gpd.GeoSeries([Point(lon, lat)], crs="EPSG:4326")
        .to_crs("EPSG:5070")
        .buffer(radius_m)
        .to_crs("EPSG:4326")
    )
    west, south, east, north = buf.total_bounds
    return [float(west), float(south), float(east), float(north)]


def _run_step_3(project: dict) -> tuple[dict, str | None, dict | None]:
    from concurrent.futures import ThreadPoolExecutor

    factors = _confirmed(project, 2)["factors"]
    names = sorted({layer for f in factors for layer in METRIC_LAYERS[f["metric"]]})
    lat, lon = _confirmed(project, 1)["centroid"]
    bbox = _bbox_from_centroid(lat, lon)
    fetchers = _layer_fetchers()
    with ThreadPoolExecutor(max_workers=4) as ex:
        gdfs = list(ex.map(lambda n: fetchers[n](tuple(bbox)), names))
    inventory = [
        {"layer": n, "feature_count": int(len(g)), "crs": str(g.crs)}
        for n, g in zip(names, gdfs)
    ]
    return {
        "kind": "layer_inventory",
        "bbox": bbox,
        "radius_m": 5000,
        "layers": inventory,
    }, None, None


# ponytail: this dict IS the mock knowledgebase — the kg.community_summary /
# rag.passages surface as canned data. Swaps for Prof. Wang's live KG behind
# the same lookup once their team settles methods and interface.
KG_THEORIES = [
    {
        "name": "WHO Healthy Cities",
        "summary": "Health is created in the settings of everyday life; city governance, "
        "urban planning, and community participation are primary levers for population "
        "health, not only healthcare provision.",
        "source": "WHO European Healthy Cities Network (1986-)",
    },
    {
        "name": "Social Determinants of Health",
        "summary": "Health outcomes are driven by conditions in the environments where "
        "people live, work, and play: economic stability, education, healthcare access, "
        "neighborhood and built environment, and social context.",
        "source": "Healthy People 2030, US HHS",
    },
    {
        "name": "Barton & Grant Health Map",
        "summary": "A settlement-scale ecosystem model of health: people at the centre, "
        "ringed by lifestyle, community, local economy, activities, built environment, "
        "and natural environment; planning interventions act on the outer rings to "
        "influence the centre.",
        "source": "Barton & Grant (2006), Journal of the Royal Society for the Promotion of Health",
    },
    {
        "name": "Urban Green Space and Health",
        "summary": "Access to urban green space reduces stress and cardiovascular risk and "
        "increases physical activity; benefits are strongest for disadvantaged groups, "
        "making greenspace provision a health-equity intervention.",
        "source": "WHO Regional Office for Europe (2016), Urban green spaces and health review",
    },
    {
        "name": "Walkability and Active Living",
        "summary": "Street connectivity, land-use mix, pedestrian infrastructure, and "
        "destination density determine everyday physical activity; walkable environments "
        "lower obesity and chronic-disease burden.",
        "source": "Active Living Research synthesis (Sallis et al.)",
    },
]

_REFERENCE_TITLES = sorted({ref["title"] for refs in REFERENCES.values() for ref in refs})

_STEP4_SYSTEM = (
    "You are the methodology engine of HCAS, drafting the solution framework for a "
    "healthy-community assessment — the academic deliverable a human researcher will "
    "review and edit. Ground the framework ONLY in the theories below, and cite ONLY "
    "from the reference database below (by exact title). Be honest in assumptions and "
    "limitations: OSM completeness varies, FAR carries a levels_tagged_pct honesty flag, "
    "and the current metric registry measures the built environment only.\n\n"
    "THEORIES (the knowledgebase):\n"
    + "\n".join(f"- {t['name']}: {t['summary']} [{t['source']}]" for t in KG_THEORIES)
    + "\n\nREFERENCE DATABASE:\n"
    + "\n".join(
        f"- {r['title']} ({r['authors']}, {r['year']}, {r['journal']})"
        for refs in REFERENCES.values()
        for r in refs
    )
)

_STEP4_SCHEMA = {
    "type": "object",
    "properties": {
        "framework": {"type": "string"},
        "theoretical_basis": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "theory": {"type": "string", "enum": [t["name"] for t in KG_THEORIES]},
                    "application": {"type": "string"},
                },
                "required": ["theory", "application"],
                "additionalProperties": False,
            },
        },
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "limitations": {"type": "array", "items": {"type": "string"}},
        "references": {
            "type": "array",
            "items": {"type": "string", "enum": _REFERENCE_TITLES},
        },
    },
    "required": ["framework", "theoretical_basis", "assumptions", "limitations", "references"],
    "additionalProperties": False,
}


def _run_step_4(project: dict) -> tuple[dict, str | None, dict | None]:
    place = _confirmed(project, 1).get("place_name", "the study area")
    factors = _confirmed(project, 2)["factors"]
    inventory = _confirmed(project, 3)["layers"]
    model = "claude-opus-4-8"
    prompt = (
        f"Study area: {place}.\n"
        f"Assessment project: {project['meta']['title']}.\n\n"
        "Confirmed factors and metrics:\n"
        + "\n".join(f"- {f['factor']} -> {f['metric']}: {f['rationale']}" for f in factors)
        + "\n\nCollected data layers:\n"
        + "\n".join(f"- {l['layer']}: {l['feature_count']} features" for l in inventory)
        + "\n\nDraft the solution framework for this assessment."
    )
    draft, usage = _llm_structured(_STEP4_SYSTEM, prompt, _STEP4_SCHEMA, model=model)
    return draft, model, usage


def _draft(project: dict, step: int) -> tuple[dict, str | None, dict | None]:
    if step == 1:
        return _run_step_1(project["meta"]), None, None
    if step == 2:
        return _run_step_2(project)
    if step == 3:
        return _run_step_3(project)
    if step == 4:
        return _run_step_4(project)
    n, phase, title, kind = STEPS[step - 1]
    # ponytail: stub artifact; real runners land per the build plan (W3: 5-8)
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
