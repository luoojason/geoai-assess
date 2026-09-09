"""HCAS — the 8-step gated assessment machine.

A code-owned coordinator, not an agent: each step drafts an artifact, a human
confirms (possibly edits) it, and only then may the next step run. Every step
row carries the audit tuple (ai_draft, human_final, edit_delta, edit_seconds)
— the dataset for the HITL-vs-autonomy ablation.

All 8 steps have real runners. The LLM speaks at steps 2 and 4 only; the
rest are deterministic over the artifacts a human already confirmed.
"""
from __future__ import annotations

import copy
import difflib
import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# --- live step progress ------------------------------------------------------
# Written by the step runners while a draft is in flight, read by
# GET /hcas/projects/{pid}/steps/{step}/progress so the wizard can show a real
# percentage (per-layer for the fetch-heavy steps). Cleared when the run ends.
_progress: dict[tuple[str, int], dict] = {}
# Per-layer detail lines shown as a live checklist beneath the bar (Prof. Shi
# 2026-07-27: "show what data layer is being fetched from where … rather than
# just a simple progress bar"). Kept in a SEPARATE registry so set_progress /
# get_progress keep their original (pct, label) contract; the HTTP endpoint
# merges the two, and omits `detail` when empty so an idle poll still returns
# exactly {"pct": 0, "label": ""}.
_progress_detail: dict[tuple[str, int], list] = {}
_progress_lock = threading.Lock()


def set_progress(pid: str, step: int, pct: float, label: str) -> None:
    with _progress_lock:
        _progress[(pid, step)] = {"pct": int(pct), "label": label}


def get_progress(pid: str, step: int) -> dict:
    with _progress_lock:
        return dict(_progress.get((pid, step), {"pct": 0, "label": ""}))


def set_progress_detail(pid: str, step: int, detail: list) -> None:
    """Replace the live per-layer checklist for a running step. Each item is
    {name, source, status: 'fetching'|'done'|'error', count?, note?}."""
    with _progress_lock:
        _progress_detail[(pid, step)] = list(detail)


def get_progress_detail(pid: str, step: int) -> list:
    with _progress_lock:
        return list(_progress_detail.get((pid, step), []))


def clear_progress(pid: str, step: int) -> None:
    with _progress_lock:
        _progress.pop((pid, step), None)
        _progress_detail.pop((pid, step), None)

from backend import kg
from backend.assess.references import REFERENCES
from backend.gis.counties import build_place_name, centroid_of_county, lookup_county

import os as _os

# Beside the code, which is what a laptop install wants. A hosted container
# replaces its image on every deploy, so there the database has to sit on a
# mounted volume instead — HCAS_DB points at it. See DEPLOY.md.
DB_PATH = Path(_os.environ.get("HCAS_DB") or Path(__file__).with_name("hcas.db"))

# Per-project cache of fetched/uploaded layer geometry, keyed by project id —
# same volume-override reasoning as DB_PATH. See _cached_layer below.
LAYER_CACHE_DIR = Path(_os.environ.get("HCAS_LAYER_CACHE") or Path(__file__).with_name("hcas_layer_cache"))

# Wall-clock ceiling for the whole per-step layer fetch. Layers still running
# when it expires are abandoned and reported as timed out, so a step ALWAYS
# returns a confirmable artifact (see _fetch_layers). Raise it with
# HCAS_LAYER_BUDGET_S for a very large county on a slow link.
#
# 900, not 420, since 2026-08-11. A healthy run does not go near either number —
# with one request per layer the whole fetch is ~2 min — but 420 was set when a
# layer meant nine tiled requests, and it is not enough to absorb even one round
# of Overpass being slow. Measured that day on a degraded network: 420 s got 3
# of 8 layers, and the five that were dropped were mid-request, not idle. The
# cost of the higher ceiling is bounded and visible: layers land on the
# checklist one at a time as they arrive, so a slow run shows real progress
# instead of a frozen bar, and nothing is left running when it does expire.
LAYER_FETCH_BUDGET_S = float(_os.environ.get("HCAS_LAYER_BUDGET_S", "900"))

# Extra passes over the layers that failed, inside the same budget. Cheap,
# because osmnx caches every tile that already arrived.
OSM_RETRY_PASSES = int(_os.environ.get("OSM_RETRY_PASSES", "2"))

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

# The flat result keys each registry metric contributes to step 7's metrics
# dict — mirrors _metric_computers(). Used to work out which published numbers
# are downstream of a layer that failed to fetch, so they can be marked as
# not-measured instead of shipped as a plausible 0.0.
METRIC_RESULT_KEYS = {
    "building_coverage_ratio": ["building_coverage_ratio"],
    "greenspace_coverage": ["greenspace_coverage_ratio", "greenspace_area_m2"],
    "road_density_km_per_km2": ["road_density_km_per_km2"],
    "far": ["far", "levels_tagged_pct"],
    "parking_coverage_ratio": ["parking_coverage_ratio"],
    "pedestrian_density_km_per_km2": ["pedestrian_density_km_per_km2"],
    "transit_access": ["transit_stop_count", "transit_stop_density_per_km2"],
    "facility_access": ["facility_count", "facility_density_per_km2"],
    "land_use_mix": ["land_use_mix", "land_use_categories"],
}


def _unmeasured_metric_keys(failed_layers) -> list[str]:
    """Result keys that cannot be measurements because a layer they need failed."""
    failed = set(failed_layers)
    out: set[str] = set()
    for metric, layers in METRIC_LAYERS.items():
        if failed.intersection(layers):
            out.update(METRIC_RESULT_KEYS.get(metric, []))
    return sorted(out)


# Human name, source, and geometry kind for each layer. Surfaced in the data
# inventory so the wizard's layer list can show a real name and a right-click
# "Properties" panel (source, geometry type) without re-deriving from geometry
# — the artifact JSON never carries the geometries themselves. (Prof. Shi
# 2026-07-27: layer list shows names; Properties includes source + CRS.)
LAYER_META = {
    "buildings":     ("Building footprints",  "OpenStreetMap · Overpass API", "polygon"),
    "greenspace":    ("Parks & green space",  "OpenStreetMap · Overpass API", "polygon"),
    "roads":         ("Road network",         "OpenStreetMap · Overpass API", "line"),
    "parking":       ("Parking areas",        "OpenStreetMap · Overpass API", "polygon"),
    "sidewalks":     ("Sidewalks & paths",    "OpenStreetMap · Overpass API", "line"),
    "transit_stops": ("Transit stops",        "OpenStreetMap · Overpass API", "point"),
    "facilities":    ("Community facilities", "OpenStreetMap · Overpass API", "point"),
    "landuse":       ("Land use",             "OpenStreetMap · Overpass API", "polygon"),
}


def _layer_meta(name: str) -> tuple[str, str, str]:
    return LAYER_META.get(name, (name.replace("_", " ").title(), "OpenStreetMap", "vector"))

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
# Step runners — all 8 are real; 2 and 4 call the LLM, the rest compute
# ---------------------------------------------------------------------------

def _run_step_1(meta: dict) -> dict:
    """Typed study-area artifact. bbox-first union so a China pivot is a new
    kind, not a rewrite (ponytail: only us_county implemented).

    Carries a descriptive Census profile (area, population, income where a key
    is configured) — Prof. Shi 2026-08-07 #10: FIPS codes are not information a
    reader needs, and "I tried to ask AI what the population of the specified
    county and it said this is not available from the current data". Putting the
    profile ON the artifact is what makes the assistant able to answer, since it
    reads the step artifact and nothing else.
    """
    import os

    from backend.gis.profile import DEFAULT_AREAL_UNIT, county_profile

    from backend.assess import places

    state, county = meta["state_fips"], meta["county_fips"]
    lat, lon = centroid_of_county(state, county)
    key = os.environ.get("CENSUS_API_KEY") or None
    # PLACES carries the county total population and needs no key, so the
    # headline figure is available from step 1 rather than only at step 3.
    pop = places.fetch_total_population(state, county)
    try:
        profile = county_profile(state, county, population=pop, census_api_key=key)
    except Exception as exc:  # never let a descriptive extra fail the step
        logger.warning(f"county_profile failed: {exc!r}")
        profile = {"unavailable": {"profile": f"could not be fetched ({type(exc).__name__})."}}
    out = {
        "kind": "us_county",
        "state_fips": state,
        "county_fips": county,
        "place_name": build_place_name(state, county),
        "centroid": [lat, lon],
        "extent": "Entire county boundary",
        "areal_unit": meta.get("areal_unit", DEFAULT_AREAL_UNIT),
        "profile": profile,
    }
    if meta.get("location_query"):
        out["location_query"] = meta["location_query"]
    if meta.get("notes"):
        out["notes"] = meta["notes"]
    return out


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
                "been revoked or replaced. To use a different key, delete the .env "
                "file next to START-HCAS and run START-HCAS again (it will ask for a "
                "new key), or edit that .env file directly. Then restart."
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


# `claude -p` gets a turn BUDGET, not a single turn. With --max-turns 1 the run
# fails outright whenever the model spends its one turn on anything but the final
# answer, and it exits non-zero with terminal_reason "max_turns". That is what
# actually broke step 4 on the shipping build (2026-08-10): twice in a row, on a
# machine where `claude` was working perfectly from a terminal.
#
# It also corrects an earlier diagnosis. On 2026-08-09 the same rc=1 was recorded
# as "a transient process failure" and answered with a retry, because the next
# attempt happened to succeed — but it is not transient, it is a budget that is
# too small for the larger prompts, and its likelihood rises with prompt size
# (step 4 carries the theory set and the whole reference database). The retry is
# kept, since the failure is probabilistic rather than certain, but the budget is
# what makes it rare.
CLI_MAX_TURNS = _os.environ.get("HCAS_CLI_MAX_TURNS", "6")


class _CliProcessError(RuntimeError):
    """The `claude` invocation failed as a PROCESS: a non-zero exit, or an exit
    of 0 with an envelope that is not the documented JSON. Both are transient in
    practice and both are retried once.

    Distinct from the model returning a well-formed reply whose CONTENT is wrong
    (bad JSON payload, or valid JSON that fails the schema) — that is answered by
    telling the model what was wrong and asking again, which is useless here.
    """


