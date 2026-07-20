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
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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
    # ponytail: 5-8 are deterministic in v1 — the metric set is closed, so the
    # workflow, execution, and report derive from confirmed artifacts; LLM
    # narration/design returns when procedures become open-ended (CodeSpec etc.)
    (5, "gis", "GIS database construction", "deterministic"),
    (6, "gis", "Workflow design", "deterministic"),
    (7, "gis", "GIS procedure execution", "deterministic"),
    (8, "report", "Report assembly", "deterministic"),
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
    "land_use_mix": "normalized Shannon entropy of OSM landuse polygon areas (0 = single use, 1 = even mix)",
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
    "land_use_mix": ["landuse"],
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
CREATE TABLE IF NOT EXISTS maps (
    project_id TEXT PRIMARY KEY,   -- Folium HTML kept out of the artifact JSON
    html TEXT NOT NULL,
    created_at REAL
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


# One plain-language message per upstream failure mode, shared by the step
# runner and the chat path so the wizard shows the same words everywhere.
_NO_KEY_MSG = (
    "No API key is configured. Open the .env file next to START-HCAS and set "
    "ANTHROPIC_API_KEY=sk-ant-... or an OpenRouter key (sk-or-...) — if the line "
    "is there but empty after the '=', paste the key after it. Then restart HCAS."
)


def _upstream_error(
    provider: str,
    status: int | None = None,
    body: str = "",
    exc: Exception | None = None,
) -> RuntimeError:
    """Log the raw upstream failure server-side (key never included) and return a
    RuntimeError whose message is plain language — the /run and /chat routes
    surface it verbatim as the 502 `detail`."""
    if status is not None:
        logger.error("%s upstream error HTTP %s: %s", provider, status, body[:800])
        if status in (401, 403):
            msg = (
                f"The AI provider ({provider}) rejected the API key — it may have "
                "been revoked or replaced. Update the key in the .env file next to "
                "START-HCAS and restart."
            )
        elif status == 402:
            msg = (
                f"The AI provider ({provider}) key is out of credit. Replace the "
                "key in .env (or add credit), then restart."
            )
        elif status == 429:
            msg = (
                f"The AI provider ({provider}) is rate-limiting requests. Wait a "
                "minute and try again."
            )
        else:
            msg = (
                f"The AI provider ({provider}) returned an unexpected error "
                f"(HTTP {status}). Try again; if it persists, the terminal window "
                "has the full error."
            )
        return RuntimeError(msg)

    logger.error("%s request failed before a response: %r", provider, exc)
    import requests as _requests

    # "Timeout" in the class name also catches anthropic.APITimeoutError
    if isinstance(exc, _requests.exceptions.Timeout) or "Timeout" in type(exc).__name__:
        msg = (
            f"The AI provider ({provider}) did not respond in time — check your "
            "internet connection and try again."
        )
    else:
        msg = (
            f"Cannot reach the AI provider ({provider}) — check your internet "
            "connection, VPN, or proxy settings. A leftover system proxy (for "
            "example from a VPN/Clash setup that is no longer running) can block "
            "HCAS while the browser still works."
        )
    return RuntimeError(msg)


def _post_openrouter(payload: dict, api_key: str, timeout: int) -> dict:
    """POST to OpenRouter chat completions and return the parsed body. Every
    failure mode — unreachable, timeout, non-200, in-body error envelope — comes
    back as the friendly RuntimeError from _upstream_error."""
    import requests

    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=timeout,
        )
    except requests.exceptions.RequestException as exc:
        raise _upstream_error("OpenRouter", exc=exc) from exc
    if r.status_code != 200:
        raise _upstream_error("OpenRouter", status=r.status_code, body=r.text)
    out = r.json()
    if "error" in out:
        err = out["error"] or {}
        code = err.get("code")
        raise _upstream_error(
            "OpenRouter",
            status=code if isinstance(code, int) else 500,
            body=json.dumps(err),
        )
    return out