class _CliReplyError(RuntimeError):
    """The process was fine but the model's reply was not JSON. Retried by
    correcting the model, not by re-running the command."""


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
            # The prompt goes in on STDIN, never as an argv string. Windows caps
            # a command line at 32,767 characters and `full` already carries the
            # JSON schema; once step 4 appends knowledgebase context the pair
            # crosses that line and CreateProcess fails with
            # `FileNotFoundError: [WinError 206] The filename or extension is
            # too long` — a message naming neither the prompt nor the limit.
            # Measured 2026-09-09: step 4 died here every time with
            # KG_ENABLED=true while the same build ran 8/8 clean with grounding
            # off, so it surfaces ONLY on the configuration being demoed. POSIX
            # hosts allow ~2 MB of argv, which is why the Linux box never showed
            # it. `system` stays an argument: it is bounded by the step's own
            # template, and it is `full` that grows without limit.
            ["claude", "-p", "--output-format", "json",
             "--model", model, "--append-system-prompt", system, "--max-turns", CLI_MAX_TURNS],
            input=full,
            capture_output=True, text=True, timeout=600,
            # text=True alone decodes with the locale codepage — cp1252 on a
            # Windows install — which turned every em dash and curly quote in a
            # model reply into mojibake ("â€”"). Seen in the wizard 2026-08-07.
            encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            # A non-zero exit still carries the JSON envelope, and its `subtype`
            # names the cause. Naming it here is what turned "transient, retry
            # it" into "the turn budget was too small" — see CLI_MAX_TURNS.
            subtype = ""
            try:
                subtype = (json.loads(proc.stdout) or {}).get("subtype", "")
            except Exception:
                pass
            raise _CliProcessError(
                f"claude CLI failed (rc={proc.returncode}"
                + (f", {subtype}" if subtype else "")
                + f"): {proc.stderr[-800:]} | {proc.stdout[-400:]}"
            )
        # The ENVELOPE (the CLI's own --output-format json wrapper), not the
        # model's answer. rc=0 with an unreadable envelope was seen live on
        # 2026-08-09 while the identical command succeeded from a shell seconds
        # later, so it is the same transient class as a non-zero exit — and it
        # used to escape as a bare JSONDecodeError, which the route reported as a
        # 400 "Expecting value: line 1 column 1", blaming the analyst.
        try:
            out = json.loads(proc.stdout)
            text = out["result"].strip()
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
            raise _CliProcessError(
                f"claude CLI exited 0 but returned an unreadable envelope "
                f"({type(exc).__name__}); stdout began: {proc.stdout[:400]!r}"
            ) from exc
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        usage = {
            k: out.get("usage", {}).get(k, 0)
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens")
        }
        usage["via"] = "claude-cli"
        # The model's own answer. Wrong content, not a broken process — so this
        # is fixed by telling the model, below, rather than by re-running.
        try:
            return json.loads(text), usage
        except json.JSONDecodeError as exc:
            raise _CliReplyError(f"the reply was not JSON ({exc}); it began: {text[:200]!r}")

    def call_retrying(extra: str) -> tuple[dict, dict]:
        """One logical call, with the process retried once underneath it."""
        try:
            return call(extra)
        except _CliProcessError as exc:
            logger.warning(f"claude CLI failed, retrying once: {exc}")
            time.sleep(2)
            try:
                return call(extra)
            except _CliProcessError as exc2:
                logger.error(f"claude CLI failed twice: {exc2}")
                # Don't send the analyst to check whether `claude` works: when
                # this fired on 2026-08-10 it worked perfectly from a terminal,
                # and the real cause was the turn budget. Name what happened.
                if "max_turns" in str(exc2):
                    raise RuntimeError(
                        f"The AI model ran out of its {CLI_MAX_TURNS}-turn budget on "
                        "this step, twice. Run the step again — or raise "
                        "HCAS_CLI_MAX_TURNS if it keeps happening."
                    ) from exc2
                raise RuntimeError(
                    "The Claude CLI did not return a usable reply, twice in a row. "
                    "Run the step again; if it keeps happening, check that `claude` "
                    "works in a terminal — this terminal window has the raw output."
                ) from exc2

    # A reply the model got wrong is corrected by telling it what was wrong and
    # asking once more — but only once, and whatever it does on that second turn
    # must come back as plain language rather than as a parser's exception text.
    try:
        try:
            parsed, usage = call_retrying("")
        except _CliReplyError as exc:
            # The answer was not JSON at all (prose, or a half-written object).
            # This used to raise straight out and fail the step, bypassing the
            # correction path that a schema failure already had.
            logger.warning(f"claude CLI reply unparseable, correcting: {exc}")
            parsed, usage = call_retrying(
                "\n\nYour previous attempt was not valid JSON. Return the JSON object "
                "alone, with no prose before or after it."
            )
        try:
            jsonschema.validate(parsed, schema)
        except jsonschema.ValidationError as exc:
            parsed, usage = call_retrying(
                f"\n\nYour previous attempt failed schema validation: {exc.message}. Fix it."
            )
            jsonschema.validate(parsed, schema)
    except (_CliReplyError, jsonschema.ValidationError) as exc:
        logger.error(f"claude CLI returned an unusable reply twice: {exc}")
        raise RuntimeError(
            "The AI model's reply could not be used, twice in a row. Run the step "
            "again — this is usually transient."
        ) from exc
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
             "--append-system-prompt", system, "--max-turns", CLI_MAX_TURNS],
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
            encoding="utf-8", errors="replace",  # see _llm_via_claude_cli
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
    # single blocking model call — no true percentage exists; the wizard fills
    # the gap with a time-based estimate
    set_progress(project["id"], 2, 10, "waiting for the AI model…")
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


def _county_geometry(state_fips: str, county_fips: str):
    """The actual county polygon as a one-row GeoDataFrame in EPSG:5070, or None
    if the boundary source is unreachable. Prof. Shi (2026-07-27): a county query
    should assess the WHOLE county, not a 5km disc around its centroid."""
    from backend.gis.collector_lite import collect_county_boundaries

    counties = collect_county_boundaries(state_fips)
    if counties is None or counties.empty:
        return None
    row = counties[counties["GEOID"] == f"{state_fips}{county_fips}"]
    if row.empty:
        return None
    geom = row[["geometry"]].to_crs("EPSG:5070").reset_index(drop=True)
    name = None
    if "NAME" in row.columns:
        try:
            name = str(row.iloc[0]["NAME"])
        except Exception:
            name = None
    geom.attrs["county_name"] = name
    return geom


def _resolve_study_area(project: dict):
    """Return (study_area_5070, bbox_4326, describe: dict). Uses the real county
    polygon when the boundary is reachable, else falls back to the legacy 5km
    disc around the centroid so a run never hard-fails on a boundary outage.

    `describe` carries what the artifact and UI need: a human string, the kind
    ('county' | 'disc'), FIPS, and (county path only) area in km²."""
    meta = project["meta"]
    state, county = meta["state_fips"], meta["county_fips"]
    place = _confirmed(project, 1).get("place_name", "the study area")
    geom = _county_geometry(state, county)
    if geom is not None and not geom.empty:
        bbox = [float(x) for x in geom.to_crs("EPSG:4326").total_bounds]
        area_km2 = round(float(geom.geometry.area.sum()) / 1_000_000, 2)
        return geom, bbox, {
            "kind": "county",
            "text": f"{place} — entire county boundary",
            "fips": f"{state}{county}",
            "area_km2": area_km2,
        }
    lat, lon = _confirmed(project, 1)["centroid"]
    disc = _study_area(lat, lon)
    return disc, _bbox_from_centroid(lat, lon), {
        "kind": "disc",
        "text": "5 km buffer around the county centroid (county boundary unavailable)",
        "fips": f"{state}{county}",
        "radius_m": 5000,
    }


# --- Base map (visible from step 1, Prof. Shi 2026-07-27) ---------------------
# A lightweight locator map of the study area (county boundary over base tiles).
# Cached per FIPS so the wizard's <iframe> and the create-form live preview do
# not re-hit the boundary service on every reload.
_basemap_cache: dict[tuple[str, str], str] = {}
_basemap_lock = threading.Lock()


def basemap_for_county(state_fips: str, county_fips: str, db: Path | None = None) -> str:
    """Rendered HTML for the study-area locator map. Raises ValueError for an
    unknown county, RuntimeError if the boundary cannot be reached."""
    if lookup_county(state_fips, county_fips) is None:
        raise ValueError(f"Unknown county FIPS {state_fips}-{county_fips}")
    key = (state_fips, county_fips)
    with _basemap_lock:
        if key in _basemap_cache:
            return _basemap_cache[key]
    geom = _county_geometry(state_fips, county_fips)
    if geom is None or geom.empty:
        raise RuntimeError(
            "The county boundary map could not be loaded — the boundary service "
            "(Census TIGERweb) was unreachable. Check your internet/VPN and retry."
        )
    place = build_place_name(state_fips, county_fips)
    from backend.assess.renderer import render_base_map

    html = render_base_map(geom, place)
    with _basemap_lock:
        _basemap_cache[key] = html
    return html


# --- Per-project layer cache + bring-your-own-data --------------------------
# Xun Shi 2026-08-14 (#all-aigis): "right now sine our data comes from open
# source platforms, the queue to get the data is very lengthy... We may allow
# the user to download and upload the data if they want to. They may upload
# their own data so that they don't have to download data every time from the
# opensource and they can use their own special data if they want to."
#
# Two effects, both plugged into _fetch_layers below so every caller (steps 3
# and 7) gets them automatically: (1) a layer this project already fetched is
# reused instead of hitting Overpass/Census again — step 7 no longer re-fetches
# what step 3 already has; (2) a layer the user uploads is used in place of any
# fetch at all, and is never silently overwritten by a later auto-fetch.
def _bbox_key(bbox) -> str:
    import hashlib

    return hashlib.sha1(",".join(f"{c:.5f}" for c in bbox).encode()).hexdigest()[:12]


def _project_cache_dir(pid: str) -> Path:
    d = LAYER_CACHE_DIR / pid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _manifest_path(pid: str) -> Path:
    return _project_cache_dir(pid) / "manifest.json"


def _read_manifest(pid: str) -> dict:
    p = _manifest_path(pid)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"manifest for {pid} unreadable, treating as empty: {exc!r}")
        return {}


def _write_manifest(pid: str, manifest: dict) -> None:
    _manifest_path(pid).write_text(json.dumps(manifest, indent=2))


def _user_layer_path(pid: str, layer: str) -> Path:
    return _project_cache_dir(pid) / f"{layer}.user.geojson"


def _fetched_layer_path(pid: str, layer: str, bbox) -> Path:
    return _project_cache_dir(pid) / f"{layer}.{_bbox_key(bbox)}.geojson"


def _cached_layer(pid: str, layer: str, bbox):
    """A GeoDataFrame already on disk for this layer, if any, plus which kind
    it was: user upload beats an auto-fetch cache, and an auto-fetch cache is
    scoped to this exact bbox — redefine the study area and it is a plain
    cache miss, no invalidation bookkeeping required."""
    import geopandas as gpd

    user_path = _user_layer_path(pid, layer)
    if user_path.exists():
        try:
            return gpd.read_file(user_path), "user"
        except Exception as exc:
            logger.warning(f"uploaded layer {layer} for {pid} unreadable: {exc!r}")
    fetched_path = _fetched_layer_path(pid, layer, bbox)
    if fetched_path.exists():
        try:
            return gpd.read_file(fetched_path), "fetched"
        except Exception as exc:
            logger.warning(f"cached layer {layer} for {pid} unreadable: {exc!r}")
    return None, None


def _save_fetched_layer(pid: str, layer: str, bbox, gdf) -> None:
    """Best-effort: a cache directory the process can't write to should not
    fail the run, only skip the cache for next time."""
    try:
        gdf.to_file(_fetched_layer_path(pid, layer, bbox), driver="GeoJSON")
        manifest = _read_manifest(pid)
        manifest.setdefault("layers", {})[layer] = {
            "kind": "fetched", "feature_count": int(len(gdf)), "crs": str(gdf.crs),
            "at": time.time(), "bbox_key": _bbox_key(bbox),
        }
        _write_manifest(pid, manifest)
    except Exception as exc:
        logger.warning(f"could not cache fetched layer {layer} for {pid}: {exc!r}")


def list_data_bundle(pid: str, db: Path | None = None) -> dict:
    """Per-layer cache/upload status for this project — what step 3/7 would use
    right now without any network fetch, and what a download would contain."""
    get_project(pid, db=db)
    return {"layers": _read_manifest(pid).get("layers", {})}


def export_data_bundle(pid: str, db: Path | None = None) -> bytes:
    """Zip every layer currently on disk for this project — fetched-and-cached
    plus any upload — with a manifest, so it can be archived or re-uploaded to
    another project."""
    import io
    import zipfile

    get_project(pid, db=db)
    layers = _read_manifest(pid).get("layers", {})
    if not layers:
        raise KeyError(f"no data cached yet for project {pid} — run step 3 first")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for layer, info in layers.items():
            if info.get("kind") == "user":
                src = _user_layer_path(pid, layer)
            else:
                bbox_key = info.get("bbox_key")
                src = _project_cache_dir(pid) / f"{layer}.{bbox_key}.geojson" if bbox_key else None
            if src is None or not src.exists():
                continue
            zf.writestr(f"{layer}.geojson", src.read_bytes())
        zf.writestr("manifest.json", json.dumps({"project_id": pid, "layers": layers}, indent=2))
    return buf.getvalue()


def import_data_bundle(pid: str, filename: str, content: bytes, db: Path | None = None) -> dict:
    """Accept an upload for step 3's data: a .zip bundle (as produced by
    export_data_bundle, or hand-assembled) or a single .geojson/.json file
    named after the layer it supplies — e.g. facilities.geojson. Each layer is
    validated independently so one bad file doesn't sink the rest, matching
    step 3's own per-layer honest-degradation style."""
    import io
    import zipfile

    import geopandas as gpd

    get_project(pid, db=db)
    known_layers = set(LAYER_META)
    entries: list[tuple[str, bytes]] = []
    if filename.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                for name in zf.namelist():
                    if name.endswith("/") or Path(name).name == "manifest.json":
                        continue
                    entries.append((Path(name).name, zf.read(name)))
        except zipfile.BadZipFile:
            return {"accepted": [], "rejected": [{"file": filename, "reason": "not a valid zip file"}]}
    else:
        entries.append((filename, content))

    accepted, rejected = [], []
    manifest = _read_manifest(pid)
    manifest.setdefault("layers", {})
    for fname, data in entries:
        stem = Path(fname).stem.lower().replace("-", "_").replace(" ", "_")
        if stem not in known_layers:
            rejected.append({
                "file": fname,
                "reason": f"'{stem}' is not a known layer. Name the file one of: "
                          f"{', '.join(sorted(known_layers))} (plus .geojson).",
            })
            continue
        try:
            gdf = gpd.read_file(io.BytesIO(data))
        except Exception as exc:
            rejected.append({"file": fname,
                              "reason": f"could not be read as GeoJSON ({type(exc).__name__})"})
            continue
        if gdf.crs is None:
            rejected.append({"file": fname,
                              "reason": "no coordinate reference system (CRS) set on the file — "
                                        "add one before uploading"})
            continue
        gdf.to_file(_user_layer_path(pid, stem), driver="GeoJSON")
        manifest["layers"][stem] = {
            "kind": "user", "feature_count": int(len(gdf)), "crs": str(gdf.crs),
            "at": time.time(), "original_filename": fname,
        }
        accepted.append({"layer": stem, "feature_count": int(len(gdf)), "crs": str(gdf.crs)})
    _write_manifest(pid, manifest)
    return {"accepted": accepted, "rejected": rejected}


def _clear_project_layer_cache(pid: str) -> None:
    """Wipe every cached/uploaded layer for a project whose geography changed
    — an uploaded facilities.geojson for County A must never silently answer
    for County B after a Back-and-redefine (update_project_scope)."""
    import shutil

    shutil.rmtree(_project_cache_dir(pid), ignore_errors=True)


def clear_data_override(pid: str, layer: str, db: Path | None = None) -> dict:
    """Delete an uploaded layer override so the next run fetches it normally."""
    get_project(pid, db=db)
    path = _user_layer_path(pid, layer)
    manifest = _read_manifest(pid)
    existed = path.exists() or layer in manifest.get("layers", {})
    if not existed:
        raise KeyError(f"no uploaded override for layer {layer!r} on project {pid}")
    path.unlink(missing_ok=True)
    manifest.get("layers", {}).pop(layer, None)
    _write_manifest(pid, manifest)
    return {"cleared": layer}


def _fetch_layers(pid: str, step: int, names: list, bbox, budget_s: float,
                  pct_span: tuple[float, float] = (2.0, 100.0)) -> tuple[dict, dict, list]:
    """Fetch `names` concurrently under a wall-clock budget.

    Returns (results, errors, detail). A layer that has not returned when the
    budget expires is abandoned: it becomes an empty layer with a timed-out
    error, exactly like a refused connection. Nothing here can block forever —
    Prof. Shi 2026-08-07 ("after the data are fetched, the process cannot move
    on") was a step with no upper bound on its own runtime, so the wizard had no
    state to advance to and no way back.

    Each layer's checklist row flips to done/error the moment ITS fetch returns,
    so a failed layer is never displayed as "done · 0 features" while the rest
    are still running.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout, as_completed

    from backend.assess.netdiag import pop_fetch_error, take_fetch_errors

    # Drain anything left in the process-wide registry first. An abandoned fetch
    # from a previous run can still be in flight and record its failure later;
    # without this, that error would be popped by whichever layer of THIS run
    # happens to share the fetcher name, and reported against the wrong data.
    take_fetch_errors()
    fetchers = _layer_fetchers()
    detail = [
        {"name": _layer_meta(n)[0], "layer": n, "source": _layer_meta(n)[1],
         "status": "fetching"}
        for n in names
    ]
    set_progress_detail(pid, step, detail)
    lo, hi = pct_span
    set_progress(pid, step, lo, f"fetching {len(names)} map layers…")

    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    started = time.time()
    deadline = started + budget_s

    # Cache check first: a layer the user uploaded, or one this project already
    # fetched for this exact study area, needs no network call at all — this is
    # what makes step 7 not re-fetch what step 3 just fetched.
    pending = []
    reused = 0
    for n in names:
        gdf, kind = _cached_layer(pid, n, bbox)
        if gdf is None:
            pending.append(n)
            continue
        results[n] = gdf
        reused += 1
        slot = next(d for d in detail if d["layer"] == n)
        slot["status"] = "done"
        slot["count"] = int(len(gdf))
        slot["note"] = ("using the data you uploaded" if kind == "user"
                         else "already fetched this run — reused, not re-fetched")
        if kind == "user":
            slot["source"] = "Your uploaded data"
    if reused:
        set_progress_detail(pid, step, detail)
        set_progress(pid, step, lo + (hi - lo) * reused / max(len(names), 1),
                     f"map layers {reused}/{len(names)} · reused, no fetch needed")

    # Automatic retry of the layers that failed. Overpass queues rather than
    # refuses when busy, so a layer that timed out is usually just unlucky — and
    # retrying is nearly free, because osmnx caches every tile that already
    # arrived (measured 2026-08-08: 0.1 s against 7.2 s cold, 70x). Without this
    # the user had to notice the warning and click Re-run themselves; the machine
    # can do the obvious thing first.
    for attempt in range(1 + OSM_RETRY_PASSES):
        if not pending:
            break
        remaining = deadline - time.time()
        # Only a RETRY needs headroom to be worth starting. The first pass always
        # runs with whatever budget it was given, however small — gating it here
        # meant a short budget fetched nothing at all.
        if attempt and remaining <= 5:
            break
        if attempt:
            logger.info(f"step {step}: retrying {len(pending)} layer(s) "
                        f"({attempt}/{OSM_RETRY_PASSES}), {remaining:.0f}s left")
            for n in pending:
                slot = next(d for d in detail if d["layer"] == n)
                slot["status"] = "fetching"
                slot["note"] = "retrying — anything already fetched is cached"
                slot.pop("count", None)
            set_progress_detail(pid, step, detail)
            time.sleep(min(3.0, max(remaining - 5, 0)))
        _fetch_pass(pid, step, pending, bbox, deadline, fetchers, detail,
                    results, errors, names, pct_span, started)
        pending = [n for n in pending if n in errors]

    return results, errors, detail


def _bounded(fetch, deadline: float):
    """Run a layer fetch with the step's deadline visible inside the fetcher.

    Abandoning a future does not stop the thread behind it — this pool is shut
    down with `wait=False` on purpose — so without this the fetchers had no way
    to know the step had given up on them. Measured 2026-08-11: a server
    reporting no run in progress was still holding 14 Overpass sockets and
    opening more, hours after the step timed out. Overpass allows ~2 slots per
    IP, so those zombies consumed the whole allowance and every RE-run started
    starved, which is exactly why re-running never got further.

    The deadline is thread-local, and cleared on the way out so a pooled worker
    never carries one step's expiry into the next step's fetch.
    """
    from backend.assess import osm_layers

    def run(bbox):
        osm_layers.set_deadline(deadline)
        try:
            return fetch(bbox)
        finally:
            osm_layers.clear_deadline()

    return run


_NOT_ATTEMPTED_NOTE = (
    "not attempted — the step ran out of time while earlier layers were still "
    "fetching, so this one was never requested. Nothing here says the data "
    "source failed. Re-run the step: the layers that did arrive are cached and "
    "return instantly, which frees the time for this one."
)


def _layer_failure_note(began, deadline: float, now: float) -> str:
    """Why a layer has no data — the honest version.

    `began` is stamped when a POOL WORKER picks the layer up, which is not the
    same as a request going out. There are more layers than workers, so a layer
    can be picked up at or after the deadline, find the budget already spent and
    do nothing at all. Measured 2026-09-01 (Adams County OH): such a layer was
    reported as "gave up after 0s — the OpenStreetMap server did not answer".
    Zero seconds is proof the server was never asked, so that sentence is false
    in the one direction that matters — it blames the data source for a
    scheduling outcome. That is the Delaware County falsehood in a new place,
    and the same class of message that sent a field tester after his own DNS.
    """
    if began is None or began >= deadline:
        return _NOT_ATTEMPTED_NOTE
    return (
        f"gave up after {int(now - began)}s — the OpenStreetMap server did not "
        "answer for this area in time. Those servers queue requests when busy, "
        "so this is usually waiting rather than a fault. Re-run the step: every "
        "piece that already arrived is cached and returns instantly, so each "
        "attempt gets further."
    )


def _fetch_pass(pid, step, names, bbox, deadline, fetchers, detail,
                results, errors, all_names, pct_span, started) -> None:
    """One concurrent pass over `names`, updating results/errors/detail in place."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout, as_completed

    from backend.assess.netdiag import pop_fetch_error

    lo, hi = pct_span
    budget_s = max(deadline - time.time(), 1)
    # Overpass hands out ~2 query slots per client, and each layer may now issue
    # several tiled requests of its own (osm_layers._tiled), so four layers in
    # flight just queues behind the rate limiter. Two keeps the pipe full without
    # fighting it. OSM_LAYER_WORKERS raises it for a self-hosted endpoint.
    ex = ThreadPoolExecutor(max_workers=int(_os.environ.get("OSM_LAYER_WORKERS", "2")))
    # When a layer's own fetch began — NOT when the step began. There are more
    # layers than workers, so a layer can sit in the pool queue for the whole
    # budget and never be requested at all. Delaware County OH, 2026-08-12: two
    # slow layers held both workers, five layers were never sent to Overpass,
    # and all seven were reported as "the OpenStreetMap server did not answer",
    # each credited with the step's full 900 s. Only three fetchers had run.
    started_at: dict[str, float] = {}
    start_lock = threading.Lock()

    def _mark_start(name, fetch):
        def run(bbox):
            with start_lock:
                started_at[name] = time.time()
            return fetch(bbox)
        return run

    try:
        futures = {ex.submit(_mark_start(n, _bounded(fetchers[n], deadline)),
                             tuple(bbox)): n
                   for n in names}
        done = len([n for n in all_names if n in results and n not in errors])
        try:
            for fut in as_completed(futures, timeout=budget_s):
                n = futures[fut]
                try:
                    g = fut.result()
                except Exception as exc:  # a fetcher is not supposed to raise
                    logger.warning(f"{n} fetcher raised: {exc!r}")
                    g = _empty_layer()
                    errors[n] = f"the data source failed unexpectedly ({type(exc).__name__})."
                err = pop_fetch_error(getattr(fetchers[n], "__name__", ""))
                if err:
                    errors[n] = err
                else:
                    errors.pop(n, None)   # succeeded on a retry: clear the old failure
                results[n] = g
                done += 1
                slot = next(d for d in detail if d["layer"] == n)
                slot["elapsed_s"] = round(time.time() - started_at.get(n, started), 1)
                if n in errors:
                    slot["status"] = "error"
                    slot["note"] = errors[n]
                else:
                    slot["status"] = "done"
                    slot["count"] = int(len(g))
                    # A real fetch, not a timeout/error: cache it so a later
                    # step (or a re-run) for this same project reuses it
                    # instead of asking Overpass/Census again.
                    _save_fetched_layer(pid, n, bbox, g)
                set_progress_detail(pid, step, detail)
                set_progress(pid, step, lo + (hi - lo) * done / max(len(all_names), 1),
                             f"map layers {done}/{len(all_names)} · {_layer_meta(n)[0]} "
                             + (f"({len(g):,} features)" if n not in errors else "— unavailable"))
        except FutTimeout:
            for fut, n in futures.items():
                if n in results:
                    continue
                fut.cancel()
                results[n] = _empty_layer()
                slot = next(d for d in detail if d["layer"] == n)
                began = started_at.get(n)
                # Never requested — or picked up so late there was no budget left
                # to request it with. Blaming the data source in either case is
                # what made a scheduling problem read as a seven-way outage.
                errors[n] = _layer_failure_note(began, deadline, time.time())
                if errors[n] is _NOT_ATTEMPTED_NOTE:
                    slot.pop("elapsed_s", None)     # no fetch, so no fetch time
                else:
                    slot["elapsed_s"] = round(time.time() - began, 1)
                slot["status"] = "error"
                slot["note"] = errors[n]
            set_progress_detail(pid, step, detail)
            logger.warning(
                f"step {step}: layer budget {budget_s}s expired with "
                f"{len(errors)} layer(s) unfinished"
            )
    finally:
        # Do NOT join abandoned worker threads: a hung Overpass socket would
        # put the block back exactly where it was removed from.
        ex.shutdown(wait=False, cancel_futures=True)