def _llm_via_claude_cli(system: str, prompt: str, schema: dict, model: str) -> tuple[dict, dict]:
    """ponytail: subscription fallback — shells out to `claude -p` (Claude Code CLI,
    Max plan) when no API key is configured. The schema is not server-enforced on
    this path, so validate client-side and retry once with the validation error;
    the API-key path is the production one."""
    import shutil
    import subprocess

    import jsonschema

    if not shutil.which("claude"):
        raise RuntimeError(_NO_KEY_MSG)

    def call(extra: str) -> tuple[dict, dict]:
        full = (
            f"{prompt}\n\nRespond with ONLY a JSON object valid against this JSON Schema "
            f"— no markdown fences, no prose. Every schema field is required and every "
            f"array item must match its item type exactly:\n{json.dumps(schema)}{extra}"
        )
        proc = subprocess.run(
            ["claude", "-p", full, "--output-format", "json",
             "--model", model, "--append-system-prompt", system, "--max-turns", "1"],
            capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude CLI failed (rc={proc.returncode}): {proc.stderr[-800:]} | {proc.stdout[-400:]}"
            )
        out = json.loads(proc.stdout)
        text = out["result"].strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        usage = {
            k: out.get("usage", {}).get(k, 0)
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens")
        }
        usage["via"] = "claude-cli"
        return json.loads(text), usage

    parsed, usage = call("")
    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        parsed, usage = call(
            f"\n\nYour previous attempt failed schema validation: {exc.message}. Fix it."
        )
        jsonschema.validate(parsed, schema)
    return parsed, usage


def _llm_via_openrouter(
    system: str, prompt: str, schema: dict, model: str, api_key: str
) -> tuple[dict, dict]:
    """OpenRouter path (OpenAI-compatible chat completions). Schema is embedded in
    the prompt and validated client-side with one retry — same contract as the CLI
    fallback, so it works regardless of OpenRouter's per-model json_schema support."""
    import jsonschema

    # ponytail: internal Anthropic model ids use dashes, OpenRouter slugs use dots
    slug = {"claude-opus-4-8": "anthropic/claude-opus-4.8",
            "claude-sonnet-5": "anthropic/claude-sonnet-5"}.get(model, f"anthropic/{model}")

    def call(extra: str) -> tuple[dict, dict]:
        out = _post_openrouter(
            {
                "model": slug,
                "max_tokens": 16000,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": (
                        f"{prompt}\n\nRespond with ONLY a JSON object valid against "
                        f"this JSON Schema — no markdown fences, no prose. Every schema "
                        f"field is required and every array item must match its item "
                        f"type exactly:\n{json.dumps(schema)}{extra}"
                    )},
                ],
            },
            api_key,
            timeout=600,
        )
        text = out["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        u = out.get("usage") or {}
        usage = {
            "input_tokens": u.get("prompt_tokens", 0),
            "output_tokens": u.get("completion_tokens", 0),
            "cache_read_input_tokens": 0,
            "via": "openrouter",
        }
        return json.loads(text), usage

    parsed, usage = call("")
    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        parsed, usage = call(
            f"\n\nYour previous attempt failed schema validation: {exc.message}. Fix it."
        )
        jsonschema.validate(parsed, schema)
    return parsed, usage


def _llm_structured(
    system: str,
    prompt: str,
    schema: dict,
    model: str = "claude-opus-4-8",
) -> tuple[dict, dict]:
    """One structured-output call. Returns (parsed_json, usage). Raises RuntimeError
    on refusal/truncation so the step surfaces a clear error instead of a bad draft."""
    import os

    import anthropic

    from backend.config import settings

    key = settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY") or ""
    openrouter_key = (
        os.environ.get("OPENROUTER_API_KEY")
        or (key if key.startswith("sk-or-") else "")
    )
    if openrouter_key:
        return _llm_via_openrouter(system, prompt, schema, model, openrouter_key)
    if not key:
        return _llm_via_claude_cli(system, prompt, schema, model)

    client = anthropic.Anthropic(api_key=key)
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=system,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIStatusError as exc:
        raise _upstream_error("Anthropic", status=exc.status_code, body=str(exc)) from exc
    except anthropic.APIConnectionError as exc:
        raise _upstream_error("Anthropic", exc=exc) from exc
    if resp.stop_reason not in ("end_turn", "stop_sequence"):
        raise RuntimeError(f"LLM stopped with {resp.stop_reason}")
    text = next(b.text for b in resp.content if b.type == "text")
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_read_input_tokens": resp.usage.cache_read_input_tokens,
    }
    return json.loads(text), usage


# --- Side-channel chatbot (NOT part of the gated pipeline) --------------------
# The live per-step assistant. It reads project/step state and returns a text
# reply, but never writes an artifact row — so the HITL-ablation dataset stays
# "LLM at steps 2 & 4 only". Its token spend is deliberately outside the audit
# tuples that steps 2 & 4 record.
_CHAT_SYSTEM = (
    "You are the assessment assistant embedded in the Healthy Community Assessment "
    "System (HCAS) — a gated GIS + generative-AI pipeline that walks an analyst "
    "through an 8-step community-health assessment. Explain the current step and its "
    "AI-drafted artifact, and answer the analyst's questions. Be concise, specific, and "
    "grounded: never invent GIS numbers — cite values that appear in the artifact JSON. "
    "You are a read-only assistant; if the analyst wants the artifact changed, tell them "
    "which field to edit and confirm — you cannot edit or advance the pipeline yourself."
)


def _llm_text(system: str, prompt: str, model: str = "claude-opus-4-8") -> tuple[str, dict]:
    """Free-form (non-structured) LLM call for the chatbot. Mirrors _llm_structured's
    provider selection (OpenRouter -> Anthropic -> claude CLI) but returns plain text."""
    import os

    from backend.config import settings

    key = settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY") or ""
    openrouter_key = (
        os.environ.get("OPENROUTER_API_KEY")
        or (key if key.startswith("sk-or-") else "")
    )
    if openrouter_key:
        slug = {"claude-opus-4-8": "anthropic/claude-opus-4.8",
                "claude-sonnet-5": "anthropic/claude-sonnet-5"}.get(model, f"anthropic/{model}")
        out = _post_openrouter(
            {"model": slug, "max_tokens": 1200, "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ]},
            openrouter_key,
            timeout=120,
        )
        u = out.get("usage") or {}
        return out["choices"][0]["message"]["content"].strip(), {
            "input_tokens": u.get("prompt_tokens", 0),
            "output_tokens": u.get("completion_tokens", 0),
            "cache_read_input_tokens": 0, "via": "openrouter",
        }
    if not key:
        import shutil
        import subprocess

        # Same guard as the step runner: without it a keyless machine crashed
        # with FileNotFoundError -> the unhandled 500 Prof. Shi hit in chat.
        if not shutil.which("claude"):
            raise RuntimeError(_NO_KEY_MSG)
        proc = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json", "--model", model,
             "--append-system-prompt", system, "--max-turns", "1"],
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"claude CLI failed (rc={proc.returncode}): {proc.stderr[-400:]}")
        out = json.loads(proc.stdout)
        u = out.get("usage", {})
        return out["result"].strip(), {
            "input_tokens": u.get("input_tokens", 0),
            "output_tokens": u.get("output_tokens", 0),
            "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
            "via": "claude-cli",
        }

    import anthropic

    client = anthropic.Anthropic(api_key=key)
    try:
        resp = client.messages.create(
            model=model, max_tokens=1200, system=system,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIStatusError as exc:
        raise _upstream_error("Anthropic", status=exc.status_code, body=str(exc)) from exc
    except anthropic.APIConnectionError as exc:
        raise _upstream_error("Anthropic", exc=exc) from exc
    if resp.stop_reason not in ("end_turn", "stop_sequence"):
        raise RuntimeError(f"LLM stopped with {resp.stop_reason}")
    text = next(b.text for b in resp.content if b.type == "text")
    return text, {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_read_input_tokens": resp.usage.cache_read_input_tokens,
    }


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
        fetch_landuse,
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
        "landuse": fetch_landuse,
    }


def _study_area(lat: float, lon: float, radius_m: float = 5000):
    """5km circular study area around the centroid, in EPSG:5070."""
    import geopandas as gpd
    from shapely.geometry import Point

    buffered = (
        gpd.GeoSeries([Point(lon, lat)], crs="EPSG:4326")
        .to_crs("EPSG:5070")
        .buffer(radius_m)
    )
    return gpd.GeoDataFrame(geometry=buffered, crs="EPSG:5070")