def _run_step_3(project: dict) -> tuple[dict, str | None, dict | None]:
    from backend.assess.netdiag import friendly_net_reason

    factors = _confirmed(project, 2)["factors"]
    names = sorted({layer for f in factors for layer in METRIC_LAYERS[f["metric"]]})
    study_area, bbox, describe = _resolve_study_area(project)
    pid = project["id"]

    results, errs_by_layer, detail = _fetch_layers(
        pid, 3, names, bbox, LAYER_FETCH_BUDGET_S, pct_span=(2.0, 88.0)
    )
    gdfs = [results[n] for n in names]
    # Failed fetches surface in the artifact (Prof. Shi's blocked-network test:
    # silent empty layers looked like a working run with no data).
    inventory = []
    for n, g in zip(names, gdfs):
        label, source, geom_type = _layer_meta(n)
        err = errs_by_layer.get(n)
        slot = next((d for d in detail if d["layer"] == n), {})
        # A layer the user uploaded is not from OpenStreetMap — say so, rather
        # than let the static per-layer source string claim a source that was
        # never actually queried (the report cites this field verbatim).
        source = slot.get("source", source)
        inventory.append({
            "layer": n,
            "name": label,
            "feature_count": int(len(g)),
            "crs": str(g.crs),
            "source": source,
            "geom_type": geom_type,
            **({"fetch_seconds": slot["elapsed_s"]} if slot.get("elapsed_s") is not None else {}),
            **({"error": err} if err else {}),
        })

    from backend.assess import places

    meta = project["meta"]
    set_progress(pid, 3, 90, "county health outcomes — CDC PLACES…")
    try:
        outcomes = places.fetch_health_outcomes(meta["state_fips"], meta["county_fips"])
    except Exception as exc:  # CDC being down shouldn't block the OSM layers
        logging.getLogger(__name__).warning(f"PLACES fetch failed: {exc!r}")
        outcomes = {"source": "CDC PLACES", "measures": [],
                    "note": f"unavailable this run: {friendly_net_reason(exc)}"}

    # The study area as a first-class layer of record, so its attribute table
    # (FIPS, area, population) and properties can be shown like any other layer.
    study_layer = {
        "kind": describe["kind"],
        "name": "Study area",
        "fips": describe.get("fips"),
        "crs": "EPSG:5070 (CONUS Albers Equal Area)",
        "extent_4326": bbox,
        "source": "US Census TIGERweb county boundaries"
                  if describe["kind"] == "county" else "Derived 5 km centroid buffer",
        "geom_type": "polygon",
    }
    if describe.get("area_km2") is not None:
        study_layer["area_km2"] = describe["area_km2"]
    if describe.get("radius_m") is not None:
        study_layer["radius_m"] = describe["radius_m"]
    pop = (outcomes or {}).get("total_population")
    if pop is not None:
        study_layer["population"] = pop

    # Prof. Shi 2026-08-07 #5: the reporting unit is the user's choice, and when
    # its data does not exist the system must SAY so after searching — which is
    # what this does: a real Census query, then a plain-language verdict.
    from backend.gis.profile import DEFAULT_AREAL_UNIT, areal_unit_availability

    set_progress(pid, 3, 95, "searching for the chosen reporting unit…")
    unit = meta.get("areal_unit", DEFAULT_AREAL_UNIT)
    areal = areal_unit_availability(meta["state_fips"], meta["county_fips"], unit)
    if areal.get("available") and unit != "county":
        areal["note"] += (
            " The metrics are reported for the whole county AND tabulated for each "
            "of these units at step 7."
        )

    artifact = {
        "kind": "layer_inventory",
        "bbox": bbox,
        "study_area": study_layer,
        "areal_unit": areal,
        "layers": inventory,
        "health_outcomes": outcomes,
    }
    # If the run was forced onto an out-of-date Overpass mirror, the features
    # are old and every number derived from them is old with them. Carry that
    # onto the artifact so it reaches the screen and the report — a stale
    # measurement that looks current is worse than a missing one.
    from backend.assess.osm_layers import stale_data_warning

    stale = stale_data_warning()
    if stale:
        artifact["data_freshness_warning"] = stale
    if describe["kind"] == "disc":
        artifact["radius_m"] = 5000  # back-compat for the disc-fallback path
    return artifact, None, None


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
    "provided are model-based county-level estimates. Those outcomes cover the same "
    "extent as the county study area, but they are modelled rather than measured and "
    "carry no variation within the county — so they cannot be attributed to any part of "
    "it, and environment-outcome links are ecological association, not causation.\n\n"
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


def _step4_system(kg_context: str = "") -> str:
    """Step-4 system prompt, optionally grounded in Prof. Wang's knowledgebase.

    The theory list stays fixed whether or not the knowledgebase answers. That
    list is the experimental control for the grounding study, so the
    knowledgebase adds *evidence and citable sources* underneath it rather than
    redefining the taxonomy.
    """
    if not kg_context:
        return _STEP4_SYSTEM
    return (
        _STEP4_SYSTEM
        + "\n\n"
        + kg_context
        + "\n\nPrefer the knowledgebase passages above when they bear on a factor, and "
        "cite their source documents by exact title alongside the reference database."
    )


def _step4_schema(extra_titles: list | None = None) -> dict:
    """Step-4 schema with the citable-reference enum widened by knowledgebase sources.

    The enum lock itself is never relaxed — only the set of titles it admits
    grows, and only to documents the knowledgebase actually returned for this
    question.
    """
    if not extra_titles:
        return _STEP4_SCHEMA
    titles = list(_REFERENCE_TITLES)
    titles.extend(t for t in extra_titles if t not in titles)
    schema = copy.deepcopy(_STEP4_SCHEMA)
    schema["properties"]["references"]["items"]["enum"] = titles
    return schema


def _run_step_4(project: dict) -> tuple[dict, str | None, dict | None]:
    place = _confirmed(project, 1).get("place_name", "the study area")
    factors = _confirmed(project, 2)["factors"]
    step3 = _confirmed(project, 3)
    inventory = step3["layers"]
    outcome_rows = (step3.get("health_outcomes") or {}).get("measures") or []
    outcomes_block = (
        "\n\nCounty health outcomes (CDC PLACES, age-adjusted prevalence — modelled at "
        "county level, with no variation within the county):\n"
        + "\n".join(
            f"- {m['label']}: {m['county_pct']}% (US: {m['us_pct']}%)"
            for m in outcome_rows
        )
        if outcome_rows
        else ""
    )
    # Best-effort knowledgebase grounding. It is off unless an operator points
    # HCAS at a running instance, and a query can take minutes, so say so on the
    # progress line and carry on with the built-in theories if it does not answer.
    kg_answer = None
    if kg.enabled():
        set_progress(project["id"], 4, 5, "searching the knowledgebase…")
        kg_answer = kg.ground_framework(
            factors=[f["factor"] for f in factors],
            place=place,
        )
    kg_context = kg.as_prompt_context(kg_answer)

    set_progress(project["id"], 4, 10, "waiting for the AI model…")
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
    draft, usage = _llm_structured(
        _step4_system(kg_context),
        prompt,
        _step4_schema(kg_answer.source_titles if kg_answer else None),
        model=model,
    )
    if kg_answer is not None:
        # Provenance for the audit trail: which index answered, and how well.
        draft["knowledgebase"] = {
            "version": kg_answer.version,
            "method": kg_answer.method,
            "duration_seconds": kg_answer.duration_seconds,
            "source_documents": kg_answer.source_titles,
        }
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
        # compute_far_detail, not compute_far: FAR assumes 1 level for every
        # untagged building, so levels_tagged_pct must travel with it to the
        # report (the METRICS registry promises this flag).
        "far": lambda L, sa: a.compute_far_detail(L["buildings"], sa),
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
    sa = inv.get("study_area") or {}
    # Prefer the step-3 study-area description; fall back for old disc artifacts.
    if sa:
        study_desc = (
            f"{sa.get('name', 'Study area')} · "
            + (f"county boundary ({sa['area_km2']} km²)" if sa.get("kind") == "county"
               else f"{sa.get('radius_m', 5000)} m centroid buffer")
        )
    else:
        study_desc = f"{inv.get('radius_m', 5000)} m circular buffer around the county centroid"
    return {
        "kind": "gis_database",
        "crs": "EPSG:5070 (CONUS Albers Equal Area)",
        "extent_bbox_4326": inv["bbox"],
        "study_area": study_desc,
        "study_area_detail": sa,
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


def _tabulate_units(project, layers, computers, metrics_wanted, fetch_errors, describe,
                    study_area=None) -> dict:
    """Metrics per reporting unit, or a stated reason there are none.

    Always returns a dict carrying `unit` and either `rows` or `note`, so the
    wizard and report never have to guess whether an absent table means "the
    county is the unit", "the data was not published", or "it failed".
    """
    from backend.gis.profile import AREAL_UNITS, DEFAULT_AREAL_UNIT

    meta = project["meta"]
    unit = meta.get("areal_unit", DEFAULT_AREAL_UNIT)
    label = (AREAL_UNITS.get(unit) or {}).get("label", unit)
    pid = project["id"]

    if unit == "county":
        return {"unit": unit, "label": label, "rows": [],
                "note": "The county is the reporting unit, so the figures above are the result."}
    if describe.get("kind") != "county":
        return {"unit": unit, "label": label, "rows": [],
                "note": "The county boundary was unavailable on this run, so the study area is a "
                        "centroid buffer and does not align with any Census unit. Re-run with the "
                        "boundary service reachable to get a per-unit table."}

    # Metrics whose source layer failed are not measurements anywhere, least of
    # all per unit.
    failed_layers = set(fetch_errors)
    usable = [m for m in metrics_wanted if not failed_layers.intersection(METRIC_LAYERS[m])]
    if not usable:
        return {"unit": unit, "label": label, "rows": [],
                "note": "None of the map layers could be fetched on this run, so there is nothing "
                        "to tabulate per unit."}

    # Units that nest in the county (tract, block group, county subdivision) are
    # already inside the fetched extent. A ZCTA is not: it is selected by overlap
    # with the county's bounding box and can lie mostly outside, where no OSM data
    # was fetched — so its full area would go into the denominator against features
    # from only the part we covered. Clip those, and disclose it in the note.
    nests = bool((AREAL_UNITS.get(unit) or {}).get("prefix"))

    set_progress(pid, 7, 86, f"computing metrics per {label.lower()}…")
    try:
        from backend.assess.by_unit import tabulate_by_unit
        from backend.gis.profile import fetch_areal_unit_geometries

        units = fetch_areal_unit_geometries(meta["state_fips"], meta["county_fips"], unit)
        if units is None or units.empty:
            return {"unit": unit, "label": label, "rows": [],
                    "note": f"Census publishes no {label} boundaries for this county, so results "
                            f"stay at the county level."}

        def _tick(done, total):
            set_progress(pid, 7, 86 + 6 * done / max(total, 1),
                         f"{label} {done}/{total}…")

        out = tabulate_by_unit(units, layers, computers, usable, progress=_tick,
                               clip_to=None if nests else study_area)
    except Exception as exc:
        logger.warning(f"per-unit tabulation failed: {exc!r}")
        return {"unit": unit, "label": label, "rows": [],
                "note": f"The per-unit table could not be built this run ({type(exc).__name__}). "
                        f"The county-level figures above are unaffected."}

    result = {
        "unit": unit,
        "label": label,
        "rows": out["rows"],
        "metrics": usable,
        "source": "US Census TIGERweb boundaries; same metric code as the county figures",
    }
    # Notes ACCUMULATE. Each caveat used to assign result["note"], so a run that
    # was both truncated and missing a layer reported only the truncation — the
    # omitted metrics vanished from the one place that would have disclosed them.
    notes: list[str] = []
    skipped = [m for m in metrics_wanted if m not in usable]
    if skipped:
        result["omitted_metrics"] = skipped
        notes.append("Omitted from the per-unit table because their data source failed this "
                     "run: " + ", ".join(skipped) + ".")
    if not nests:
        result["clipped_to_study_area"] = True
        notes.append(f"{label}s cross county lines, so each row covers only the part of it "
                     f"inside this county — the area outside was never surveyed for this "
                     f"assessment and counting it would understate every ratio.")
    if out["truncated"]:
        result["truncated"] = True
        result["unit_count"] = out["unit_count"]
        notes.append(f"This county has {out['unit_count']} {label.lower()}s; the first "
                     f"{len(out['rows'])} are tabulated.")
    if notes:
        result["note"] = " ".join(notes)
    return result


def _run_step_7(project: dict) -> tuple[dict, str | None, dict | None]:
    from backend.assess import generate_interventions

    # Execute the human-confirmed workflow: a deleted compute step stays deleted.
    plan = [s for s in _confirmed(project, 6)["steps"] if s["action"] == "compute"]
    metrics_wanted = [s["metric"] for s in plan]
    names = sorted({layer for m in metrics_wanted for layer in METRIC_LAYERS[m]})
    study_area, bbox_list, describe = _resolve_study_area(project)
    bbox = tuple(bbox_list)
    pid = project["id"]
    results, fetch_errors, detail = _fetch_layers(
        pid, 7, names, bbox, LAYER_FETCH_BUDGET_S, pct_span=(2.0, 65.0)
    )
    layers = {n: results[n] for n in names}
    # study_area resolved above (county polygon, or 5 km disc fallback)
    computers = _metric_computers()
    set_progress(pid, 7, 70, "computing metrics…")
    metrics: dict = {}
    for m in metrics_wanted:
        metrics.update(computers[m](layers, study_area))
    metrics = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in metrics.items()}
    set_progress(pid, 7, 82, "drawing the map…")

    # A failed fetch yields an empty layer, and an empty layer computes a
    # perfectly plausible 0.0. Null those out so nothing downstream — the
    # intervention engine, the wizard, or the exported report — can read
    # "source unreachable" as "this county has none of it".
    unmeasured = [k for k in _unmeasured_metric_keys(fetch_errors) if k in metrics]
    for key in unmeasured:
        metrics[key] = None
    measured = {k: v for k, v in metrics.items() if v is not None}

    artifact = {
        "kind": "execution_results",
        "metrics": metrics,
        # Only measured metrics may produce a public-health finding.
        "interventions": generate_interventions(measured),
        # The extent the metrics were ACTUALLY computed over. Step 3 resolves
        # the study area independently and can disagree (a boundary-service
        # outage silently downgrades a county to a 5 km disc), and step 8 used
        # to take its study-area line from step 3 while taking its numbers from
        # here — an ~18x denominator error with nothing to catch it.
        "study_area": describe,
    }
    if fetch_errors:
        artifact["fetch_errors"] = fetch_errors
    if unmeasured:
        artifact["unmeasured_metrics"] = unmeasured

    # Per-unit tabulation (Prof. Shi 2026-08-07 #5). Choosing a reporting unit
    # that changed nothing about the output would be a control that does nothing
    # — his objection to Re-run in the very next item — so the metrics are
    # computed for each unit, using the same computers as the county figure.
    # Only from layers that actually arrived: a per-tract table built from a
    # failed fetch would spread one silent zero across 28 rows.
    artifact["by_unit"] = _tabulate_units(
        project, layers, computers, metrics_wanted, fetch_errors, describe,
        study_area=study_area,
    )
    if layers and all(g.empty for g in layers.values()):
        artifact["map_note"] = (
            "Every data layer came back empty, so the map has no features to "
            "show — check the data-source errors above and your network."
        )
    try:
        from backend.assess import render_assessment_map
        from backend.assess.renderer import _clip_to_study_area

        empty = _empty_layer()
        slim_pairs = {n: _overview_geoms(g) for n, g in layers.items()}
        slim = {n: pair[0] for n, pair in slim_pairs.items()}
        sample_info = {n: pair[1] for n, pair in slim_pairs.items()}

        # The payload the map draws from — every feature of every layer,
        # bucketed so the frontend module paints only what the camera covers.
        # This is the ONLY tier: there is no sampled overview and no zoom gate
        # any more (see _overview_geoms). Each layer builds independently, so
        # one bad geometry cannot cost the others their detail, and a drawing
        # aid must never fail the step.
        #
        # Clipped to the study area BEFORE building the payload, with the same
        # _clip_to_study_area the renderer uses. OSM layers are fetched by
        # bounding box, so an unclipped payload embeds every feature in the
        # bbox corners that falls outside the county — features the metrics
        # never counted. Left unclipped once already: see
        # _clip_to_study_area's docstring for the 2026-08-09 report this
        # would have reopened.
        from backend.assess.renderer import landuse_class_index

        detail = {}
        for _n in layers:
            try:
                clipped = _clip_to_study_area(layers[_n], study_area)
                # landuse alone carries a per-feature class, so one layer can
                # draw farmland, housing and a quarry in different colours
                # instead of one undifferentiated wash.
                detail[_n] = _detail_payload(
                    clipped,
                    class_col="landuse" if _n == "landuse" else None,
                    classer=landuse_class_index if _n == "landuse" else None,
                )
            except Exception as _exc:          # a drawing aid never fails the step
                logger.warning(f"detail payload for {_n} failed: {_exc}")

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
            landuse=slim.get("landuse"),
            sample_info=sample_info,
            detail_payloads=detail,
        )
        # The 25 MB / 150 MB ceilings below are byte budgets, so they compare
        # against len(html.encode()) rather than len(html). The two are NOT
        # equal today, despite this map's own inputs (county names, metric
        # labels) being plain ASCII: viewport_layers.js's own comments (this
        # module's source is embedded verbatim in the page — see the
        # detail_payloads block below) use em dashes (U+2014, 3 bytes in
        # UTF-8, 1 char), so a char-count already undercounts the real byte
        # size by a small but nonzero amount on every render, not just in
        # some hypothetical future. Encoded once and reused so the fallback
        # path below does not pay for it twice.
        #
        # The 25 MB ceiling itself is checked against what actually ships,
        # not the 21.88 MB / 25.39 MB pair that used to live in this comment
        # and in map_payload.py's docstring — those were measured against a
        # REJECTED design (sampling cap removed, plain folium.GeoJson, no
        # clip/grid/gate) and never described the shipped renderer. What
        # ships (grid-bucketed viewport tiers, clipped to the study area
        # BEFORE the detail payload is built — see the clip above) measures,
        # on the same real Delaware County data (26,393 buildings + 28,865
        # roads + 7,135 sidewalks fetched; 22,953 / 22,759 / 5,388 remain
        # after clipping to the county boundary): 11,077,515 bytes (11.08
        # MB) — see docs/measurements/2026-08-12-map-viewport-verification.md.
        # Franklin County (~60k buildings, ~2.3x Delaware's) projects from
        # that real base to roughly 25 MB — close enough to this ceiling
        # that the fallback below stays load-bearing, not just a margin.
        html_bytes = len(html.encode())
        if html_bytes > 25_000_000 and detail:
            logger.warning(
                f"map HTML {html_bytes/1e6:.0f} MB over the 25 MB ceiling — "
                "re-rendering without embedded detail layers")
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
                # landuse belongs here too. Omitting it made the degraded path
                # drop the Land use layer entirely and silently — reinstating
                # the "the green is missing" report that adding landuse to the
                # renderer exists to close, on precisely the dense counties
                # most likely to trip the ceiling in the first place.
                landuse=slim.get("landuse"),
                sample_info=sample_info,
            )
            html_bytes = len(html.encode())
        if html_bytes > 150_000_000:
            raise RuntimeError(f"rendered map too large ({html_bytes/1e6:.0f} MB)")
        artifact["_map_html"] = html
        artifact["map_available"] = True
    except Exception as exc:  # map is a bonus, never fails the step
        logger.warning(f"step 7 map render failed: {exc}")
        artifact["map_available"] = False
    return artifact, None, None