def _bbox_from_centroid(lat: float, lon: float, radius_m: float = 5000) -> list[float]:
    west, south, east, north = _study_area(lat, lon, radius_m).to_crs("EPSG:4326").total_bounds
    return [float(west), float(south), float(east), float(north)]


def _run_step_3(project: dict) -> tuple[dict, str | None, dict | None]:
    from concurrent.futures import ThreadPoolExecutor

    from backend.assess.netdiag import friendly_net_reason, take_fetch_errors

    factors = _confirmed(project, 2)["factors"]
    names = sorted({layer for f in factors for layer in METRIC_LAYERS[f["metric"]]})
    lat, lon = _confirmed(project, 1)["centroid"]
    bbox = _bbox_from_centroid(lat, lon)
    fetchers = _layer_fetchers()
    with ThreadPoolExecutor(max_workers=4) as ex:
        gdfs = list(ex.map(lambda n: fetchers[n](tuple(bbox)), names))
    # Failed fetches surface in the artifact (Prof. Shi's blocked-network test:
    # silent empty layers looked like a working run with no data).
    errs = take_fetch_errors()
    inventory = [
        {
            "layer": n,
            "feature_count": int(len(g)),
            "crs": str(g.crs),
            **({"error": e} if (e := errs.get(getattr(fetchers[n], "__name__", ""))) else {}),
        }
        for n, g in zip(names, gdfs)
    ]

    from backend.assess import places

    meta = project["meta"]
    try:
        outcomes = places.fetch_health_outcomes(meta["state_fips"], meta["county_fips"])
    except Exception as exc:  # CDC being down shouldn't block the OSM layers
        logging.getLogger(__name__).warning(f"PLACES fetch failed: {exc!r}")
        outcomes = {"source": "CDC PLACES", "measures": [],
                    "note": f"unavailable this run: {friendly_net_reason(exc)}"}

    return {
        "kind": "layer_inventory",
        "bbox": bbox,
        "radius_m": 5000,
        "layers": inventory,
        "health_outcomes": outcomes,
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
    "the metric registry measures the built environment, and any county health outcomes "
    "provided are model-based county-wide estimates — coarser than the 5km study area, "
    "so treat environment-outcome links as ecological association, not causation.\n\n"
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
    step3 = _confirmed(project, 3)
    inventory = step3["layers"]
    outcome_rows = (step3.get("health_outcomes") or {}).get("measures") or []
    outcomes_block = (
        "\n\nCounty health outcomes (CDC PLACES, age-adjusted prevalence — "
        "county-wide, coarser than the study area):\n"
        + "\n".join(
            f"- {m['label']}: {m['county_pct']}% (US: {m['us_pct']}%)"
            for m in outcome_rows
        )
        if outcome_rows
        else ""
    )
    model = "claude-opus-4-8"
    prompt = (
        f"Study area: {place}.\n"
        f"Assessment project: {project['meta']['title']}.\n\n"
        "Confirmed factors and metrics:\n"
        + "\n".join(f"- {f['factor']} -> {f['metric']}: {f['rationale']}" for f in factors)
        + "\n\nCollected data layers:\n"
        + "\n".join(f"- {l['layer']}: {l['feature_count']} features" for l in inventory)
        + outcomes_block
        + "\n\nDraft the solution framework for this assessment. Where county health "
        "outcomes exceed the US benchmark, connect them to the relevant built-"
        "environment factors."
    )
    draft, usage = _llm_structured(_STEP4_SYSTEM, prompt, _STEP4_SCHEMA, model=model)
    return draft, model, usage


# Step 7 computation: metric -> flat result entries (same key shape app.py's
# /assess uses, so generate_interventions' thresholds apply unchanged).
def _metric_computers() -> dict:
    from backend import assess as a

    return {
        "building_coverage_ratio": lambda L, sa: {
            "building_coverage_ratio": a.compute_building_coverage(L["buildings"], sa)
        },
        "greenspace_coverage": lambda L, sa: (
            lambda g: {"greenspace_coverage_ratio": g["ratio"], "greenspace_area_m2": g["area_m2"]}
        )(a.compute_greenspace_coverage(L["greenspace"], sa)),
        "road_density_km_per_km2": lambda L, sa: {
            "road_density_km_per_km2": a.compute_road_density(L["roads"], sa)
        },
        "far": lambda L, sa: {"far": a.compute_far(L["buildings"], sa)},
        "parking_coverage_ratio": lambda L, sa: {
            "parking_coverage_ratio": a.compute_parking_coverage(L["parking"], sa)
        },
        "pedestrian_density_km_per_km2": lambda L, sa: {
            "pedestrian_density_km_per_km2": a.compute_pedestrian_density(L["sidewalks"], sa)
        },
        "transit_access": lambda L, sa: (
            lambda t: {"transit_stop_count": t["stop_count"], "transit_stop_density_per_km2": t["stop_density_per_km2"]}
        )(a.compute_transit_access(L["transit_stops"], sa)),
        "facility_access": lambda L, sa: a.compute_facility_access(L["facilities"], sa),
        "land_use_mix": lambda L, sa: a.compute_land_use_mix(L["landuse"], sa),
    }


def _run_step_5(project: dict) -> tuple[dict, str | None, dict | None]:
    inv = _confirmed(project, 3)
    return {
        "kind": "gis_database",
        "crs": "EPSG:5070 (CONUS Albers Equal Area)",
        "extent_bbox_4326": inv["bbox"],
        "study_area": f"{inv['radius_m']}m circular buffer around the county centroid",
        "layers": inv["layers"],
        "storage": "layers fetched from OSM per run; artifacts persisted in the audit store",
    }, None, None


def _run_step_6(project: dict) -> tuple[dict, str | None, dict | None]:
    metrics = sorted({f["metric"] for f in _confirmed(project, 2)["factors"]})
    steps = [
        {"action": "compute", "metric": m, "layers": METRIC_LAYERS[m]} for m in metrics
    ]
    steps.append({"action": "generate_interventions", "input": "all computed metrics"})
    steps.append({"action": "assemble_report", "input": "confirmed artifacts of steps 1-7"})
    return {"kind": "workflow", "steps": steps}, None, None


def _run_step_7(project: dict) -> tuple[dict, str | None, dict | None]:
    from concurrent.futures import ThreadPoolExecutor

    from backend.assess import generate_interventions
    from backend.assess.netdiag import take_fetch_errors

    # Execute the human-confirmed workflow: a deleted compute step stays deleted.
    plan = [s for s in _confirmed(project, 6)["steps"] if s["action"] == "compute"]
    metrics_wanted = [s["metric"] for s in plan]
    names = sorted({layer for m in metrics_wanted for layer in METRIC_LAYERS[m]})
    lat, lon = _confirmed(project, 1)["centroid"]
    bbox = tuple(_bbox_from_centroid(lat, lon))
    fetchers = _layer_fetchers()
    with ThreadPoolExecutor(max_workers=4) as ex:
        layers = dict(zip(names, ex.map(lambda n: fetchers[n](bbox), names)))
    errs = take_fetch_errors()
    fetch_errors = {
        n: e for n in names
        if (e := errs.get(getattr(fetchers[n], "__name__", "")))
    }
    study_area = _study_area(lat, lon)
    computers = _metric_computers()
    metrics: dict = {}
    for m in metrics_wanted:
        metrics.update(computers[m](layers, study_area))
    metrics = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in metrics.items()}

    artifact = {
        "kind": "execution_results",
        "metrics": metrics,
        "interventions": generate_interventions(metrics),
    }
    if fetch_errors:
        artifact["fetch_errors"] = fetch_errors
    if layers and all(g.empty for g in layers.values()):
        artifact["map_note"] = (
            "Every data layer came back empty, so the map has no features to "
            "show — check the data-source errors above and your network."
        )
    try:
        from backend.assess import render_assessment_map

        empty = _empty_layer()
        slim = {n: _slim_for_map(g) for n, g in layers.items()}
        # ponytail: stashed under _map_html; run_step pops it into the maps table
        # so the multi-MB HTML never bloats the editable artifact JSON
        html = render_assessment_map(
            study_area,
            slim.get("buildings", empty),
            slim.get("greenspace", empty),
            slim.get("roads", empty),
            metrics,
            parking=slim.get("parking"),
            sidewalks=slim.get("sidewalks"),
            transit_stops=slim.get("transit_stops"),
            facilities=slim.get("facilities"),
        )
        if len(html) > 150_000_000:
            raise RuntimeError(f"rendered map too large ({len(html)/1e6:.0f} MB)")
        artifact["_map_html"] = html
        artifact["map_available"] = True
    except Exception as exc:  # map is a bonus, never fails the step
        logger.warning(f"step 7 map render failed: {exc}")
        artifact["map_available"] = False
    return artifact, None, None