# There is no feature cap. `_MAP_MAX_FEATURES = 3500` lived here and is gone.
#
# Its stated justification — "dense urban counties return ~60k buildings;
# rendering all of them made the map HTML ~45 MB and could freeze a low-spec
# laptop" — was never measured. When it finally was, on real Delaware County
# data, the uncapped payload came to a third of that. The draw-cost half, which
# the cap's last defence rested on, was then measured too, on New York County,
# the densest case to hand: all 96,294 clipped features build in 202 ms and pan
# in 82-96 ms against a 100 ms budget
# (docs/measurements/2026-08-12-map-payload-sizes.md and
# -map-viewport-verification.md).
#
# What the cap actually cost: it was a fixed COUNT, so the fraction drawn fell
# as density rose — 1 in 2 on rural Adams, 1 in 8 on suburban Delaware, 1 in 58
# on New York County, roughly one building per two city blocks. Reported by a
# user as the map failing to load. A cap calibrated against one county is not a
# cap, and the one tier that remains is SMALLER than the two it replaced
# (12.23 MB vs 13.05 MB), because the old design embedded every feature twice.
_MAP_SIMPLIFY_M = 12.0

# The frame `_overview_geoms` returns is now only the GeoJson fallback for a
# layer whose payload build failed, so it keeps the coarse 12 m tolerance. The
# tier that actually draws uses _DETAIL_SIMPLIFY_M: a typical house is 10-15 m
# across, so 12 m turns every footprint into a blob, and drawing every building
# is pointless if every building is melted.
_DETAIL_SIMPLIFY_M = 1.5


def _overview_geoms(gdf):
    """Geometry-only and coarsely simplified — the GeoJson fallback frame.

    Nothing is capped or sampled any more; every feature survives. This frame
    is what the renderer draws for a layer whose viewport payload failed to
    build, so it must not be a subset of that layer. Stripping to geometry
    still matters: OSM tag columns (hundreds of mostly-NaN keys per feature)
    and full-precision vertices are what made raw Franklin County maps ~500 MB.

    ⚠ `total` and `shown` are counted BEFORE the renderer clips to the study
    area, so both are the unclipped fetch count. Inert today because the
    legend's sampling sentence self-suppresses at stride 1, but wrong by
    construction if sampling ever returns.

    Returns (slimmed, info). `info` is {"shown", "total", "stride"} so the caller
    can state on the map how much of the layer is drawn: a map that silently
    shows 1 building in 8 is read as a failed fetch, which is what happened on
    Delaware County 2026-08-12.
    """
    if gdf is None or gdf.empty:
        return gdf, {"shown": 0, "total": 0, "stride": 1}
    g = gdf[["geometry"]].copy()
    g = g[~g.geometry.is_empty & g.geometry.notna()]
    n = len(g)
    # No sampling. The 3,500-feature cap that used to live here produced a
    # fixed COUNT, so fidelity collapsed as density rose: 1 in 2 on rural
    # Adams, 1 in 8 on suburban Delaware, 1 in 58 on New York County — about
    # one building per two city blocks, which reads as a map that failed to
    # load. Every layer now draws through the viewport tier, which holds all
    # of it; this frame is only the fallback path for a layer whose payload
    # build failed, and a fallback that silently drops 98% of its features is
    # worse than none. `stride` stays in the returned dict, always 1, because
    # the legend's sampling sentence keys off it and self-suppresses at 1.
    stride = 1
    g["geometry"] = g.geometry.simplify(_MAP_SIMPLIFY_M)
    g = g[~g.geometry.is_empty]
    return g, {"shown": len(g), "total": n, "stride": stride}


def _detail_payload(gdf, class_col=None, classer=None):
    """Every feature, lightly simplified, bucketed for viewport rendering.

    This is the tier the legend promises "zoom in and every one in view is
    drawn" for, so silently dropping features here is exactly the bug class
    this whole plan exists to fix. `preserve_topology=True` simplification
    does not collapse even a 1mm square, and measured against the real
    26,393-feature Delaware buildings layer this filter drops zero features —
    but a filter believed never to fire is how silent data loss ships, so the
    pre- and post-filter counts are compared and any gap is logged loudly
    rather than assumed away.
    """
    from backend.assess.map_payload import build_payload

    if gdf is None or gdf.empty:
        return build_payload(None)
    # Keep the class column when one is asked for: it is what lets a single
    # layer draw in several colours (landuse). Everything else is dropped —
    # OSM tag columns are hundreds of mostly-NaN keys per feature.
    keep = ["geometry"] + ([class_col] if class_col and class_col in gdf.columns else [])
    g = gdf[keep].copy()
    # Sampled BEFORE the empty/notna filter, not after: that filter itself
    # can silently drop features (an already-empty or null geometry arriving
    # from upstream), and a count taken after it would never see that drop.
    before = len(g)
    g = g[~g.geometry.is_empty & g.geometry.notna()]
    # preserve_topology=True stated explicitly, not left to shapely's default
    # (which is also True): this docstring's central safety claim — that
    # simplification here cannot collapse a feature to nothing — is a
    # promise about THIS call, and it should not rest on an unstated default
    # that a future shapely version, or a future edit, could quietly change.
    g["geometry"] = g.geometry.simplify(_DETAIL_SIMPLIFY_M, preserve_topology=True)
    g = g[~g.geometry.is_empty]
    after = len(g)
    if after != before:
        logger.warning(
            f"detail payload dropped {before - after} of {before} feature(s) "
            "to an empty/null/simplified-away geometry"
        )
    return build_payload(g, class_col=class_col, classer=classer)


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


def _fmt_report_num(v: float) -> str:
    """A number a planner can read at a glance, for the report's tables.

    `:g` switches to scientific notation above six digits, so a tract's
    greenspace printed as "2.28373e+07" — correct, and unreadable in a document
    meant to be skimmed. Thousands separators for magnitudes, full precision for
    the ratios (which are all well below 1).
    """
    try:
        a = abs(float(v))
    except (TypeError, ValueError):
        return str(v)
    if a >= 1000:
        return f"{v:,.0f}"
    if a >= 1:
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    return f"{v:.6g}"