def _slim_for_map(gdf):
    """Geometry-only + 2m simplification: OSM tag columns (hundreds of mostly-NaN
    keys serialized per feature) and full-precision vertices are what made raw
    Franklin County maps ~500 MB."""
    if gdf is None or gdf.empty:
        return gdf
    g = gdf[["geometry"]].copy()
    g["geometry"] = g.geometry.simplify(2.0)
    return g[~g.geometry.is_empty]


def _empty_layer():
    import geopandas as gpd

    return gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")


def get_map(pid: str, db: Path | None = None) -> str:
    db = db or DB_PATH
    with _conn(db) as conn:
        row = conn.execute("SELECT html FROM maps WHERE project_id = ?", (pid,)).fetchone()
    if row is None:
        raise KeyError(f"no map rendered yet for project {pid}")
    return row["html"]


def export_audit_csv(db: Path | None = None) -> str:
    """Every step row across all projects, flattened — the HITL ablation dataset."""
    import csv
    import io

    db = db or DB_PATH
    with _conn(db) as conn:
        rows = conn.execute(
            "SELECT p.id, p.meta_json, a.step, a.status, a.data_json, a.drafted_at, a.confirmed_at"
            " FROM artifacts a JOIN projects p ON p.id = a.project_id ORDER BY p.id, a.step"
        ).fetchall()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([
        "project_id", "title", "step", "status", "drafted_at", "confirmed_at",
        "edited", "edit_seconds", "model", "input_tokens", "output_tokens",
    ])
    for r in rows:
        meta, data = json.loads(r["meta_json"]), json.loads(r["data_json"])
        usage = data.get("usage") or {}
        w.writerow([
            r["id"], meta.get("title"), r["step"], r["status"],
            r["drafted_at"], r["confirmed_at"],
            data.get("edit_delta") is not None, data.get("edit_seconds"),
            data.get("model"), usage.get("input_tokens"), usage.get("output_tokens"),
        ])
    return out.getvalue()