def _run_step_8(project: dict) -> tuple[dict, str | None, dict | None]:
    area = _confirmed(project, 1)
    factors = _confirmed(project, 2)["factors"]
    framework = _confirmed(project, 4)
    results = _confirmed(project, 7)
    cited = {r["title"]: r for refs in REFERENCES.values() for r in refs}

    step3 = _confirmed(project, 3)
    sa3 = (step3.get("study_area") or {}) if isinstance(step3, dict) else {}
    # The numbers below come from step 7, so the study-area line must describe
    # the extent step 7 ACTUALLY analysed. Step 3 resolves the study area on its
    # own run and a boundary-service outage between the two silently downgrades
    # a whole county to a 5 km disc — the report used to keep quoting step 3's
    # county area over disc-computed densities.
    sa7 = results.get("study_area") or {}
    sa = sa7 or sa3

    def _describe_area(d: dict) -> str:
        if d.get("kind") == "county":
            return (
                f"{area.get('place_name', 'study area')} — entire county boundary"
                + (f" ({d['area_km2']} km²)" if d.get("area_km2") is not None else "")
            )
        if d.get("kind") == "disc":
            radius_km = (d.get("radius_m") or 5000) / 1000
            return f"{radius_km:g} km buffer around the county centroid"
        return "not recorded"

    study_line = ""
    if sa:
        study_line = f"\n_Study area: {_describe_area(sa)}._\n"
    study_warnings: list[str] = []
    if not sa7:
        study_warnings.append(
            "This run predates study-area recording on the analysis step, so the "
            "extent above is the one planned at step 3 — it was not verified "
            "against the extent the metrics were computed over. Re-run step 7 to "
            "confirm it."
        )
    elif sa3 and sa3.get("kind") != sa7.get("kind"):
        study_warnings.append(
            f"The analysis step fell back to a different study area than the one "
            f"confirmed at step 3 (planned: {_describe_area(sa3)}; analysed: "
            f"{_describe_area(sa7)}). The boundary service was most likely "
            "unreachable on one of the two runs. Every density and coverage "
            "figure below uses the ANALYSED area as its denominator — re-run "
            "steps 3 and 7 together before citing these numbers."
        )

    # Prof. Shi 2026-07-27: the report must include maps. The interactive study-
    # area map is embedded in the HTML export (Download HTML) and shown in the
    # app; markdown carries this section so the map has a home in every format.
    map_section = ["\n## Study area map"]
    if results.get("map_available"):
        map_section.append(
            "An interactive map of the study area and its data layers "
            "(buildings, green space, roads, facilities, and more) is included in "
            "the HTML export of this report and shown in the assessment app. Each "
            "layer can be toggled on or off."
        )
    else:
        map_section.append(
            "_The map could not be rendered on the last analysis run — re-run "
            "step 7 with a working connection to include it._"
        )

    # --- Data quality -------------------------------------------------------
    # Step 7 records which layers failed to fetch, but the exported report used
    # to ignore that key entirely: metrics computed from an empty layer were
    # printed as measurements, and the intervention engine cited APA/EPA/FTA
    # standards against them. This is the academic deliverable — the failure
    # has to appear here, not only in the wizard's step-7 warnbar.
    fetch_errors = results.get("fetch_errors") or {}
    unmeasured = results.get("unmeasured_metrics") or _unmeasured_metric_keys(fetch_errors)
    unmeasured = [k for k in unmeasured if k in results["metrics"]]
    quality: list[str] = []
    if fetch_errors or unmeasured or study_warnings or results.get("map_note"):
        quality.append("\n## Data quality")
    for w in study_warnings:
        quality.append(f"- **Study area:** {w}")
    for layer, reason in sorted(fetch_errors.items()):
        quality.append(f"- **`{layer}` could not be fetched** — {reason}")
    if unmeasured:
        quality.append(
            "- Because of the failure(s) above, these metrics were **not measured** "
            "and are reported as `not measured` rather than as a value: "
            + ", ".join(f"`{k}`" for k in unmeasured)
            + ". No intervention below is derived from them, and an absent finding "
            "for these topics means *unknown*, not *no problem*."
        )
    if results.get("map_note"):
        quality.append(f"- {results['map_note']}")

    def _result_line(k, v) -> str:
        if v is None:
            return f"- `{k}`: not measured — the source data could not be fetched on this run."
        return f"- `{k}`: {v}"

    results_lines = [_result_line(k, v) for k, v in results["metrics"].items()]
    # FAR assumes 1 level for every building OSM does not tag, so it must never
    # stand next to building_coverage_ratio as an independent measurement.
    tagged = results["metrics"].get("levels_tagged_pct")
    if "far" in results["metrics"] and results["metrics"]["far"] is not None:
        if tagged is None:
            results_lines.append(
                "\n_`far` assumes 1 storey for every building without an OSM "
                "`building:levels` tag. No buildings were present in the study "
                "area, so the tagged share is undefined._"
            )
        else:
            results_lines.append(
                f"\n_`far` assumes 1 storey for every building without an OSM "
                f"`building:levels` tag; only {tagged}% of buildings here carry one "
                f"(`levels_tagged_pct`). At that coverage FAR is an assumed lower "
                f"bound close to `building_coverage_ratio`, not an independent "
                f"measurement of vertical density._"
            )

    lines = [
        f"# Healthy Community Assessment: {area.get('place_name', 'study area')}",
        f"\n_Project: {project['meta']['title']}_",
        study_line,
        *quality,
        "\n## Methodology",
        framework.get("framework", ""),
        *map_section,
        "\n### Theoretical basis",
        *[f"- **{t['theory']}** — {t['application']}" for t in framework.get("theoretical_basis", [])],
        "\n### Factors and metrics",
        *[f"- {f['factor']} → `{f['metric']}`: {f['rationale']}" for f in factors],
        "\n## Results",
        *results_lines,
    ]
    outcomes = _confirmed(project, 3).get("health_outcomes") or {}
    if outcomes.get("measures"):
        lines += [
            "\n## Health outcomes (county-level)",
            f"_{outcomes['source']}. Model-based county-level estimates: they carry no "
            "variation within the county, so they describe the study area as a whole "
            "and cannot be attributed to any part of it._\n",
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
    # Per-unit table (Prof. Shi 2026-08-07 #5). Included in the academic
    # deliverable, not only on screen — and when there is no table, the reason
    # is printed rather than the section silently vanishing.
    by_unit = results.get("by_unit") or {}
    if by_unit:
        unit_label = by_unit.get("label", by_unit.get("unit", "unit"))
        lines.append(f"\n## Results by {unit_label.lower()}")
        rows = by_unit.get("rows") or []
        if not rows:
            lines.append(f"_{by_unit.get('note', 'Not tabulated for this run.')}_")
        else:
            if by_unit.get("note"):
                lines.append(f"_{by_unit['note']}_\n")
            cols: list[str] = []
            for r in rows:
                for k in (r.get("metrics") or {}):
                    if k not in cols:
                        cols.append(k)
            lines.append("| " + " | ".join([unit_label, "km²", *cols]) + " |")
            lines.append("|" + "---|" * (len(cols) + 2))
            for r in rows:
                cells = [str(r.get("name", "")), _fmt_report_num(r.get("area_km2", 0))]
                for c in cols:
                    v = (r.get("metrics") or {}).get(c)
                    cells.append("not measured" if v is None
                                 else _fmt_report_num(v) if isinstance(v, float) else str(v))
                lines.append("| " + " | ".join(cells) + " |")
            lines.append(
                f"\n_Computed with the same metric code as the county figures, clipped to each "
                f"boundary. Source: {by_unit.get('source', 'US Census TIGERweb')}._"
            )

    # Age of the map data, if the run was pushed onto an out-of-date mirror.
    # Sits with Data quality, above the numbers it qualifies.
    stale = (step3.get("data_freshness_warning") if isinstance(step3, dict) else None)
    if stale:
        lines.insert(1, f"\n> **Out-of-date map data.** {stale}\n")

    lines += [
        "\n## Recommended interventions",
    ]
    # The thresholds behind these findings are URBAN-NEIGHBOURHOOD standards
    # (APA, EPA Smart Growth, FTA, ULI), and they were written for the 5 km disc
    # this system originally assessed. Since 2026-07-27 the study area is an
    # entire county, so a rural one trips several of them by construction — its
    # road, sidewalk and transit densities are low because most of it is farmland,
    # not because of a planning failure. Say so once, here, rather than let a
    # column of "high severity" rows imply a verdict the thresholds cannot support.
    if sa.get("kind") == "county":
        lines.append(
            "_These thresholds are urban-neighborhood planning standards, applied here "
            "to an entire county. A largely rural county will fall below the density "
            "thresholds because most of its land is not settled — read a finding below "
            "as a prompt to look at the settled parts, not as a county-wide verdict._\n"
        )
    for iv in results["interventions"]:
        lines += [
            f"### {iv['category']} ({iv['severity']})",
            f"{iv['finding']}",
            f"**Recommendation:** {iv['recommendation']}",
            f"_Standard: {iv['standard']}_",
        ]
    if not results["interventions"]:
        lines.append(
            "All measured metrics fall within acceptable ranges."
            + (
                " This does NOT cover the metrics listed as not measured under "
                "Data quality above — those were never evaluated."
                if unmeasured else ""
            )
        )
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
    areal_unit: str | None = None,
    location_query: str = "",
    notes: str = "",
) -> str:
    from backend.gis.profile import AREAL_UNITS, DEFAULT_AREAL_UNIT

    db = db or DB_PATH
    if lookup_county(state_fips, county_fips) is None:
        raise ValueError(f"Unknown county FIPS {state_fips}-{county_fips}")
    if gate_mode not in ("gated", "auto"):
        raise ValueError("gate_mode must be 'gated' or 'auto'")
    unit = areal_unit or DEFAULT_AREAL_UNIT
    if unit not in AREAL_UNITS:
        raise ValueError(f"areal_unit must be one of {', '.join(AREAL_UNITS)}")
    # ponytail: gate_mode is stored only; the auto path lands with the M2 ablation
    pid = uuid.uuid4().hex[:12]
    meta = {
        "title": title,
        "state_fips": state_fips,
        "county_fips": county_fips,
        "gate_mode": gate_mode,
        "areal_unit": unit,
        "location_query": location_query,
        "notes": notes,
        "current_step": 1,
        "created_at": time.time(),
    }
    with _conn(db) as conn:
        conn.execute("INSERT INTO projects VALUES (?, ?)", (pid, json.dumps(meta)))
    return pid


def update_project_scope(
    pid: str,
    title: str | None = None,
    state_fips: str | None = None,
    county_fips: str | None = None,
    areal_unit: str | None = None,
    location_query: str | None = None,
    notes: str | None = None,
    db: Path | None = None,
) -> dict:
    """Redefine an existing project instead of starting a new one.

    Prof. Shi 2026-08-07 #8 asked for a working Back button on step 1 "allowing
    the user to go back to re-define the project". Changing the study area
    invalidates everything drafted from it, so every artifact is marked stale
    (the same signal an edited upstream step already produces) and the step-1
    draft is dropped so it must be re-run against the new scope.
    """
    from backend.gis.profile import AREAL_UNITS

    db = db or DB_PATH
    with _conn(db) as conn:
        row = conn.execute("SELECT meta_json FROM projects WHERE id = ?", (pid,)).fetchone()
        if row is None:
            raise KeyError(pid)
        meta = json.loads(row["meta_json"])
        new_state = state_fips or meta["state_fips"]
        new_county = county_fips or meta["county_fips"]
        if lookup_county(new_state, new_county) is None:
            raise ValueError(f"Unknown county FIPS {new_state}-{new_county}")
        if areal_unit is not None and areal_unit not in AREAL_UNITS:
            raise ValueError(f"areal_unit must be one of {', '.join(AREAL_UNITS)}")

        geo_changed = (new_state, new_county) != (meta["state_fips"], meta["county_fips"])
        scope_changed = geo_changed or (
            areal_unit is not None and areal_unit != meta.get("areal_unit")
        )
        meta.update(state_fips=new_state, county_fips=new_county)
        if title:
            meta["title"] = title
        if areal_unit is not None:
            meta["areal_unit"] = areal_unit
        if location_query is not None:
            meta["location_query"] = location_query
        if notes is not None:
            meta["notes"] = notes
        conn.execute("UPDATE projects SET meta_json = ? WHERE id = ?", (json.dumps(meta), pid))

        stale = 0
        if scope_changed:
            # Step 1 is re-derived from the new scope, so its old draft is not
            # "stale", it is wrong — remove it. Later steps keep their rows so
            # the audit trail survives, flagged stale.
            conn.execute("DELETE FROM artifacts WHERE project_id = ? AND step = 1", (pid,))
            stale = conn.execute(
                "UPDATE artifacts SET status = 'stale' WHERE project_id = ?", (pid,)
            ).rowcount
    if geo_changed:
        _clear_project_layer_cache(pid)
    return {"project_id": pid, "scope_changed": scope_changed, "stale_steps": stale}