def _run_step_8(project: dict) -> tuple[dict, str | None, dict | None]:
    area = _confirmed(project, 1)
    factors = _confirmed(project, 2)["factors"]
    framework = _confirmed(project, 4)
    results = _confirmed(project, 7)
    cited = {r["title"]: r for refs in REFERENCES.values() for r in refs}

    lines = [
        f"# Healthy Community Assessment: {area.get('place_name', 'study area')}",
        f"\n_Project: {project['meta']['title']}_\n",
        "## Methodology",
        framework.get("framework", ""),
        "\n### Theoretical basis",
        *[f"- **{t['theory']}** — {t['application']}" for t in framework.get("theoretical_basis", [])],
        "\n### Factors and metrics",
        *[f"- {f['factor']} → `{f['metric']}`: {f['rationale']}" for f in factors],
        "\n## Results",
        *[f"- `{k}`: {v}" for k, v in results["metrics"].items()],
    ]
    outcomes = _confirmed(project, 3).get("health_outcomes") or {}
    if outcomes.get("measures"):
        lines += [
            "\n## Health outcomes (county-level)",
            f"_{outcomes['source']}. County-wide estimates — coarser than the study area._\n",
            "| Measure | County | US |",
            "|---|---|---|",
            *[
                f"| {m['label']} | {m['county_pct']}%"
                + (" **(above US)**" if m.get("us_pct") is not None and m["county_pct"] > m["us_pct"] else "")
                + f" | {m['us_pct']}% |"
                for m in outcomes["measures"]
            ],
        ]
    elif outcomes.get("note"):
        lines += ["\n## Health outcomes (county-level)", f"_{outcomes['note']}_"]
    lines += [
        "\n## Recommended interventions",
    ]
    for iv in results["interventions"]:
        lines += [
            f"### {iv['category']} ({iv['severity']})",
            f"{iv['finding']}",
            f"**Recommendation:** {iv['recommendation']}",
            f"_Standard: {iv['standard']}_",
        ]
    if not results["interventions"]:
        lines.append("All computed metrics fall within acceptable ranges.")
    def _strs(key: str) -> list[str]:
        return [x for x in framework.get(key, []) if isinstance(x, str) and x.strip()]

    lines += [
        "\n## Assumptions",
        *[f"- {x}" for x in _strs("assumptions")],
        "\n## Limitations",
        *[f"- {x}" for x in _strs("limitations")],
        "\n## References",
        *[
            f"- {cited[t]['authors']} ({cited[t]['year']}). {t}. _{cited[t]['journal']}_."
            + (f" doi:{cited[t]['doi']}" if cited[t]["doi"] else "")
            for t in _strs("references")
            if t in cited
        ],
    ]
    return {"kind": "report_markdown", "markdown": "\n".join(lines)}, None, None