def list_projects(db: Path | None = None, limit: int = 50) -> list[dict]:
    """Every saved project, newest first, with enough to recognise it.

    Work has always been persisted step by step, but the only way back in was to
    type the 12-character project id — so closing the tab without writing it down
    lost the project in practice, even though nothing was lost on disk. This is
    what the Open dialog lists.
    """
    db = db or DB_PATH
    with _conn(db) as conn:
        rows = conn.execute("SELECT id, meta_json FROM projects").fetchall()
        arts = conn.execute(
            "SELECT project_id, step, status, data_json, confirmed_at"
            " FROM artifacts ORDER BY project_id, step"
        ).fetchall()

    by_pid: dict[str, list] = {}
    for a in arts:
        by_pid.setdefault(a["project_id"], []).append(a)

    out = []
    for r in rows:
        meta = json.loads(r["meta_json"])
        steps = by_pid.get(r["id"], [])
        confirmed = [s for s in steps if s["status"] == "confirmed"]
        place = None
        for s in steps:
            if s["step"] == 1:
                data = json.loads(s["data_json"])
                art = data.get("human_final") or data.get("ai_draft") or {}
                place = art.get("place_name")
                break
        touched = max(
            [meta.get("created_at") or 0]
            + [s["confirmed_at"] or 0 for s in steps]
        )
        out.append({
            "project_id": r["id"],
            "title": meta.get("title") or "Untitled assessment",
            "place_name": place,
            "created_at": meta.get("created_at"),
            "last_activity": touched,
            "confirmed_steps": len(confirmed),
            "furthest_step": max([s["step"] for s in steps], default=0),
        })
    out.sort(key=lambda p: p["last_activity"] or 0, reverse=True)
    return out[:limit]


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

    try:
        ai_draft, model, usage = _draft(project, step)
    finally:
        clear_progress(pid, step)
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


_INTAKE_SYSTEM = (
    "You are the intake assistant of HCAS (Healthy Community Assessment System), a "
    "step-by-step GIS + AI assessment of community health for a US county. The "
    "analyst has not created a project yet, so you have no project data: answer "
    "questions about what the system does, what the eight steps are (define project, "
    "choose factors, gather data, set methodology, build database, plan workflow, run "
    "analysis, write report), what data it uses (OpenStreetMap built environment, CDC "
    "PLACES county health outcomes, US Census boundaries), and how to define a study "
    "area. Be concise. Never invent measurements for any place — no assessment has "
    "been run. If asked for numbers about a specific area, say the analyst should "
    "create the project and run the steps."
)

_AREA_SYSTEM = (
    "You resolve a free-text description of a place in the United States to the single "
    "US county that contains it. The description may be a ZIP code, a neighborhood or "
    "community name, a city, a landmark ('around Dartmouth-Hitchcock Medical Center'), "
    "or a natural feature. Return the county and state as plain names — the county "
    "WITHOUT the word 'County' (e.g. 'Grafton', 'Franklin', 'St. Louis'), and the state "
    "as its full name. Also return what you understood and, when the place spans more "
    "than one county or you are unsure, say so in `caveat` and pick the county holding "
    "most of it. If the text is not a US place at all, set `county` and `state` to the "
    "empty string and explain in `caveat`."
)

_AREA_SCHEMA = {
    "type": "object",
    "properties": {
        "county": {"type": "string"},
        "state": {"type": "string"},
        "interpretation": {"type": "string"},
        "caveat": {"type": "string"},
    },
    "required": ["county", "state", "interpretation", "caveat"],
    "additionalProperties": False,
}


def _match_county(county_name: str, state_name: str) -> dict | None:
    """Map the model's plain names onto the shipped county table.

    The county list is 3,143 rows — far too large for a schema enum — so the
    model returns names and the MATCH happens here, deterministically. An
    unmatched name is a miss, never a guess at a nearby county.
    """
    from backend.gis.counties import list_counties, list_states

    def norm(s: str) -> str:
        s = (s or "").strip().lower()
        for suffix in (" county", " parish", " borough", " census area", " city and borough",
                       " municipality", " municipio"):
            if s.endswith(suffix):
                s = s[: -len(suffix)]
        return s.replace(".", "").replace("'", "").replace("-", " ").strip()

    state = next((s for s in list_states() if norm(s["state_name"]) == norm(state_name)), None)
    if state is None:
        return None
    target = norm(county_name)
    for c in list_counties(state["state_fips"]):
        if norm(c["county_name"]) == target:
            return c
    return None


def resolve_area(text: str) -> dict:
    """Turn a free-text location into a study area (Prof. Shi 2026-08-07 #4).

    Returns {resolved: bool, state_fips, county_fips, county_name, state_name,
    interpretation, caveat}. `resolved` False means the wizard keeps the manual
    State/County selects — the point of the box is to save typing, never to
    silently assess somewhere the analyst did not ask for.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Describe the location first (ZIP code, community, landmark…).")
    draft, _usage = _llm_structured(
        _AREA_SYSTEM,
        f"Location description: {text}",
        _AREA_SCHEMA,
        model="claude-sonnet-5",  # a lookup, not a methodology decision
    )
    match = _match_county(draft.get("county", ""), draft.get("state", ""))
    if match is None:
        return {
            "resolved": False,
            "interpretation": draft.get("interpretation", ""),
            "caveat": draft.get("caveat")
            or f"Could not match “{draft.get('county')}, {draft.get('state')}” to a US "
               "county. Choose the state and county manually.",
            "query": text,
        }
    return {
        "resolved": True,
        "state_fips": match["state_fips"],
        "county_fips": match["county_fips"],
        "county_name": match["county_name"],
        "state_name": match["state_name"],
        "interpretation": draft.get("interpretation", ""),
        "caveat": draft.get("caveat", ""),
        "query": text,
    }


_REVISE_SYSTEM = (
    "You revise an existing factor list for a healthy-community assessment in "
    "response to one instruction from the analyst. Do NOT redesign the list. Return "
    "only what to ADD and what to REMOVE. Add a factor only if the instruction asks "
    "for it; remove one only if the instruction asks for it. Every added factor must "
    "map to exactly one metric from the registry below. If the instruction asks for "
    "something the registry cannot measure, add nothing and explain in `note`.\n\n"
    "METRIC REGISTRY:\n"
    + "\n".join(f"- {name}: {desc}" for name, desc in METRICS.items())
)

_REVISE_SCHEMA = {
    "type": "object",
    "properties": {
        "add": {
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
        "remove": {"type": "array", "items": {"type": "string"}},
        "note": {"type": "string"},
    },
    "required": ["add", "remove", "note"],
    "additionalProperties": False,
}


def revise_factors(current: dict, instruction: str, place: str = "the study area") -> dict:
    """Apply one analyst instruction to the current factor list, incrementally.

    Prof. Shi 2026-08-07 #11: re-running step 2 redrafted everything, so an
    instruction like "add pollution data" came back with a different list in a
    different order and the selection never converged. The model is therefore
    asked ONLY for a diff, and the merge below is deterministic: factors the
    analyst kept stay, in their original order; requested removals drop; new
    factors are appended at the end. Order can only change if the analyst
    changes it.
    """
    instruction = (instruction or "").strip()
    if not instruction:
        raise ValueError("Type what to change first.")
    factors = list(current.get("factors") or [])
    listing = "\n".join(
        f"- {f['factor']} -> {f['metric']}" for f in factors
    ) or "- (the list is empty)"
    draft, usage = _llm_structured(
        _REVISE_SYSTEM,
        f"Study area: {place}.\n\nCurrent factor list, in order:\n{listing}\n\n"
        f"Analyst instruction: {instruction}",
        _REVISE_SCHEMA,
        model="claude-sonnet-5",
    )

    def key(f: dict) -> str:
        return f"{f.get('factor', '').strip().lower()}|{f.get('metric', '')}"

    drop = {(d or "").strip().lower() for d in draft.get("remove") or []}
    kept = [f for f in factors if f.get("factor", "").strip().lower() not in drop]
    removed = [f["factor"] for f in factors if f.get("factor", "").strip().lower() in drop]

    have = {key(f) for f in kept}
    added = []
    for f in draft.get("add") or []:
        if key(f) in have:
            continue
        have.add(key(f))
        kept.append(f)
        added.append(f["factor"])

    revised = dict(current)
    revised["factors"] = kept
    return {
        "artifact": revised,
        "added": added,
        "removed": removed,
        "note": draft.get("note", ""),
        "usage": usage,
    }


def _clip_json(obj: Any, limit: int) -> str:
    """JSON for the assistant's prompt, with any truncation ANNOUNCED.

    A silently cut blob reads to the model as the complete artifact, so it
    answers "that is not in the data" about a table that was merely chopped off.
    That is exactly the failure Prof. Shi reported in 2026-08-07 #10 — the
    assistant denying a figure it had never been shown — and step 7 reintroduces
    the risk on its own: a per-unit table of up to 400 rows dwarfs this budget,
    and `by_unit` sits near the end of the artifact, so it is the first thing to
    be cut.
    """
    text = json.dumps(obj, indent=1)
    if len(text) <= limit:
        return text
    return text[:limit] + (
        f"\n… [TRUNCATED: this artifact is {len(text):,} characters and only the first "
        f"{limit:,} are shown here. If the analyst asks about something that would "
        "appear further down — a per-unit table, a specific tract or ZIP area — say "
        "the value is in the step's table on screen rather than that it does not exist.]"
    )


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
    # The study area and its Census profile travel with EVERY step's context.
    # Prof. Shi 2026-08-07 #10 asked the assistant for the county's population
    # and was told it was not available — it was simply not in the prompt.
    s1 = next((s for s in project["steps"] if s["step"] == 1), None)
    area = (s1.get("human_final") or s1.get("ai_draft")) if s1 else None
    if area and step != 1:
        ctx.append("Study area and Census profile (JSON):\n" + _clip_json(area, 2500))
    if artifact is not None:
        ctx.append("Current step artifact (JSON):\n" + _clip_json(artifact, 6000))
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


def chat_intake(message: str, history: list[dict] | None = None) -> dict:
    """Answer questions before a project exists (Prof. Shi 2026-08-07 #3:
    "Allow the user to ask AI questions – even before creating a project").
    Same side-channel guarantee: it writes nothing, and it has no project data,
    so its system prompt forbids inventing any."""
    convo = []
    for turn in (history or [])[-6:]:
        who = "User" if turn.get("role") == "user" else "Assistant"
        convo.append(f"{who}: {turn.get('content', '')}")
    convo.append(f"User: {message}")
    reply, usage = _llm_text(_INTAKE_SYSTEM, "\n".join(convo) + "\n\nAssistant:")
    return {"reply": reply, "usage": usage}