def _draft(project: dict, step: int) -> tuple[dict, str | None, dict | None]:
    if step == 1:
        return _run_step_1(project["meta"]), None, None
    if step == 2:
        return _run_step_2(project)
    runners = {3: _run_step_3, 4: _run_step_4, 5: _run_step_5, 6: _run_step_6, 7: _run_step_7, 8: _run_step_8}
    return runners[step](project)


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
    map_html = ai_draft.pop("_map_html", None)
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
        if map_html is not None:
            conn.execute(
                "INSERT OR REPLACE INTO maps VALUES (?, ?, ?)", (pid, map_html, time.time())
            )
    return data["ai_draft"]


def run_auto(pid: str, db: Path | None = None) -> dict:
    """AI-only baseline arm of the ablation: draft and auto-confirm steps 1-8
    with zero human edits (edit_seconds=0). Refuses gated projects so the
    gated audit rows always reflect real human review."""
    db = db or DB_PATH
    project = get_project(pid, db)
    if project["meta"]["gate_mode"] != "auto":
        raise ValueError("run_auto requires a project created with gate_mode='auto'")
    done = {s["step"] for s in project["steps"] if s["status"] == "confirmed"}
    for n in range(1, 9):
        if n in done:
            continue
        draft = run_step(pid, n, db=db)
        confirm_step(pid, n, draft, edit_seconds=0.0, db=db)
    return get_project(pid, db)


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


def chat(
    pid: str,
    step: int,
    message: str,
    history: list[dict] | None = None,
    db: Path | None = None,
) -> dict:
    """Answer an analyst's question about a step. Side-channel only: it reads
    project state and calls the LLM for a text reply, but writes NOTHING — the
    artifacts table (and thus every audit tuple) is untouched. Raises KeyError for
    an unknown project (-> 404) and RuntimeError on LLM failure (-> 502)."""
    db = db or DB_PATH
    project = get_project(pid, db)  # KeyError -> unknown project
    title = STEPS[step - 1][2] if 1 <= step <= len(STEPS) else f"step {step}"
    row = next((s for s in project["steps"] if s["step"] == step), None)
    artifact = (row.get("human_final") or row.get("ai_draft")) if row else None

    ctx = [f"Project: {project['meta']['title']}", f"Current step: {step} — {title}"]
    if artifact is not None:
        ctx.append("Current step artifact (JSON):\n" + json.dumps(artifact, indent=1)[:6000])
    else:
        ctx.append("The current step has not been run yet (no artifact drafted).")

    convo = []
    for turn in (history or [])[-6:]:
        who = "User" if turn.get("role") == "user" else "Assistant"
        convo.append(f"{who}: {turn.get('content', '')}")
    convo.append(f"User: {message}")

    prompt = "\n\n".join(ctx) + "\n\n---\n\n" + "\n".join(convo) + "\n\nAssistant:"
    reply, usage = _llm_text(_CHAT_SYSTEM, prompt)
    return {"reply": reply, "usage": usage}
