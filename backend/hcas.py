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


# When each in-flight run began (time.time()). Set by run_step for EVERY step,
# including the ones that never report a percentage, so a reloaded browser can
# ask "is step N running?" and get a true answer, and can show the real elapsed
# time instead of restarting its clock at 0:00 when it re-attaches.
_progress_started: dict[tuple[str, int], float] = {}


def mark_started(pid: str, step: int) -> None:
    with _progress_lock:
        _progress_started[(pid, step)] = time.time()


def get_started(pid: str, step: int) -> float | None:
    with _progress_lock:
        return _progress_started.get((pid, step))


def clear_progress(pid: str, step: int) -> None:
    with _progress_lock:
        _progress.pop((pid, step), None)
        _progress_detail.pop((pid, step), None)
        _progress_started.pop((pid, step), None)

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

# The measures step 2 selects from — the built-environment ones mirror
# backend/assess/metrics.py. Steps 2 and 6 prompt against this; step 7 executes
# against the same names.
#
# Until 2026-09-16 this was nine OpenStreetMap measures and nothing else, so
# step 2 listed pollution, housing, income, food, health care, safety and
# broadband as things it "cannot yet measure", and a request to "add pollution
# data" or "demographic and socioeconomic factors" was refused (Prof. Shi and
# Prof. Wang's feedback #6). The community measures below come from public CDC,
# County Health Rankings and EPA data (backend/assess/indicators.py); the two
# exposure proxies are computed from map layers step 3 already fetches.
from backend.assess.indicators import INDICATORS as _INDICATORS

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
    "near_major_road_exposure": "% of buildings within 150 m of a motorway, trunk or primary road — proxy for traffic air pollution and noise exposure (OpenStreetMap)",
    "industrial_land_share": "% of the area mapped as industrial land, quarry or landfill — proxy for industrial pollution sources (OpenStreetMap)",
    **{name: f"{d['desc']} [{d['unit']}; source: {d['source'].upper()}]"
       for name, d in _INDICATORS.items()},
}

# Measure groups, in the order step 2's prompt presents them.
METRIC_DOMAINS = {
    "Built environment (OpenStreetMap, measured for the area)": [
        "building_coverage_ratio", "greenspace_coverage", "road_density_km_per_km2", "far",
        "parking_coverage_ratio", "pedestrian_density_km_per_km2", "transit_access",
        "facility_access", "land_use_mix"],
    "Environmental exposure and pollution": [
        "near_major_road_exposure", "industrial_land_share", "air_pollution_pm25",
        "unhealthy_air_days", "median_aqi", "drinking_water_violation"],
    "Socioeconomic and demographic conditions (census tracts, combined for the area)": [
        "poverty_rate", "unemployment_rate", "no_high_school_diploma", "income_inequality",
        "minority_share", "older_adults_share", "disability_rate", "limited_english",
        "social_vulnerability"],
    "Housing": ["housing_cost_burden", "crowded_housing", "severe_housing_problems",
                "housing_insecurity"],
    "Food, transport and digital access": [
        "food_insecurity", "food_environment_index", "limited_healthy_food_access",
        "no_vehicle_households", "lack_of_transportation", "no_internet_households"],
    "Health care access and capacity": [
        "uninsured_rate", "lack_of_health_insurance", "routine_checkup",
        "primary_care_physicians", "mental_health_providers"],
    "Safety, activity and social cohesion": [
        "injury_deaths", "motor_vehicle_crash_deaths", "exercise_opportunities",
        "social_associations", "loneliness"],
}

# A factor the analyst or the AI considers important but no connected data
# source can measure yet. It is KEPT in the factor list — never refused — and
# carried through to the report as awaiting data.
NEEDS_DATA = "needs_data"

# Which OSM layers each metric needs (step 3 fetches the union). Community
# measures need none; they come from tables (METRIC_SOURCES).
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
    "near_major_road_exposure": ["buildings", "roads"],
    "industrial_land_share": ["landuse"],
    **{name: [] for name in _INDICATORS},
}

# Which public table each community measure is read from.
METRIC_SOURCES = {name: d["source"] for name, d in _INDICATORS.items()}

# What a planner reads instead of an identifier (Prof. Shi 2026-09-16: "Not sure
# what this 'Registry' is" — the wizard showed raw names like greenspace_coverage).
METRIC_LABELS = {
    "building_coverage_ratio": "Building coverage",
    "greenspace_coverage": "Green space coverage",
    "road_density_km_per_km2": "Road density",
    "far": "Floor area ratio",
    "parking_coverage_ratio": "Parking coverage",
    "pedestrian_density_km_per_km2": "Sidewalk and path density",
    "transit_access": "Transit stops",
    "facility_access": "Community facilities",
    "land_use_mix": "Land-use mix",
    "near_major_road_exposure": "Homes near major roads",
    "industrial_land_share": "Industrial land",
    "poverty_rate": "Poverty", "unemployment_rate": "Unemployment",
    "housing_cost_burden": "Housing cost burden", "no_high_school_diploma": "No high school diploma",
    "uninsured_rate": "No health insurance (all ages)", "no_vehicle_households": "Households without a vehicle",
    "no_internet_households": "Households without broadband", "older_adults_share": "Residents 65 and over",
    "disability_rate": "Disability", "limited_english": "Limited English",
    "minority_share": "Racial and ethnic minority residents", "crowded_housing": "Crowded housing",
    "social_vulnerability": "Social vulnerability (percentile)",
    "food_insecurity": "Food insecurity", "housing_insecurity": "Housing insecurity",
    "lack_of_transportation": "Lack of reliable transportation",
    "lack_of_health_insurance": "No health insurance (adults 18–64)",
    "routine_checkup": "Routine checkup in the past year", "loneliness": "Loneliness",
    "air_pollution_pm25": "Fine particle air pollution (PM2.5)",
    "drinking_water_violation": "Drinking water violation",
    "severe_housing_problems": "Severe housing problems",
    "food_environment_index": "Food environment index",
    "limited_healthy_food_access": "Limited access to healthy food",
    "primary_care_physicians": "Primary care physicians",
    "mental_health_providers": "Mental health providers",
    "exercise_opportunities": "Access to exercise opportunities",
    "injury_deaths": "Injury deaths", "motor_vehicle_crash_deaths": "Motor vehicle crash deaths",
    "social_associations": "Social associations", "income_inequality": "Income inequality",
    "unhealthy_air_days": "Unhealthy air days", "median_aqi": "Median air quality index",
    NEEDS_DATA: "Awaiting data",
}


def measure_catalog() -> dict:
    """Every measure with its plain label, description, topic and source — for
    the wizard, so no screen has to show an internal identifier."""
    from backend.assess.indicators import INDICATORS, SOURCES

    domain_of = {m: d for d, ms in METRIC_DOMAINS.items() for m in ms}
    out = {}
    for m, desc in METRICS.items():
        ind = INDICATORS.get(m)
        out[m] = {
            "label": METRIC_LABELS.get(m, m.replace("_", " ").capitalize()),
            "description": ind["desc"] if ind else desc,
            "unit": ind["unit"] if ind else None,
            "domain": domain_of.get(m, "Other"),
            "source": SOURCES[ind["source"]]["short"] if ind else "OpenStreetMap",
        }
    out[NEEDS_DATA] = {"label": METRIC_LABELS[NEEDS_DATA], "description":
                       "Kept in the assessment; no connected data source measures it yet.",
                       "unit": None, "domain": "Awaiting data", "source": None}
    return out

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
    "near_major_road_exposure": ["near_major_road_exposure"],
    "industrial_land_share": ["industrial_land_share"],
    **{name: [name] for name in _INDICATORS},
}


def _metric_layers(metric: str) -> list[str]:
    """Layers a metric needs; none for a community measure or a factor awaiting data."""
    return METRIC_LAYERS.get(metric, [])


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

def _measure_catalog() -> str:
    """The available measures, grouped by topic, as the model sees them."""
    lines = []
    listed = set()
    for domain, names in METRIC_DOMAINS.items():
        lines.append(f"{domain}:")
        for n in names:
            lines.append(f"- {n}: {METRICS[n]}")
            listed.add(n)
    rest = [n for n in METRICS if n not in listed]
    if rest:
        lines.append("Other:")
        lines.extend(f"- {n}: {METRICS[n]}" for n in rest)
    return "\n".join(lines)


# How a factor with no connected data is to be handled, shared by step 2 and by
# the "Change the list" box so the two can never disagree.
_NEEDS_DATA_RULE = (
    f"If a factor is important for this community's health but none of the measures "
    f"below can represent it (for example noise measured directly, crime, water quality "
    f"at the tap, or local survey data), STILL INCLUDE IT: set its metric to "
    f"\"{NEEDS_DATA}\" and say in `data_needed` what data would measure it and where such "
    f"data usually comes from. Never leave out or refuse an important factor because it "
    f"cannot be measured yet. For every other factor, `data_needed` is the empty string."
)

# Plain words only in anything a planner reads. Prof. Shi (2026-09-16): "Not sure
# what this 'Registry' is" — the word came straight out of this prompt.
_PLAIN_WORDS_RULE = (
    "Write every rationale and note for urban planners and public-health researchers: "
    "never say \"registry\", \"schema\", \"enum\" or name internal identifiers; call them "
    "\"the available measures\" or \"the data connected to this system\"."
)

_STEP2_SYSTEM = (
    "You are the methodology engine of HCAS, a healthy-community assessment system "
    "used by urban-planning researchers. Given a study area, select the health-relevant "
    "factors to assess and map each to exactly one of the available measures below. "
    "Cover the determinants of health broadly — the built environment, environmental "
    "exposure and pollution, socioeconomic conditions, housing, food and transport "
    "access, health care, and safety and social cohesion — choosing the measures that "
    "best fit this particular area rather than every measure: a focused list, typically "
    "12 to 20 factors, that an analyst can review in a few minutes (they can add more "
    "afterwards). Ground rationales in "
    "healthy-cities / social-determinants-of-health reasoning. Measures marked county-wide "
    "describe the whole county, not a smaller project area; say so in the rationale when "
    "the study area is smaller than its county. "
    + _NEEDS_DATA_RULE + " " + _PLAIN_WORDS_RULE
    + " Use `notes` for caveats about the selection as a whole.\n\n"
    "AVAILABLE MEASURES:\n" + _measure_catalog()
)

_FACTOR_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "factor": {"type": "string"},
        "metric": {"type": "string", "enum": [*METRICS, NEEDS_DATA]},
        "rationale": {"type": "string"},
        "data_needed": {"type": "string"},
    },
    "required": ["factor", "metric", "rationale", "data_needed"],
    "additionalProperties": False,
}

_STEP2_SCHEMA = {
    "type": "object",
    "properties": {
        "factors": {"type": "array", "items": _FACTOR_ITEM_SCHEMA},
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
CREATE TABLE IF NOT EXISTS ai_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    step INTEGER,                  -- the wizard step the interaction belongs to
    kind TEXT NOT NULL,            -- find_area | suggest_area | place_area | chat | step_draft | revise_factors
    user_input TEXT,               -- JSON: exactly what the user (or the system) sent
    ai_output TEXT,                -- JSON: what the AI returned, or a structured summary of it
    context TEXT,                  -- JSON: area / unit / model, enough to replay the call later
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ai_history_project ON ai_history (project_id, id);
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

def _area_intake(spec: dict, pid: str | None, which: str) -> dict:
    """Step 1's description of one area: the county's Census profile, and — for
    an area smaller than the county — the selected units, their combined area
    and 2020 population, with the union polygon stored for every later step."""
    import os

    from backend.gis.profile import DEFAULT_AREAL_UNIT, county_profile

    from backend.assess import places

    state, county = spec["state_fips"], spec["county_fips"]
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
    county_place = build_place_name(state, county)
    unit = spec.get("areal_unit") or DEFAULT_AREAL_UNIT
    out = {
        "kind": "us_county",
        "state_fips": state,
        "county_fips": county,
        "place_name": county_place,
        "county_place_name": county_place,
        "centroid": [lat, lon],
        "extent": "Entire county boundary",
        "areal_unit": unit,
        "profile": profile,
        "project_area": {"kind": "county", "unit": unit, "label": county_place,
                         "text": f"{county_place} — entire county boundary"},
    }
    if _is_sub_county(spec):
        # Fails the step on purpose when the boundaries cannot be had: the
        # alternative is analysing the whole county under the name of a
        # neighbourhood.
        built = _build_area(spec)
        if pid:
            _store_area(pid, which, built["polygon"])
        summary = built["summary"]
        c = built["polygon"].to_crs("EPSG:4326").geometry.union_all().representative_point()
        out.update({
            "kind": "us_sub_county",
            "place_name": summary["label"],
            "centroid": [c.y, c.x],
            "extent": summary["text"],
            "project_area": summary,
        })
    return out


def _run_step_1(meta: dict, pid: str | None = None) -> dict:
    """Typed study-area artifact. bbox-first union so a China pivot is a new
    kind, not a rewrite (ponytail: only us_county implemented).

    Carries a descriptive Census profile (area, population, income where a key
    is configured) — Prof. Shi 2026-08-07 #10: FIPS codes are not information a
    reader needs, and "I tried to ask AI what the population of the specified
    county and it said this is not available from the current data". Putting the
    profile ON the artifact is what makes the assistant able to answer, since it
    reads the step artifact and nothing else.

    Since 2026-09-16 the area may be smaller than the county (a town, ZIP code,
    tracts, block groups, or a named community built from block groups), and a
    second, "base" area may be defined for comparison; it is described the same
    way under `base`.
    """
    out = _area_intake(_area_spec({"meta": meta}), pid, "project")
    if meta.get("location_query"):
        out["location_query"] = meta["location_query"]
    if meta.get("notes"):
        out["notes"] = meta["notes"]
    if meta.get("base"):
        out["base"] = _area_intake(meta["base"], pid, "base")
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
# QA 2026-09-29: the chat told a tester to "add a new entry to the `factors`
# array", named ids like `near_major_road_exposure`, and answered in raw
# markdown. The analyst never sees the data behind a page, only the page, so
# the assistant must talk about the page.
_CHAT_PLAIN_RULE = (
    "The analyst sees the web page, never the data behind it. Never show or mention "
    "JSON, arrays, fields, keys or ids (such as snake_case names); refer to every "
    "factor, measure, layer or setting by the name the page shows, in plain words. "
    "Never tell the analyst to edit data or add entries. You cannot change anything "
    "yourself — never claim you did. When the analyst wants a change, say what they "
    "can do on the page: on Choose factors, tick or untick a factor, or type the "
    "change into the 'Change the list' box and press 'Apply change'; on any step, "
    "edit the panels shown, press 'Re-run' to draft the step again, 'Confirm' to "
    "accept it and move on, or 'Back' to return to the previous step. Keep formatting "
    "light: short paragraphs, **bold** for emphasis and simple '-' lists at most; no "
    "headings, tables or code."
)

_CHAT_SYSTEM = (
    "You are the assessment assistant embedded in the Healthy Community Assessment "
    "System (HCAS) — a gated GIS + generative-AI pipeline that walks an analyst "
    "through an 8-step community-health assessment. Explain the current step and its "
    "AI-drafted artifact, and answer the analyst's questions. Be concise, specific, and "
    "grounded: never invent GIS numbers — use only values that appear in the step's "
    "data below. "
    + _PLAIN_WORDS_RULE + " "
    + _CHAT_PLAIN_RULE
)


def study_area_interface_note() -> str:
    """What step 1 of the wizard really offers for defining a study area, as a
    system-prompt section (Prof. Shi 2026-11-13 #17). The step-1 AI once told an
    analyst Gary could be defined "as a named town/place" — an option that does
    not exist on the screen. Built from the live reporting-unit list so the two
    cannot drift apart."""
    from backend.gis.profile import AREAL_UNITS

    units = "; ".join(v["label"] for k, v in AREAL_UNITS.items() if k != "custom")
    return (
        "THE STEP-1 SCREEN, and the ONLY ways you may tell the analyst to define a study "
        "area (never suggest an option that is not listed here):\n"
        "- A 'Describe the location' box with a 'Find with AI' button. It understands a "
        "ZIP code, a city or town (it finds the city's Census boundary and selects the block "
        "groups inside it), a neighborhood or community, and a landmark.\n"
        "- 'State' and 'County' lists.\n"
        f"- A 'Reporting unit' list with exactly these choices: {units}. It is the building "
        "block of the project area, and results are reported for each one. There is NO choice "
        "called 'named town/place', 'city', 'neighborhood', 'community' or 'customized'.\n"
        "- With 'Census block group' chosen, a 'Neighborhood or community' box with a "
        "'Generate/Update AI's suggestion' button that picks the block groups for a "
        "neighborhood.\n"
        "- For any reporting unit except County: 'Whole county' or 'Only the ... I choose' "
        "(pick from a list or click the units on the map).\n"
        "- An 'Upload study area' button for the analyst's own boundary (a zipped shapefile "
        "or GeoJSON); the units lying mostly inside it are selected.\n"
        "- A 'Compare with a base area' checkbox for a larger or similar area to compare with.\n"
        "A city such as Gary, Indiana: tell the analyst to type it in the location box and press "
        "'Find with AI' (or choose Census block group and click the block groups on the map, or "
        "upload the city boundary). If a place name could be in more than one state or county, "
        "ask which one."
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


# --- project areas smaller than a county (Prof. Shi 2026-09-16 #1) -----------
_units_cache: dict[tuple[str, str, str], tuple[float, Any]] = {}
_units_lock = threading.Lock()
UNITS_CACHE_S = 6 * 3600


def _unit_features(state_fips: str, county_fips: str, unit: str):
    """The county's Census units of one kind (EPSG:4326, GEOID/NAME/POP100),
    cached for a few hours — the area picker, step 1 and step 7 all ask for the
    same polygons. Raises RuntimeError in plain language when Census cannot be
    reached, because an area cannot be built from boundaries nobody has."""
    from backend.gis import areas
    from backend.gis import profile as gis_profile

    gunit = areas.geometry_unit(unit)
    key = (state_fips, county_fips, gunit)
    with _units_lock:
        hit = _units_cache.get(key)
        if hit and time.time() - hit[0] < UNITS_CACHE_S:
            return hit[1]
    try:
        gdf = gis_profile.fetch_areal_unit_geometries(state_fips, county_fips, gunit)
    except Exception as exc:
        logger.warning(f"unit boundaries {key} failed: {exc!r}")
        gdf = None
    if gdf is None or gdf.empty:
        raise RuntimeError(
            "The Census boundary service (TIGERweb) did not return the "
            f"{gunit.replace('_', ' ')} boundaries for this county. Check the connection "
            "and try again."
        )
    gdf = gdf.copy()
    pops = areas.unit_populations(state_fips, county_fips, gunit,
                                  geoids=list(gdf["GEOID"].astype(str)))
    gdf["POP100"] = gdf["GEOID"].astype(str).map(pops)
    with _units_lock:
        _units_cache[key] = (time.time(), gdf)
    return gdf


def _area_spec(project: dict, which: str = "project") -> dict | None:
    """The area definition for the project itself or for its base area."""
    meta = project["meta"]
    if which == "base":
        return meta.get("base") or None
    return {k: meta.get(k) for k in ("state_fips", "county_fips", "areal_unit",
                                     "area_geoids", "area_name")}


def _is_sub_county(spec: dict | None) -> bool:
    return bool(spec) and (bool(spec.get("area_geoids")) or spec.get("areal_unit") == "custom")


def _area_path(pid: str, which: str) -> Path:
    return LAYER_CACHE_DIR / pid / f"area.{which}.geojson"


def _unit_label(unit: str, plural: bool = False) -> str:
    from backend.gis.profile import AREAL_UNITS

    names = {"county_subdivision": ("town or township", "towns and townships"),
             "zcta": ("ZIP code area", "ZIP code areas"),
             "tract": ("census tract", "census tracts"),
             "block_group": ("block group", "block groups"),
             "custom": ("block group", "block groups"),
             "county": ("county", "counties")}
    s, p = names.get(unit, ((AREAL_UNITS.get(unit) or {}).get("label", unit),) * 2)
    return p if plural else s


def _build_area(spec: dict) -> dict:
    """Everything a sub-county area needs, from its GEOIDs: the union polygon
    (EPSG:5070), the selected units, and the figures step 1 reports."""
    from backend.gis import areas

    state, county, unit = spec["state_fips"], spec["county_fips"], spec["areal_unit"]
    units = _unit_features(state, county, unit)
    chosen = areas.select(units, spec.get("area_geoids") or [])
    polygon = areas.union_area(chosen)
    pops = {str(g): int(p) for g, p in zip(chosen["GEOID"], chosen["POP100"])
            if p is not None and p == p}
    summary = areas.describe_selection(chosen, pops, _unit_label(unit))
    county_place = build_place_name(state, county)
    n = summary["count"]
    if unit == "custom" or (unit == "block_group" and (spec.get("area_name") or "").strip()):
        name = spec.get("area_name") or "Custom area"
        label = f"{name}, {county_place}"
        text = f"{name} — {n} {_unit_label('block_group', n != 1)} selected in {county_place}"
    elif n == 1:
        unit_name = str(chosen.iloc[0].get("NAME") or chosen.iloc[0]["GEOID"])
        label = f"{unit_name}, {county_place}"
        text = f"{unit_name} ({_unit_label(unit)}) in {county_place}"
    else:
        label = f"{n} {_unit_label(unit, True)} in {county_place}"
        text = label
    summary.update({
        "unit_label_plural": _unit_label(unit, True),
        "kind": "custom" if unit == "custom" else "units",
        "unit": unit,
        "geoids": [str(g) for g in chosen["GEOID"]],
        "names": [str(x) for x in chosen.get("NAME", chosen["GEOID"])],
        "name": spec.get("area_name") or "",
        "label": label,
        "text": text,
        "source": "US Census TIGERweb boundaries; 2020 Census population",
    })
    return {"polygon": polygon, "units": chosen, "summary": summary}


def _store_area(pid: str, which: str, polygon) -> None:
    path = _area_path(pid, which)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(polygon.to_crs("EPSG:4326").to_json())


def _load_area(project: dict, which: str):
    """The stored union polygon (EPSG:5070) for a sub-county area; rebuilt from
    the GEOIDs if the file is gone (a copied or pre-2026-09-16 project)."""
    import geopandas as gpd

    path = _area_path(project["id"], which)
    if path.exists():
        try:
            return gpd.read_file(path).to_crs("EPSG:5070")
        except Exception as exc:
            logger.warning(f"stored area {path} unreadable, rebuilding: {exc!r}")
    built = _build_area(_area_spec(project, which))
    _store_area(project["id"], which, built["polygon"])
    return built["polygon"]


def _resolve_study_area(project: dict, which: str = "project"):
    """Return (study_area_5070, bbox_4326, describe: dict). Uses the real county
    polygon when the boundary is reachable, else falls back to the legacy 5km
    disc around the centroid so a run never hard-fails on a boundary outage.

    `describe` carries what the artifact and UI need: a human string, the kind
    ('county' | 'disc' | 'units' | 'custom'), FIPS, and area in km².

    A sub-county area (2026-09-16) has no disc fallback: it is exactly the
    units the analyst chose, and a 5 km disc would silently measure somewhere
    else. Its polygon is stored at step 1, so later steps need no network for it.
    `which="base"` resolves the comparison area the same way.
    """
    spec = _area_spec(project, which)
    if _is_sub_county(spec):
        polygon = _load_area(project, which)
        bbox = [float(x) for x in polygon.to_crs("EPSG:4326").total_bounds]
        s1 = _confirmed(project, 1) if any(s["step"] == 1 for s in project["steps"]) else {}
        summary = ((s1.get("base") or {}) if which == "base" else s1).get("project_area") or {}
        return polygon, bbox, {
            "kind": "custom" if spec["areal_unit"] == "custom" else "units",
            "text": summary.get("text") or f"{len(spec['area_geoids'])} selected "
                                            f"{_unit_label(spec['areal_unit'], True)}",
            "fips": f"{spec['state_fips']}{spec['county_fips']}",
            "area_km2": round(float(polygon.geometry.area.sum()) / 1_000_000, 2),
            "unit": spec["areal_unit"],
            "geoids": list(spec["area_geoids"]),
        }
    if which == "base":
        if not spec:
            raise ValueError("this project has no base area")
        meta = spec
    else:
        meta = project["meta"]
    state, county = meta["state_fips"], meta["county_fips"]
    place = (build_place_name(state, county) if which == "base"
             else _confirmed(project, 1).get("place_name", "the study area"))
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
    lat, lon = (centroid_of_county(state, county) if which == "base"
                else _confirmed(project, 1)["centroid"])
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


def basemap_for_project(pid: str, which: str = "project", db: Path | None = None) -> str:
    """Locator map of a project's own area — the county, or the selected
    sub-areas drawn over it. A sub-county project used to show the whole county
    on every step, which is exactly the "fixed to the whole county" impression
    the 2026-09-16 feedback describes."""
    project = get_project(pid, db=db)
    spec = _area_spec(project, which)
    if not spec:
        raise KeyError(f"project {pid} has no {which} area")
    if not _is_sub_county(spec):
        # A whole-county BASE area is still the base area, not the study area
        # (QA 2026-09-29: the base column's map was captioned "Study area").
        return basemap_for_county(
            spec["state_fips"], spec["county_fips"],
            subtitle="Base area · full county boundary" if which == "base" else None)
    from backend.assess.renderer import render_base_map

    polygon = _load_area(project, which)
    s1 = next((s for s in project["steps"] if s["step"] == 1), None)
    art = (s1.get("human_final") or s1.get("ai_draft") or {}) if s1 else {}
    summary = ((art.get("base") or {}) if which == "base" else art).get("project_area") or {}
    import html as _html

    n = len(spec.get("area_geoids") or [])
    return render_base_map(
        polygon,
        _html.escape(summary.get("label") or build_place_name(spec["state_fips"], spec["county_fips"])),
        subtitle=("Base area" if which == "base" else "Project area")
                 + f" · {n} selected {_unit_label(spec['areal_unit'], n != 1)}",
        outline=_outline_gdf(project, which),
    )


def basemap_for_county(state_fips: str, county_fips: str, db: Path | None = None,
                       subtitle: str | None = None) -> str:
    """Rendered HTML for the study-area locator map. Raises ValueError for an
    unknown county, RuntimeError if the boundary cannot be reached."""
    if lookup_county(state_fips, county_fips) is None:
        raise ValueError(f"Unknown county FIPS {state_fips}-{county_fips}")
    key = (state_fips, county_fips) if subtitle is None else (state_fips, county_fips, subtitle)
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

    html = render_base_map(geom, place, subtitle=subtitle) if subtitle else render_base_map(geom, place)
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


def _user_data_root() -> Path:
    """Where the user's own files live: uploaded layers, step 3 tables, saved
    database packages. Unlike the fetched-layer cache these cannot be fetched
    again, so a hosted server points HCAS_USER_DATA at its persistent volume
    (the container's own disk is wiped on every restart and idle sleep).
    Locally they sit beside the layer cache, as before."""
    env = _os.environ.get("HCAS_USER_DATA")
    return Path(env) if env else LAYER_CACHE_DIR


def _user_layer_path(pid: str, layer: str) -> Path:
    d = _user_data_root() / pid
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{layer}.user.geojson"


def _fetched_layer_path(pid: str, layer: str, bbox) -> Path:
    return _project_cache_dir(pid) / f"{layer}.{_bbox_key(bbox)}.geojson"


def _cached_layer(pid: str, layer: str, bbox, refresh: bool = False):
    """A GeoDataFrame already on disk for this layer, if any, plus which kind
    it was: user upload beats an auto-fetch cache, and an auto-fetch cache is
    scoped to this exact bbox — redefine the study area and it is a plain
    cache miss, no invalidation bookkeeping required.

    After the project's own cache comes the SHARED one (see _shared_layer), so
    a county any project has already fetched is not fetched again. `refresh`
    skips both automatic caches — never an upload, which the user put there on
    purpose."""
    import geopandas as gpd

    user_path = _user_layer_path(pid, layer)
    if user_path.exists():
        try:
            return gpd.read_file(user_path), "user"
        except Exception as exc:
            logger.warning(f"uploaded layer {layer} for {pid} unreadable: {exc!r}")
    if refresh:
        return None, None
    fetched_path = _fetched_layer_path(pid, layer, bbox)
    if fetched_path.exists():
        try:
            return gpd.read_file(fetched_path), "fetched"
        except Exception as exc:
            logger.warning(f"cached layer {layer} for {pid} unreadable: {exc!r}")
    shared = _shared_layer(layer, bbox)
    if shared is not None:
        gdf, fetched_at = shared
        _note_shared_use(pid, layer, bbox, gdf, fetched_at)
        return gdf, "shared"
    return None, None


_manifest_lock = threading.Lock()


def _save_fetched_layer(pid: str, layer: str, bbox, gdf) -> None:
    """Best-effort: a cache directory the process can't write to should not
    fail the run, only skip the cache for next time.

    Called from the fetch worker threads since 2026-09-16, so the manifest's
    read-modify-write is locked — two layers finishing together would otherwise
    each write a manifest missing the other."""
    try:
        gdf.to_file(_fetched_layer_path(pid, layer, bbox), driver="GeoJSON")
        with _manifest_lock:
            manifest = _read_manifest(pid)
            manifest.setdefault("layers", {})[layer] = {
                "kind": "fetched", "feature_count": int(len(gdf)), "crs": str(gdf.crs),
                "at": time.time(), "bbox_key": _bbox_key(bbox),
            }
            _write_manifest(pid, manifest)
    except Exception as exc:
        logger.warning(f"could not cache fetched layer {layer} for {pid}: {exc!r}")
    _save_shared_layer(layer, bbox, gdf)


# --- shared layer cache ------------------------------------------------------
# Prof. Shi 2026-09-16 #2: "Save the retrieved data locally, so that they don't
# have to be re-retrieved when the user comes back to the same project." The
# per-project cache above already did that for one project id — but the same
# counties come back under NEW projects all the time (the Delaware County
# buildings file sat, byte-identical, in four project folders on this machine),
# and every one of those paid the full Overpass fetch again.
#
# So a successful fetch is also filed here by layer and bounding box, and any
# project reuses it — including a smaller area INSIDE it: a neighbourhood
# project in a county that is already cached reads the county's layer and keeps
# the part that meets its own box, with no network at all.
#
# GeoPackage, not GeoJSON: measured 2026-09-16 on a real 7,391-building layer,
# 32 MB and 1.5 s to read as GeoJSON against 3 MB and 0.3 s as GeoPackage.
# Entries expire (HCAS_SHARED_CACHE_DAYS, default 30) so OpenStreetMap edits do
# reach new runs; Re-fetch at step 3 ignores the cache on demand.
SHARED_CACHE_DAYS = float(_os.environ.get("HCAS_SHARED_CACHE_DAYS", "30"))
_shared_lock = threading.Lock()


def _shared_dir(layer: str) -> Path:
    d = LAYER_CACHE_DIR / "_shared" / layer
    d.mkdir(parents=True, exist_ok=True)
    return d


def _shared_index(layer: str) -> dict:
    p = _shared_dir(layer) / "index.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _bbox_contains(outer, inner, tol: float = 1e-6) -> bool:
    return (outer[0] <= inner[0] + tol and outer[1] <= inner[1] + tol
            and outer[2] >= inner[2] - tol and outer[3] >= inner[3] - tol)


def _shared_layer(layer: str, bbox):
    """(GeoDataFrame, fetched_at) from the shared cache covering `bbox`, or None.

    The smallest unexpired entry that CONTAINS the requested box wins, cut down
    to the features that meet the box. Anything unreadable is a miss, never an
    error: the worst a broken cache can do is cost a fetch."""
    import geopandas as gpd
    from shapely.geometry import box as _box

    now = time.time()
    try:
        with _shared_lock:
            index = _shared_index(layer)
        best = None
        for key, e in index.items():
            if now - float(e.get("at", 0)) > SHARED_CACHE_DAYS * 86400:
                continue
            if not _bbox_contains(e["bbox"], bbox):
                continue
            area = (e["bbox"][2] - e["bbox"][0]) * (e["bbox"][3] - e["bbox"][1])
            if best is None or area < best[0]:
                best = (area, key, e)
        if best is None:
            return None
        _, key, entry = best
        path = _shared_dir(layer) / f"{key}.gpkg"
        if not path.exists():
            return None
        gdf = gpd.read_file(path)
        if gdf.crs is None:
            gdf = gdf.set_crs(entry.get("crs") or "EPSG:5070")
        if [round(c, 5) for c in entry["bbox"]] != [round(c, 5) for c in bbox] and len(gdf):
            clip = gpd.GeoSeries([_box(*bbox)], crs="EPSG:4326").to_crs(gdf.crs).iloc[0]
            gdf = gdf.iloc[gdf.sindex.query(clip, predicate="intersects")].copy()
        return gdf, float(entry.get("at", now))
    except Exception as exc:
        logger.warning(f"shared cache read for {layer} failed, fetching instead: {exc!r}")
        return None


def _gpkg_safe(gdf):
    """GeoPackage stores scalars only. OSM rows carry lists (a way's `nodes`)
    and mixed-type tag columns, so anything that is not a plain scalar is kept
    as text rather than failing the write."""
    import pandas as pd

    out = gdf.copy()
    for col in out.columns:
        if col == out.geometry.name:
            continue
        s = out[col]
        if s.dtype == object:
            out[col] = s.map(lambda v: v if (v is None or isinstance(v, str)
                                            or (isinstance(v, float) and pd.isna(v)))
                             else str(v))
    return out


def _save_shared_layer(layer: str, bbox, gdf) -> None:
    try:
        key = _bbox_key(bbox)
        path = _shared_dir(layer) / f"{key}.gpkg"
        tmp = path.with_suffix(".tmp.gpkg")
        _gpkg_safe(gdf).to_file(tmp, driver="GPKG")
        _os.replace(tmp, path)
        with _shared_lock:
            index = _shared_index(layer)
            index[key] = {"bbox": [float(c) for c in bbox], "at": time.time(),
                          "feature_count": int(len(gdf)), "crs": str(gdf.crs)}
            (_shared_dir(layer) / "index.json").write_text(json.dumps(index, indent=1))
    except Exception as exc:
        logger.warning(f"could not add {layer} to the shared cache: {exc!r}")


def _note_shared_use(pid: str, layer: str, bbox, gdf, fetched_at: float) -> None:
    """Record in the project's manifest that a layer came from the shared cache,
    so the data list and the download both know it exists."""
    try:
        with _manifest_lock:
            manifest = _read_manifest(pid)
            manifest.setdefault("layers", {})[layer] = {
                "kind": "shared", "feature_count": int(len(gdf)), "crs": str(gdf.crs),
                "at": fetched_at, "bbox_key": _bbox_key(bbox),
                "bbox": [float(c) for c in bbox],
            }
            _write_manifest(pid, manifest)
    except Exception as exc:
        logger.warning(f"manifest note for shared {layer} failed: {exc!r}")


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
            if info.get("kind") == "shared":
                # Reused from another project's fetch: nothing sits in this
                # project's folder, so export what the step actually read.
                hit = _shared_layer(layer, info.get("bbox") or []) if info.get("bbox") else None
                if hit is not None:
                    zf.writestr(f"{layer}.geojson", hit[0].to_json())
                continue
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
    if _user_data_root() != LAYER_CACHE_DIR:
        shutil.rmtree(_user_data_root() / pid, ignore_errors=True)


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
                  pct_span: tuple[float, float] = (2.0, 100.0),
                  refresh: bool = False) -> tuple[dict, dict, list]:
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
        gdf, kind = _cached_layer(pid, n, bbox, refresh=refresh)
        if gdf is None:
            pending.append(n)
            continue
        results[n] = gdf
        reused += 1
        slot = next(d for d in detail if d["layer"] == n)
        slot["status"] = "done"
        slot["count"] = int(len(gdf))
        slot["note"] = _REUSE_NOTES.get(kind, _REUSE_NOTES["fetched"])
        slot["cached"] = kind
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
                    results, errors, names, pct_span, started, refresh=refresh)
        pending = [n for n in pending if n in errors]

    return results, errors, detail


_REUSE_NOTES = {
    "user": "using the data you uploaded",
    "fetched": "already fetched for this project — reused, not re-fetched",
    "shared": "saved from an earlier download of this area — reused, not re-fetched",
    "inflight": "downloaded moments ago for this area — reused, not re-fetched",
}

# One download per (layer, area) at a time, process-wide. A background prefetch
# and a step can both want the same layer; the second waits for the first and
# reads its result from the cache instead of opening another request.
_inflight: dict[str, tuple[threading.Event, float]] = {}
_inflight_lock = threading.Lock()

class _SlotPool:
    """Overpass hands out about two query slots per client address. Layer
    workers were capped at two PER RUN, which was right while only one run
    fetched at a time — a background prefetch beside a step, or two users at
    once, would open four and turn the allowance into 429s. So the cap is
    process-wide.

    A plain semaphore would reintroduce the starvation fixed on 2026-08-11: an
    abandoned fetch can sit in a socket read long after its step gave up, and
    it would keep its slot while the NEXT run starved. A slot therefore counts
    only until its holder's deadline; past that it is free to be reissued.
    """

    def __init__(self):
        self._cond = threading.Condition()
        self._held: dict[int, float] = {}
        self._next = 0

    def _limit(self) -> int:
        return max(1, int(_os.environ.get("OSM_LAYER_WORKERS", "2")))

    def acquire(self, deadline: float) -> int | None:
        with self._cond:
            while True:
                now = time.time()
                for token, d in list(self._held.items()):
                    if d <= now:
                        del self._held[token]
                if len(self._held) < self._limit():
                    self._next += 1
                    self._held[self._next] = deadline
                    return self._next
                if now >= deadline:
                    return None
                soonest = min(self._held.values())
                self._cond.wait(timeout=max(0.05, min(deadline, soonest) - now))

    def release(self, token: int | None) -> None:
        if token is None:
            return
        with self._cond:
            self._held.pop(token, None)
            self._cond.notify_all()


_overpass_slots = _SlotPool()


def _claim_inflight(key: str, deadline: float) -> tuple[bool, threading.Event, float]:
    """(am I the downloader, the event to wait on, until when it is worth waiting).

    An owner past its own deadline is a download its step already abandoned —
    it may sit in a socket read for another minute — so it is replaced rather
    than waited on. Its eventual release cannot evict the new owner."""
    with _inflight_lock:
        held = _inflight.get(key)
        if held is None or held[1] <= time.time():
            ev = threading.Event()
            _inflight[key] = (ev, deadline)
            return True, ev, deadline
        return False, held[0], held[1]


def _release_inflight(key: str, ev: threading.Event) -> None:
    with _inflight_lock:
        held = _inflight.get(key)
        if held is not None and held[0] is ev:
            del _inflight[key]
    ev.set()


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


_NO_SLOT = object()   # a worker that never got an Overpass slot before the deadline


def _fetch_pass(pid, step, names, bbox, deadline, fetchers, detail,
                results, errors, all_names, pct_span, started, refresh=False) -> None:
    """One concurrent pass over `names`, updating results/errors/detail in place.

    Each worker owns its whole layer since 2026-09-16: it records its own
    failures in a private scope, saves its own successful result to the caches,
    and hands the step (gdf, error, how). Errors used to live in one
    process-wide registry popped by fetcher name, which was safe only while a
    single run fetched at a time. With a background prefetch beside a step, a
    step draining that registry could swallow the prefetch's failure — and the
    prefetch would then cache an EMPTY layer as a real one, to be reused as
    "0 features" by every later run.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout, as_completed

    from backend.assess.netdiag import collect_fetch_errors

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

    def work(name):
        fetch = fetchers[name]
        key = f"{name}|{_bbox_key(bbox)}"
        owner, ev, until = _claim_inflight(key, deadline)
        if not owner:
            # Exactly this layer, for exactly this area, is already downloading
            # (a prefetch, or another user). Wait for it and read its result.
            ev.wait(timeout=max(min(deadline, until) - time.time(), 0))
            gdf, _kind = _cached_layer(pid, name, bbox)
            if gdf is not None:
                return gdf, None, "inflight"
            owner, ev, _ = _claim_inflight(key, deadline)   # it failed: try it ourselves
        try:
            token = _overpass_slots.acquire(deadline)
            if token is None:
                return None, _NO_SLOT, None
            with start_lock:
                started_at[name] = time.time()
            try:
                with collect_fetch_errors() as errs:
                    g = _bounded(fetch, deadline)(tuple(bbox))
            finally:
                _overpass_slots.release(token)
            err = errs.get(getattr(fetch, "__name__", "")) or next(iter(errs.values()), None)
            # Cache only a clean result that arrived inside the budget. A fetch
            # the step had already given up on may have stopped part-way through
            # its tiles, and a partial layer must not be filed as the whole one.
            if err is None and time.time() < deadline:
                _save_fetched_layer(pid, name, bbox, g)
            return g, err, "fetched"
        finally:
            if owner:
                _release_inflight(key, ev)

    try:
        futures = {ex.submit(work, n): n for n in names}
        done = len([n for n in all_names if n in results and n not in errors])
        try:
            for fut in as_completed(futures, timeout=budget_s):
                n = futures[fut]
                how = None
                try:
                    g, err, how = fut.result()
                except Exception as exc:  # a fetcher is not supposed to raise
                    logger.warning(f"{n} fetcher raised: {exc!r}")
                    g = _empty_layer()
                    err = f"the data source failed unexpectedly ({type(exc).__name__})."
                if err is _NO_SLOT:
                    g, err = _empty_layer(), _NOT_ATTEMPTED_NOTE
                if err:
                    errors[n] = err
                else:
                    errors.pop(n, None)   # succeeded on a retry: clear the old failure
                results[n] = g
                done += 1
                slot = next(d for d in detail if d["layer"] == n)
                if err is _NOT_ATTEMPTED_NOTE:
                    slot.pop("elapsed_s", None)
                else:
                    slot["elapsed_s"] = round(time.time() - started_at.get(n, started), 1)
                if n in errors:
                    slot["status"] = "error"
                    slot["note"] = errors[n]
                else:
                    slot["status"] = "done"
                    slot["count"] = int(len(g))
                    if how == "inflight":
                        slot["note"] = _REUSE_NOTES["inflight"]
                        slot["cached"] = "inflight"
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


# --- community data tables (Prof. Shi and Prof. Wang 2026-09-16 #6) ------------

def _tables_dir() -> Path:
    d = LAYER_CACHE_DIR / "_shared" / "tables"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fetch_table(source: str, spec: dict):
    """One public table for an area's county. Raises in plain language."""
    from backend.assess import indicators as ind

    state, county = spec["state_fips"], spec["county_fips"]
    if source == "svi":
        return ind.svi_tracts(state, county, _tables_dir())
    if source == "places":
        return ind.places_tracts(state, county, _tables_dir())
    if source == "chr":
        return ind.chr_counties(_tables_dir())
    if source == "aqi":
        return ind.aqi_counties(_tables_dir())
    raise KeyError(source)


def _fetch_tables(sources, spec: dict) -> tuple[dict, dict]:
    """(tables by source, plain-language errors by source)."""
    from backend.assess.netdiag import friendly_net_reason

    tables, errors = {}, {}
    for src in sources:
        try:
            tables[src] = _fetch_table(src, spec)
        except Exception as exc:
            logger.warning(f"community data {src} failed: {exc!r}")
            if isinstance(exc, (LookupError, RuntimeError)) and not isinstance(exc, KeyError):
                errors[src] = str(exc)
            else:
                errors[src] = f"could not be downloaded — {friendly_net_reason(exc)}"
    return tables, errors


def _table_inventory(sources, tables, errors, spec) -> list[dict]:
    from backend.assess import indicators as ind

    out = []
    for src in sources:
        meta = ind.SOURCES[src]
        entry = {"source": src, "name": meta["name"], "level": meta["level"]}
        if src in errors:
            entry["error"] = errors[src]
        else:
            data = tables[src]
            if src in ("svi", "places"):
                entry["rows"] = len(data)
                entry["note"] = f"{len(data)} census tracts in the county"
            elif src == "chr":
                have = f"{spec['state_fips']}{spec['county_fips']}" in data
                entry["note"] = (f"{data.get('_year', '')} release" + ("" if have else
                                 " — this county is not in the data"))
            elif src == "aqi":
                c = lookup_county(spec["state_fips"], spec["county_fips"]) or {}
                key = ind.aqi_key(data, c.get("state_name", ""), c.get("county_name", ""))
                entry["note"] = (f"{data.get('_year', '')} summary" + ("" if key else
                                 " — no EPA air monitor in this county, so its air-quality "
                                 "measures cannot be reported"))
        out.append(entry)
    return out


def _tract_shares(spec: dict, polygon=None) -> dict[str, float] | None:
    """Fraction of each census tract's population inside the area.

    None means the whole county (every tract counts in full). A block-group
    area is exact: the selected block groups' 2020 population over their tract's.
    Tracts are whole by construction. A town or ZIP code is split by land area,
    the best available without finer population data.
    """
    if not _is_sub_county(spec):
        return None
    state, county, unit = spec["state_fips"], spec["county_fips"], spec["areal_unit"]
    geoids = [str(g) for g in spec.get("area_geoids") or []]
    if unit == "tract":
        return {g: 1.0 for g in geoids}
    tracts = _unit_features(state, county, "tract")
    tract_pop = {str(g): p for g, p in zip(tracts["GEOID"], tracts["POP100"]) if p == p and p}
    if unit in ("block_group", "custom"):
        bgs = _unit_features(state, county, "block_group")
        bg_pop = {str(g): p for g, p in zip(bgs["GEOID"], bgs["POP100"]) if p == p}
        shares: dict[str, float] = {}
        for g in geoids:
            t = g[:11]
            if t in tract_pop and g in bg_pop:
                shares[t] = shares.get(t, 0.0) + bg_pop[g] / tract_pop[t]
        return {t: min(s, 1.0) for t, s in shares.items()}
    if polygon is None:
        return {}
    t5070 = tracts.to_crs("EPSG:5070")
    area = polygon.geometry.union_all()
    shares = {}
    for g, geom in zip(t5070["GEOID"], t5070.geometry):
        if geom is None or geom.is_empty or not geom.intersects(area):
            continue
        frac = geom.intersection(area).area / geom.area if geom.area else 0.0
        if frac > 0.001:
            shares[str(g)] = min(frac, 1.0)
    return shares


def _indicator_values(metrics, spec: dict, tables: dict, table_errors: dict,
                      polygon=None) -> tuple[dict, dict]:
    """(values, details) for the community measures in `metrics`.

    A measure whose table failed is None and named in details as not measured —
    never a number. County-level measures carry `level: county` so every screen
    and the report can say they describe the whole county.
    """
    from backend.assess import indicators as ind

    values, details = {}, {}
    wanted = [m for m in metrics if m in ind.INDICATORS]
    if not wanted:
        return values, details
    shares = None
    if any(ind.INDICATORS[m]["source"] in ("svi", "places") for m in wanted):
        try:
            shares = _tract_shares(spec, polygon)
        except Exception as exc:
            logger.warning(f"tract shares failed: {exc!r}")
            shares = {}
    geoid = f"{spec['state_fips']}{spec['county_fips']}"
    c = lookup_county(spec["state_fips"], spec["county_fips"]) or {}
    for m in wanted:
        d = ind.INDICATORS[m]
        src = d["source"]
        meta = ind.SOURCES[src]
        info = {"source": meta["short"], "unit": d["unit"], "level": meta["level"]}
        if src in table_errors or src not in tables:
            values[m] = None
            info["not_measured"] = table_errors.get(src, "the data source was not fetched")
            details[m] = info
            continue
        table = tables[src]
        if src in ("svi", "places"):
            use = shares if shares is not None else {t: 1.0 for t in table}
            v, coverage = ind.aggregate(table, d["field"], use)
            info["coverage"] = round(coverage, 3)
            info["level"] = ("population-weighted from census tracts"
                             + (" overlapping the area" if shares is not None else " in the county"))
            if v is None:
                info["not_measured"] = ("no tract estimate covers most of the area's residents"
                                        if use else "the area could not be matched to census tracts")
            values[m] = None if v is None else round(v * d.get("scale", 1), 4)
        elif src == "chr":
            v = ind.county_value(table, geoid, d["field"], d.get("scale", 1))
            us = ind.county_value(table, "00000", d["field"], d.get("scale", 1))
            info["level"] = "county-wide"
            info["year"] = table.get("_year")
            if us is not None:
                info["us"] = round(us, 4)
            if v is None:
                info["not_measured"] = "County Health Rankings publishes no value for this county"
            values[m] = None if v is None else round(v, 4)
        elif src == "aqi":
            key = ind.aqi_key(table, c.get("state_name", ""), c.get("county_name", ""))
            v = (table.get(key) or {}).get(d["field"]) if key else None
            info["level"] = "county-wide (EPA monitors)"
            info["year"] = table.get("_year")
            if v is None:
                info["not_measured"] = ("no EPA air-quality monitor reports for this county"
                                        if not key else "EPA reports no value for this county")
            values[m] = None if v is None else round(float(v), 4)
        details[m] = info
    return values, details


# --- step 3 --------------------------------------------------------------------

def _factor_metrics(factors) -> list[str]:
    return [f["metric"] for f in factors if f.get("metric") in METRICS]


def _step3_area(project: dict, which: str, names: list, sources: list,
                pct_span: tuple[float, float]) -> dict:
    """Step 3's inventory for one area (the project's own, or its base area)."""
    from backend.assess.netdiag import friendly_net_reason

    study_area, bbox, describe = _resolve_study_area(project, which)
    spec = _area_spec(project, which)
    pid = project["id"]
    lo, hi = pct_span
    # Only the bounding box of THIS area is fetched — Prof. Shi 2026-09-16 #2,
    # "retrieve the data only for the defined project area". For a neighbourhood
    # that is a few square kilometres instead of the county.
    results, errs_by_layer, detail = _fetch_layers(
        pid, 3, names, bbox, LAYER_FETCH_BUDGET_S, pct_span=(lo, lo + (hi - lo) * 0.8),
        refresh=bool(project.get("_refresh")),
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
            **({"reused": slot["cached"]} if slot.get("cached") else {}),
            **({"error": err} if err else {}),
        })

    set_progress(pid, 3, lo + (hi - lo) * 0.82, "community data — CDC, County Health Rankings, EPA…")
    tables, table_errors = _fetch_tables(sources, spec)
    table_inventory = _table_inventory(sources, tables, table_errors, spec)

    from backend.assess import places

    state, county = spec["state_fips"], spec["county_fips"]
    set_progress(pid, 3, lo + (hi - lo) * 0.9, "health outcomes — CDC PLACES…")
    try:
        outcomes = places.fetch_health_outcomes(state, county)
    except Exception as exc:  # CDC being down shouldn't block the OSM layers
        logging.getLogger(__name__).warning(f"PLACES fetch failed: {exc!r}")
        outcomes = {"source": "CDC PLACES", "measures": [],
                    "note": f"unavailable this run: {friendly_net_reason(exc)}"}
    if describe["kind"] in ("units", "custom") and outcomes.get("measures"):
        outcomes = _area_outcomes(spec, study_area, outcomes)

    # The study area as a first-class layer of record, so its attribute table
    # (FIPS, area, population) and properties can be shown like any other layer.
    study_layer = {
        "kind": describe["kind"],
        "name": ("Base area" if which == "base"
                 else "Project area" if describe["kind"] in ("units", "custom") else "Study area"),
        "fips": describe.get("fips"),
        "crs": "EPSG:5070 (CONUS Albers Equal Area)",
        "extent_4326": bbox,
        "source": {"county": "US Census TIGERweb county boundaries",
                   "disc": "Derived 5 km centroid buffer"}.get(
                       describe["kind"], "US Census TIGERweb boundaries of the selected areas"),
        "geom_type": "polygon",
    }
    if describe.get("area_km2") is not None:
        study_layer["area_km2"] = describe["area_km2"]
    if describe.get("radius_m") is not None:
        study_layer["radius_m"] = describe["radius_m"]
    if describe["kind"] in ("units", "custom"):
        s1 = _confirmed(project, 1)
        pa = ((s1.get("base") or {}) if which == "base" else s1).get("project_area") or {}
        if pa.get("population") is not None:
            study_layer["population"] = pa["population"]
        study_layer["units"] = len(spec.get("area_geoids") or [])
    else:
        pop = (outcomes or {}).get("total_population")
        if pop is not None:
            study_layer["population"] = pop

    # Prof. Shi 2026-08-07 #5: the reporting unit is the user's choice, and when
    # its data does not exist the system must SAY so after searching — which is
    # what this does: a real Census query, then a plain-language verdict.
    from backend.gis.profile import AREAL_UNITS, DEFAULT_AREAL_UNIT, areal_unit_availability

    unit = spec.get("areal_unit", DEFAULT_AREAL_UNIT)
    if describe["kind"] in ("units", "custom"):
        n_sel = len(spec.get("area_geoids") or [])
        label = (AREAL_UNITS.get(unit) or {}).get("label", unit)
        areal = {"unit": unit, "label": label, "available": True, "count": n_sel,
                 "source": "US Census TIGERweb",
                 "note": (f"The area is {n_sel} selected {_unit_label(unit, n_sel != 1)}. "
                          + ("Results are reported for the area as a whole and for each of them "
                             "at step 7." if n_sel > 1 else "Results are reported for it at step 7."))}
    else:
        set_progress(pid, 3, lo + (hi - lo) * 0.96, "searching for the chosen reporting unit…")
        areal = areal_unit_availability(state, county, unit)
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
        "tables": table_inventory,
        "health_outcomes": outcomes,
    }
    if describe["kind"] == "disc":
        artifact["radius_m"] = 5000  # back-compat for the disc-fallback path
    return artifact


def _area_outcomes(spec: dict, study_area, county_outcomes: dict) -> dict:
    """PLACES outcomes for an area smaller than its county: tract estimates,
    population-weighted, in CRUDE prevalence beside the county's and the US's
    crude figures (see places.fetch_crude_outcomes for why never age-adjusted)."""
    from backend.assess import indicators as ind
    from backend.assess import places

    try:
        rows = ind.places_tracts(spec["state_fips"], spec["county_fips"], _tables_dir(),
                                 fields=list(places.MEASURES))
        shares = _tract_shares(spec, study_area) or {}
        county_crude, us_crude = places.fetch_crude_outcomes(spec["state_fips"], spec["county_fips"])
    except Exception as exc:
        logger.warning(f"area health outcomes failed: {exc!r}")
        return {**county_outcomes, "area_note": (
            "Estimates for the project area itself could not be computed this run, so the "
            "figures below are for the whole county.")}
    measures = []
    for mid, label in places.MEASURES.items():
        v, coverage = ind.aggregate(rows, mid, shares)
        if v is None and mid not in county_crude:
            continue
        measures.append({"id": mid, "label": label,
                         "area_pct": None if v is None else round(v, 1),
                         "county_pct": county_crude.get(mid), "us_pct": us_crude.get(mid)})
    return {
        "source": "CDC PLACES (BRFSS model-based estimates, crude %)",
        "level": "project_area",
        "total_population": county_outcomes.get("total_population"),
        "measures": measures,
        "note": ("Project-area figures combine the census-tract estimates for the tracts the area "
                 "covers, weighted by the residents inside it. Every column is crude prevalence, "
                 "so the three can be compared directly; tract estimates are modelled, not surveyed "
                 "in each tract."),
    }


def _run_step_3(project: dict) -> tuple[dict, str | None, dict | None]:
    factors = _confirmed(project, 2)["factors"]
    metrics = _factor_metrics(factors)
    names = sorted({layer for m in metrics for layer in _metric_layers(m)})
    from backend.assess.indicators import sources_for

    sources = sources_for(metrics)
    has_base = bool(project["meta"].get("base"))
    span = (2.0, 60.0) if has_base else (2.0, 96.0)
    artifact = _step3_area(project, "project", names, sources, span)
    if has_base:
        # The base area gets exactly the same data, so step 7 can compare like
        # with like (Prof. Shi 2026-09-16 #3).
        artifact["base"] = _step3_area(project, "base", names, sources, (60.0, 96.0))
    waiting = [f for f in factors if f.get("metric") == NEEDS_DATA]
    if waiting:
        artifact["awaiting_data"] = [
            {"factor": f.get("factor", ""), "data_needed": f.get("data_needed", "")} for f in waiting
        ]
    # If the run was forced onto an out-of-date Overpass mirror, the features
    # are old and every number derived from them is old with them. Carry that
    # onto the artifact so it reaches the screen and the report — a stale
    # measurement that looks current is worse than a missing one.
    from backend.assess.osm_layers import stale_data_warning

    stale = stale_data_warning()
    if stale:
        artifact["data_freshness_warning"] = stale
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
    "map-derived measures describe the built environment, community measures from census "
    "tracts are population-weighted averages of survey or model estimates, county-wide "
    "measures describe the whole county rather than a smaller study area, factors "
    "awaiting data are not measured at all, and any county health outcomes provided are "
    "model-based county-level estimates. Those outcomes are modelled rather than measured "
    "and carry no variation within the county — so they cannot be attributed to any part "
    "of it, and environment-outcome links are ecological association, not causation. "
    + _PLAIN_WORDS_RULE + "\n\n"
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
    outcomes = step3.get("health_outcomes") or {}
    outcome_rows = outcomes.get("measures") or []
    if outcome_rows and outcomes.get("level") == "project_area":
        outcomes_block = (
            "\n\nHealth outcomes (CDC PLACES, crude prevalence — project area combined from "
            "census-tract model estimates):\n"
            + "\n".join(
                f"- {m['label']}: area {m.get('area_pct')}%, county {m.get('county_pct')}%, "
                f"US {m.get('us_pct')}%" for m in outcome_rows
            )
        )
    elif outcome_rows:
        outcomes_block = (
            "\n\nCounty health outcomes (CDC PLACES, age-adjusted prevalence — modelled at "
            "county level, with no variation within the county):\n"
            + "\n".join(
                f"- {m['label']}: {m['county_pct']}% (US: {m['us_pct']}%)"
                for m in outcome_rows
            )
        )
    else:
        outcomes_block = ""
    tables_block = "".join(
        f"\n- {t['name']} ({t['level']})" + (f": not available — {t['error']}" if t.get("error") else "")
        for t in step3.get("tables") or []
    )
    if tables_block:
        tables_block = "\n\nCommunity data sources:" + tables_block
    waiting = [f for f in factors if f.get("metric") == NEEDS_DATA]
    waiting_block = (
        "\n\nFactors kept in the assessment but awaiting data (not measured):\n"
        + "\n".join(f"- {f['factor']}: needs {f.get('data_needed') or 'a data source'}" for f in waiting)
        if waiting else ""
    )
    base_block = ""
    if project["meta"].get("base"):
        b1 = (_confirmed(project, 1).get("base") or {})
        base_block = (f"\n\nComparison (base) area: {b1.get('place_name', 'defined')} — every "
                      "measure is also computed for it, so the framework should say how the two "
                      "areas will be compared.")
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
        + tables_block
        + outcomes_block
        + waiting_block
        + base_block
        + "\n\nDraft the solution framework for this assessment. Where health "
        "outcomes exceed the US benchmark, connect them to the relevant environmental, "
        "social and built-environment factors."
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
        "near_major_road_exposure": lambda L, sa: a.compute_near_major_road_exposure(
            L["buildings"], L["roads"], sa),
        "industrial_land_share": lambda L, sa: a.compute_industrial_land_share(L["landuse"], sa),
    }


def _database_for(inv: dict, which: str = "project") -> dict:
    sa = inv.get("study_area") or {}
    # Prefer the step-3 study-area description; fall back for old disc artifacts.
    if sa:
        kind = sa.get("kind")
        if kind == "county":
            shape = f"county boundary ({sa['area_km2']} km²)"
        elif kind in ("units", "custom"):
            shape = f"{sa.get('units', '')} selected Census areas ({sa.get('area_km2')} km²)"
        else:
            shape = f"{sa.get('radius_m', 5000)} m centroid buffer"
        # Step-3 artifacts from before 2026-09-29 named the base area "Project
        # area"; label by the column this database belongs to.
        name = "Base area" if which == "base" else sa.get("name", "Study area")
        study_desc = f"{name} · {shape}"
    else:
        study_desc = f"{inv.get('radius_m', 5000)} m circular buffer around the county centroid"
    return {
        "kind": "gis_database",
        "crs": "EPSG:5070 (CONUS Albers Equal Area)",
        "extent_bbox_4326": inv["bbox"],
        "study_area": study_desc,
        "study_area_detail": sa,
        "layers": inv["layers"],
        "tables": inv.get("tables") or [],
        "storage": "map layers saved on this machine and reused across projects for 30 days; "
                   "community data tables saved likewise; artifacts persisted in the audit store",
    }


def _run_step_5(project: dict) -> tuple[dict, str | None, dict | None]:
    inv = _confirmed(project, 3)
    out = _database_for(inv)
    if inv.get("base"):
        out["base"] = _database_for(inv["base"], "base")
    return out, None, None


def _run_step_6(project: dict) -> tuple[dict, str | None, dict | None]:
    from backend.assess.indicators import SOURCES

    factors = _confirmed(project, 2)["factors"]
    metrics = sorted({f["metric"] for f in factors if f.get("metric") in METRICS})
    steps = []
    for m in metrics:
        step = {"action": "compute", "metric": m, "layers": _metric_layers(m)}
        if m in METRIC_SOURCES:
            step["source"] = SOURCES[METRIC_SOURCES[m]]["short"]
        steps.append(step)
    for f in factors:
        if f.get("metric") == NEEDS_DATA:
            steps.append({"action": "awaiting_data", "factor": f.get("factor", ""),
                          "data_needed": f.get("data_needed", "")})
    if project["meta"].get("base"):
        steps.append({"action": "compare_with_base", "input": "every computed metric, both areas"})
    steps.append({"action": "generate_interventions", "input": "all computed metrics"})
    steps.append({"action": "assemble_report", "input": "confirmed artifacts of steps 1-7"})
    return {"kind": "workflow", "steps": steps}, None, None


def _tabulate_units(project, layers, computers, metrics_wanted, fetch_errors, describe,
                    study_area=None, which: str = "project") -> dict:
    """Metrics per reporting unit, or a stated reason there are none.

    Always returns a dict carrying `unit` and either `rows` or `note`, so the
    wizard and report never have to guess whether an absent table means "the
    county is the unit", "the data was not published", or "it failed".

    For an area built from selected units (2026-09-16) the rows are exactly
    those units — a customized area is broken down by its block groups.
    """
    from backend.gis.areas import geometry_unit
    from backend.gis.profile import AREAL_UNITS, DEFAULT_AREAL_UNIT

    meta = _area_spec(project, which) or project["meta"]
    unit = meta.get("areal_unit", DEFAULT_AREAL_UNIT)
    spec_units = AREAL_UNITS.get(unit) or {}
    label = spec_units.get("short") or spec_units.get("label", unit)
    pid = project["id"]
    selected = [str(g) for g in (meta.get("area_geoids") or [])]

    if unit == "county":
        return {"unit": unit, "label": label, "rows": [],
                "note": "The county is the reporting unit, so the figures above are the result."}
    if describe.get("kind") in ("units", "custom") and len(selected) == 1:
        return {"unit": unit, "label": label, "rows": [],
                "note": f"The area is a single {_unit_label(unit)}, so the figures above are the result."}
    if describe.get("kind") not in ("county", "units", "custom"):
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

        units = fetch_areal_unit_geometries(meta["state_fips"], meta["county_fips"],
                                            geometry_unit(unit))
        if units is None or units.empty:
            return {"unit": unit, "label": label, "rows": [],
                    "note": f"Census publishes no {label} boundaries for this county, so results "
                            f"stay at the county level."}
        if selected:
            units = units[units["GEOID"].astype(str).isin(selected)]

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


def _execute_area(project: dict, which: str, metrics_wanted: list,
                  pct_span: tuple[float, float]) -> dict:
    """Step 7 for one area — the project's own, or its base area (2026-09-16 #3).

    Both areas go through this one function with the same confirmed workflow,
    so a difference between them can only come from the areas, never from the
    two having been computed differently.
    """
    from backend.assess import generate_interventions
    from backend.assess.indicators import sources_for

    lo, hi = pct_span
    span = lambda f: lo + (hi - lo) * f  # noqa: E731 — progress within this area's share
    tag = "base area — " if which == "base" else ""
    names = sorted({layer for m in metrics_wanted for layer in _metric_layers(m)})
    study_area, bbox_list, describe = _resolve_study_area(project, which)
    bbox = tuple(bbox_list)
    pid = project["id"]
    spec = _area_spec(project, which)
    results, fetch_errors, detail = _fetch_layers(
        pid, 7, names, bbox, LAYER_FETCH_BUDGET_S, pct_span=(span(0.02), span(0.65))
    )
    layers = {n: results[n] for n in names}
    # study_area resolved above (county polygon, or 5 km disc fallback)
    computers = _metric_computers()
    set_progress(pid, 7, span(0.70), f"{tag}computing metrics…")
    metrics: dict = {}
    compute_errors: dict[str, str] = {}
    for m in metrics_wanted:
        if m not in computers:
            continue
        try:
            metrics.update(computers[m](layers, study_area))
        except Exception as exc:
            # One measure that cannot be computed on this data is not measured;
            # it must not take every other measure — and the step — down with it.
            logger.exception(f"{tag}metric {m} failed")
            compute_errors[m] = f"could not be computed on this data ({type(exc).__name__})"
            for key in METRIC_RESULT_KEYS.get(m, [m]):
                metrics[key] = None
    metrics = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in metrics.items()}

    # Community measures (#6): read from the public tables step 3 saved, and
    # combined for exactly this area.
    tables, table_errors = _fetch_tables(sources_for(metrics_wanted), spec)
    table_values, measure_details = _indicator_values(
        metrics_wanted, spec, tables, table_errors, polygon=study_area)
    metrics.update(table_values)
    # Step 3 (Prof. Shi 2026-11-13): a factor the analyst supplied their own GEOID table for
    # takes its value from that table instead of the computed or fetched one.
    from backend import hcas_userdata

    user_values, user_details = hcas_userdata.apply_tables(project, which, metrics_wanted)
    metrics.update(user_values)
    table_values.update(user_values)
    measure_details.update(user_details)
    set_progress(pid, 7, span(0.82), f"{tag}drawing the map…")

    # A failed fetch yields an empty layer, and an empty layer computes a
    # perfectly plausible 0.0. Null those out so nothing downstream — the
    # intervention engine, the wizard, or the exported report — can read
    # "source unreachable" as "this county has none of it".
    unmeasured = [k for k in _unmeasured_metric_keys(fetch_errors) if k in metrics]
    for key in unmeasured:
        metrics[key] = None
    # A community measure with no value is not measured either — its source
    # failed, the county is not covered, or too few residents have an estimate.
    unmeasured += [k for k, v in table_values.items() if v is None and k not in unmeasured]
    unmeasured += [k for m in compute_errors for k in METRIC_RESULT_KEYS.get(m, [m])
                   if k not in unmeasured]
    for m, why in compute_errors.items():
        measure_details.setdefault(m, {"source": "OpenStreetMap", "level": "the area"})["not_measured"] = why
    # So is an exposure share with no buildings to share out.
    unmeasured += [k for k in ("near_major_road_exposure",) if k in metrics and metrics[k] is None
                   and k not in unmeasured]
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
    if measure_details:
        artifact["measure_details"] = measure_details
    if fetch_errors:
        artifact["fetch_errors"] = fetch_errors
    if table_errors:
        artifact["table_errors"] = table_errors
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
        study_area=study_area, which=which,
    )
    community = [m for m in metrics_wanted if m in METRIC_SOURCES]
    if community and artifact["by_unit"].get("rows"):
        extra = ("Community measures (" + ", ".join(community) + ") come from census-tract or "
                 "county tables and are reported for the area as a whole, not per row.")
        bu = artifact["by_unit"]
        bu["note"] = f"{bu['note']} {extra}" if bu.get("note") else extra
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
            outline=_outline_gdf(project, which),
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
                outline=_outline_gdf(project, which),
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
    return artifact




def _compare_metrics(project_metrics: dict, base_metrics: dict) -> dict:
    """Side-by-side figures for every metric both areas measured."""
    out = {}
    for k, a in project_metrics.items():
        b = base_metrics.get(k)
        if isinstance(a, bool) or isinstance(b, bool):
            continue
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            out[k] = {"project": a, "base": b, "difference": round(a - b, 6),
                      "ratio": round(a / b, 4) if b else None}
        elif k in base_metrics:
            out[k] = {"project": a, "base": b, "difference": None, "ratio": None}
    return out


def _run_step_7(project: dict) -> tuple[dict, str | None, dict | None]:
    # Execute the human-confirmed workflow: a deleted compute step stays deleted.
    steps6 = _confirmed(project, 6)["steps"]
    metrics_wanted = [s["metric"] for s in steps6
                      if s["action"] == "compute" and s.get("metric") in METRICS]
    has_base = bool(project["meta"].get("base"))
    artifact = _execute_area(project, "project", metrics_wanted,
                             (2.0, 55.0) if has_base else (2.0, 96.0))
    waiting = [s for s in steps6 if s["action"] == "awaiting_data"]
    if waiting:
        artifact["awaiting_data"] = [{"factor": s.get("factor", ""),
                                      "data_needed": s.get("data_needed", "")} for s in waiting]
    if has_base:
        base = _execute_area(project, "base", metrics_wanted, (55.0, 96.0))
        artifact["_base_map_html"] = base.pop("_map_html", None)
        artifact["base"] = base
        artifact["comparison"] = _compare_metrics(artifact["metrics"], base["metrics"])
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


def get_map(pid: str, db: Path | None = None, which: str = "project") -> str:
    db = db or DB_PATH
    key = f"{pid}:base" if which == "base" else pid
    with _conn(db) as conn:
        row = conn.execute("SELECT html FROM maps WHERE project_id = ?", (key,)).fetchone()
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
        # A Save-as copy repeats its source's rows verbatim; the ablation must
        # drop them rather than count one human review twice.
        "copied_from",
    ])
    for r in rows:
        meta, data = _load_meta(r["meta_json"]), json.loads(r["data_json"])
        usage = data.get("usage") or {}
        w.writerow([
            r["id"], meta.get("title"), r["step"], r["status"],
            r["drafted_at"], r["confirmed_at"],
            data.get("edit_delta") is not None, data.get("edit_seconds"),
            data.get("model"), usage.get("input_tokens"), usage.get("output_tokens"),
            meta.get("copied_from") or "",
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


def _awaiting_data_lines(factors) -> list[str]:
    """Factors kept in the assessment though nothing connected can measure them
    yet (#6) — listed, never silently dropped."""
    waiting = [f for f in factors if f.get("metric") == NEEDS_DATA]
    if not waiting:
        return []
    return ["\n### Factors awaiting data",
            "_Important to this community's health, but not measured in this assessment: no "
            "connected data source covers them yet. They are listed so the gap stays visible "
            "until a data source for them is added._\n",
            *[f"- **{f.get('factor', '')}** — {f.get('rationale', '')}"
              + (f" _Data needed: {f['data_needed']}_" if f.get("data_needed") else "")
              for f in waiting]]


# Plain names for step 7's flat result keys — the same words the wizard shows.
RESULT_LABELS = {
    "building_coverage_ratio": "Building coverage (share of area)",
    "greenspace_coverage_ratio": "Green space (share of area)",
    "greenspace_area_m2": "Green space area (m²)",
    "road_density_km_per_km2": "Road density (km per km²)",
    "far": "Floor area ratio",
    "levels_tagged_pct": "Buildings with a height tag (%)",
    "parking_coverage_ratio": "Parking (share of area)",
    "pedestrian_density_km_per_km2": "Sidewalks and paths (km per km²)",
    "transit_stop_count": "Transit stops",
    "transit_stop_density_per_km2": "Transit stops per km²",
    "facility_count": "Community facilities",
    "facility_density_per_km2": "Community facilities per km²",
    "land_use_mix": "Land-use mix (0 = one use, 1 = even mix)",
    "land_use_categories": "Land-use categories",
    "near_major_road_exposure": "Buildings within 150 m of a major road (%)",
    "industrial_land_share": "Industrial land (% of area)",
}


def _result_label(key: str) -> str:
    if key in RESULT_LABELS:
        return RESULT_LABELS[key]
    from backend.assess.indicators import INDICATORS

    unit = (INDICATORS.get(key) or {}).get("unit")
    return with_unit(METRIC_LABELS.get(key, key.replace("_", " ")), unit)


def with_unit(label: str, unit: str | None) -> str:
    """`label (unit)`, without saying the unit twice.

    "Social vulnerability (percentile)" + "percentile (0–1)" used to read
    "Social vulnerability (percentile) (percentile (0–1))" (QA 2026-09-29).
    When the label already names the unit, the unit's extra detail is folded
    into the label's own parentheses instead: "(percentile, 0–1)".
    """
    if not unit:
        return label
    head, _, rest = unit.partition(" (")
    rest = rest[:-1] if rest.endswith(")") else rest
    tag = f"({head})"
    if label.lower().endswith(tag.lower()):
        return label[: -len(tag)] + (f"({head}, {rest})" if rest else tag)
    if len(head) >= 4 and head.lower() in label.lower():
        return label
    return f"{label} ({unit})"


def _comparison_lines(project: dict, results: dict) -> list[str]:
    """Project area against the base area, every metric both measured (#3)."""
    base = results.get("base")
    comp = results.get("comparison") or {}
    if not base or not comp:
        return []
    b1 = _confirmed(project, 1).get("base") or {}
    s1 = _confirmed(project, 1)

    def cell(v):
        return "not measured" if v is None else _fmt_report_num(v)

    out = [
        "\n## Comparison with the base area",
        f"_Project area: {s1.get('place_name', 'project area')}. Base area: "
        f"{b1.get('place_name', 'base area')}. Both were computed with the same confirmed "
        "workflow and data sources. Difference = project area minus base area._\n",
        "| Measure | Project area | Base area | Difference |",
        "|---|---|---|---|",
    ]
    for k, row in comp.items():
        out.append(f"| {_result_label(k)} | {cell(row.get('project'))} | {cell(row.get('base'))} | "
                   f"{cell(row.get('difference'))} |")
    return out


def _workflow_lines(project: dict) -> list[str]:
    """The step-6 workflow as a report section (Prof. Shi 2026-11-13, step 6: a
    flowchart of the workflow). The drawn flowchart (input data -> processing ->
    metrics -> outputs) comes with the page and the HTML export; markdown carries the
    same chain as text so it is in every format."""
    row = next((s for s in project["steps"] if s["step"] == 6), None)
    wf = (row.get("human_final") or row.get("ai_draft")) if row else None
    steps = (wf or {}).get("steps") or []
    if not steps:
        return []
    out = ["\n## Workflow",
           "_The flowchart of this workflow (input data layers, processing operations, metrics "
           "and outputs) is shown in the assessment app and included in the HTML export of this "
           "report._\n"]
    n = 0
    for st in steps:
        n += 1
        act = st.get("action")
        if act == "compute":
            src = ", ".join(_layer_meta(l)[0] for l in st.get("layers") or []) or st.get("source") or ""
            out.append(f"{n}. Compute `{st.get('metric')}`" + (f" from {src}" if src else "")
                       + " for each reporting unit.")
        elif act == "awaiting_data":
            out.append(f"{n}. Awaiting data: {st.get('factor', '')}"
                       + (f" (needs {st['data_needed']})" if st.get("data_needed") else "") + ".")
        elif act == "compare_with_base":
            out.append(f"{n}. Compare every result with the base area.")
        elif act == "generate_interventions":
            out.append(f"{n}. Generate interventions from all computed metrics.")
        elif act == "assemble_report":
            out.append(f"{n}. Assemble this report from the confirmed steps.")
        else:
            out.append(f"{n}. {act}")
    return out


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
        if d.get("kind") in ("units", "custom"):
            return (d.get("text") or "selected Census areas") + (
                f" ({d['area_km2']} km²)" if d.get("area_km2") is not None else "")
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
    table_errors = results.get("table_errors") or {}
    if fetch_errors or unmeasured or study_warnings or results.get("map_note") or table_errors:
        quality.append("\n## Data quality")
    for w in study_warnings:
        quality.append(f"- **Study area:** {w}")
    for layer, reason in sorted(fetch_errors.items()):
        quality.append(f"- **`{layer}` could not be fetched** — {reason}")
    from backend.assess.indicators import SOURCES as _SOURCES

    for src, reason in sorted(table_errors.items()):
        quality.append(f"- **{_SOURCES.get(src, {}).get('name', src)}** — {reason}")
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

    details = results.get("measure_details") or {}

    def _result_line(k, v) -> str:
        d = details.get(k) or {}
        if v is None:
            why = d.get("not_measured") or "the source data could not be fetched on this run"
            return f"- `{k}`: not measured — {why}."
        if not d:
            return f"- `{k}`: {v}"
        unit = d.get("unit") or ""
        if unit in ("", "ratio"):
            suffix = ""
        elif unit.startswith("%"):
            suffix = unit                        # "%", "% of days"
        elif any(ch in unit for ch in "=–("):
            suffix = f" ({unit})"                # a scale, not a unit
        else:
            suffix = f" {unit}"
        us = f"; US {_fmt_report_num(d['us'])}" if d.get("us") is not None else ""
        return (f"- `{k}`: {_fmt_report_num(v)}{suffix} — {d.get('level', '')}, "
                f"{d.get('source', '')}{us}")

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
        *[f"- {f['factor']} → `{f['metric']}`: {f['rationale']}" for f in factors
          if f.get("metric") != NEEDS_DATA],
        *_awaiting_data_lines(factors),
        *_workflow_lines(project),
        *__import__("backend.hcas_userdata", fromlist=["report_lines"]).report_lines(project),
        "\n## Results",
        *results_lines,
        *_comparison_lines(project, results),
    ]
    outcomes = _confirmed(project, 3).get("health_outcomes") or {}
    if outcomes.get("measures") and outcomes.get("level") == "project_area":
        def _pct(v):
            return "—" if v is None else f"{v}%"

        lines += [
            "\n## Health outcomes (project area)",
            f"_{outcomes['source']}. {outcomes.get('note', '')}_\n",
            "| Measure | Project area | County | US |",
            "|---|---|---|---|",
            *[f"| {m['label']} | {_pct(m.get('area_pct'))} | {_pct(m.get('county_pct'))} | "
              f"{_pct(m.get('us_pct'))} |" for m in outcomes["measures"]],
        ]
    elif outcomes.get("measures"):
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
        *_report_references(framework, _strs("references"), cited),
    ]
    return {"kind": "report_markdown", "markdown": "\n".join(lines)}, None, None


def _report_references(framework: dict, titles: list[str], cited: dict) -> list[str]:
    """The report's reference lines, in step 4's order.

    Built-in titles carry full bibliographic data. Titles the knowledgebase
    returned for this project are cited too: they used to be dropped because
    only built-in titles have that data (QA 2026-09-29: step 4 listed 14
    references, the report 13). The grounding note appears only when step 4
    really was grounded; a run without the knowledgebase says nothing about it.
    """
    kb = framework.get("knowledgebase")
    grounded = isinstance(kb, dict)
    kb_docs = [t for t in (kb.get("source_documents") or []) if isinstance(t, str)] if grounded else []
    kb_titles = set(kb_docs)
    out: list[str] = []
    if grounded:
        n = len(kb_docs)
        out.append(f"_Theoretical framework grounded in the project knowledgebase "
                   f"({n} source{'' if n == 1 else 's'})._\n")
    for t in titles:
        if t in cited:
            r = cited[t]
            out.append(f"- {r['authors']} ({r['year']}). {t}. _{r['journal']}_."
                       + (f" doi:{r['doi']}" if r["doi"] else ""))
        elif t in kb_titles:
            out.append(f"- {t}. From the project knowledgebase.")
    return out


def _draft(project: dict, step: int) -> tuple[dict, str | None, dict | None]:
    if step == 1:
        return _run_step_1(project["meta"], pid=project["id"]), None, None
    if step == 2:
        return _run_step_2(project)
    runners = {3: _run_step_3, 4: _run_step_4, 5: _run_step_5, 6: _run_step_6, 7: _run_step_7, 8: _run_step_8}
    return runners[step](project)


# ---------------------------------------------------------------------------
# Machine
# ---------------------------------------------------------------------------

def _clean_polygon(feature) -> dict:
    """Meta fields for an uploaded study-area outline (a GeoJSON Feature), or {}.
    Only the shape is checked here; the file itself was validated when uploaded."""
    if not feature:
        return {}
    geom = feature.get("geometry") if isinstance(feature, dict) and feature.get("type") == "Feature" else feature
    if not isinstance(geom, dict) or geom.get("type") not in ("Polygon", "MultiPolygon")             or not geom.get("coordinates"):
        raise ValueError("The uploaded study-area outline must be a polygon.")
    if len(json.dumps(geom)) > 1_500_000:
        raise ValueError("The uploaded study-area outline is too large to save with the project.")
    props = feature.get("properties") if isinstance(feature, dict) else None
    return {"area_polygon": geom, "area_polygon_name": str((props or {}).get("name") or "")}


def _outline_gdf(project: dict, which: str = "project"):
    """The uploaded outline as a GeoDataFrame (EPSG:4326), for drawing on maps; None
    when the project has none (the base area never has one)."""
    geom = None if which == "base" else (project.get("meta") or {}).get("area_polygon")
    if not geom:
        return None
    import geopandas as gpd
    from shapely.geometry import shape

    try:
        return gpd.GeoDataFrame({"name": ["Uploaded study area"]}, geometry=[shape(geom)], crs="EPSG:4326")
    except Exception as exc:
        logger.warning("stored study-area outline unreadable: %r", exc)
        return None


def _load_meta(raw: str) -> dict:
    """Project meta from its JSON, with the retired "Customized" reporting unit
    read as block group (Prof. Shi 2026-11-13 #15): a saved project that used
    `custom` opens with the same selected block groups and the same name."""
    meta = json.loads(raw)
    if meta.get("areal_unit") == "custom":
        meta["areal_unit"] = "block_group"
    base = meta.get("base")
    if isinstance(base, dict) and base.get("areal_unit") == "custom":
        base["areal_unit"] = "block_group"
    return meta


def _clean_area(state_fips: str, county_fips: str, areal_unit: str | None,
                area_geoids, area_name: str | None) -> dict:
    """Validate one area definition (project or base) and return its meta fields.

    `area_geoids` empty means the whole county — for a Census unit that is the
    old behaviour, results broken down by that unit. A Customized area has no
    whole-county form: it IS its block groups, so it needs at least one.
    Membership of each GEOID in the county is checked when step 1 fetches the
    boundaries; here only the shape is checked, since that needs no network.
    """
    from backend.gis.profile import AREAL_UNITS, DEFAULT_AREAL_UNIT

    if lookup_county(state_fips, county_fips) is None:
        raise ValueError(f"Unknown county FIPS {state_fips}-{county_fips}")
    unit = areal_unit or DEFAULT_AREAL_UNIT
    if unit == "custom":              # retired option (#15): a neighbourhood is block groups
        unit = "block_group"
    if unit not in AREAL_UNITS:
        raise ValueError(f"areal_unit must be one of {', '.join(AREAL_UNITS)}")
    geoids = [str(g).strip() for g in (area_geoids or []) if str(g).strip()]
    geoids = list(dict.fromkeys(geoids))            # de-duplicate, keep order
    if unit == "county" and geoids:
        raise ValueError("A whole-county project has no sub-areas to select.")
    if any(not g.isdigit() for g in geoids):
        raise ValueError("Area identifiers must be Census GEOIDs (digits only).")
    prefix = f"{state_fips}{county_fips}"
    if unit != "zcta" and any(not g.startswith(prefix) for g in geoids):
        raise ValueError("Every selected area must lie in the chosen county.")
    return {
        "state_fips": state_fips, "county_fips": county_fips, "areal_unit": unit,
        "area_geoids": geoids, "area_name": (area_name or "").strip(),
    }


def _clean_base(base) -> dict | None:
    """The comparison ("base") area, or None when there is none (#3)."""
    if not base:
        return None
    return _clean_area(str(base.get("state_fips") or ""), str(base.get("county_fips") or ""),
                       base.get("areal_unit"), base.get("area_geoids"), base.get("area_name"))


def create_project(
    title: str,
    state_fips: str,
    county_fips: str,
    gate_mode: str = "gated",
    db: Path | None = None,
    areal_unit: str | None = None,
    location_query: str = "",
    notes: str = "",
    area_geoids: list | None = None,
    area_name: str = "",
    base: dict | None = None,
    ai_note: str = "",
    area_polygon: dict | None = None,
) -> str:
    db = db or DB_PATH
    area = _clean_area(state_fips, county_fips, areal_unit, area_geoids, area_name)
    if gate_mode not in ("gated", "auto"):
        raise ValueError("gate_mode must be 'gated' or 'auto'")
    base_area = _clean_base(base)
    # ponytail: gate_mode is stored only; the auto path lands with the M2 ablation
    pid = uuid.uuid4().hex[:12]
    meta = {
        "title": title,
        **area,
        "gate_mode": gate_mode,
        "location_query": location_query,
        "notes": notes,
        "ai_note": ai_note or "",
        **_clean_polygon(area_polygon),
        "current_step": 1,
        "created_at": time.time(),
    }
    if base_area:
        meta["base"] = base_area
    with _conn(db) as conn:
        conn.execute("INSERT INTO projects VALUES (?, ?)", (pid, json.dumps(meta)))
    return pid


_UNSET = object()


def update_project_scope(
    pid: str,
    title: str | None = None,
    state_fips: str | None = None,
    county_fips: str | None = None,
    areal_unit: str | None = None,
    location_query: str | None = None,
    notes: str | None = None,
    db: Path | None = None,
    area_geoids: list | None = None,
    area_name: str | None = None,
    base: Any = _UNSET,
    ai_note: str | None = None,
    area_polygon: Any = _UNSET,
) -> dict:
    """Redefine an existing project instead of starting a new one.

    Prof. Shi 2026-08-07 #8 asked for a working Back button on step 1 "allowing
    the user to go back to re-define the project". Changing the study area
    invalidates everything drafted from it, so every artifact is marked stale
    (the same signal an edited upstream step already produces) and the step-1
    draft is dropped so it must be re-run against the new scope.

    Since 2026-09-16 the scope also covers the selected sub-areas and the base
    area. `area_geoids=None` keeps the current selection; `base=None` removes
    the base area, and leaving `base` out keeps it.
    """
    db = db or DB_PATH
    with _conn(db) as conn:
        row = conn.execute("SELECT meta_json FROM projects WHERE id = ?", (pid,)).fetchone()
        if row is None:
            raise KeyError(pid)
        meta = _load_meta(row["meta_json"])
        new_state = state_fips or meta["state_fips"]
        new_county = county_fips or meta["county_fips"]
        new_unit = areal_unit if areal_unit is not None else meta.get("areal_unit")
        geo_changed = (new_state, new_county) != (meta["state_fips"], meta["county_fips"])
        unit_changed = new_unit != meta.get("areal_unit")
        if area_geoids is not None:
            new_geoids = area_geoids
        elif geo_changed or unit_changed:
            new_geoids = []           # a selection never carries over to another county or unit
        else:
            new_geoids = meta.get("area_geoids") or []
        new_name = area_name if area_name is not None else meta.get("area_name", "")
        area = _clean_area(new_state, new_county, new_unit, new_geoids, new_name)
        new_base = meta.get("base") if base is _UNSET else _clean_base(base)

        scope_changed = (
            geo_changed or unit_changed
            or area["area_geoids"] != (meta.get("area_geoids") or [])
            or new_base != meta.get("base")
        )
        meta.update(area)
        if new_base:
            meta["base"] = new_base
        else:
            meta.pop("base", None)
        if title:
            meta["title"] = title
        if location_query is not None:
            meta["location_query"] = location_query
        if notes is not None:
            meta["notes"] = notes
        if ai_note is not None:       # the assistant's note on step 1; not part of the scope
            meta["ai_note"] = ai_note
        if area_polygon is not _UNSET:   # the uploaded outline; null removes it
            meta.pop("area_polygon", None)
            meta.update(_clean_polygon(area_polygon))
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
    elif scope_changed:
        for which in ("project", "base"):
            _area_path(pid, which).unlink(missing_ok=True)
    return {"project_id": pid, "scope_changed": scope_changed, "stale_steps": stale}


def copy_project(pid: str, title: str | None = None, db: Path | None = None) -> str:
    """Save as: a new project that starts from everything `pid` has so far.

    Prof. Shi 2026-09-16 #4: "a project can only be saved to itself, i.e., a new
    project cannot start based on an existing project." Every artifact row keeps
    its status, so a confirmed step stays confirmed and the copy picks up where
    the original is. Maps and the per-project data cache are copied too — the
    point is not having to fetch the same county again.

    The copy records `copied_from`. Its rows repeat the original's audit tuples
    verbatim, and the HITL study must be able to drop the duplicates rather than
    count one human review twice; export_audit_csv carries the column.
    """
    import shutil

    db = db or DB_PATH
    new_pid = uuid.uuid4().hex[:12]
    with _conn(db) as conn:
        row = conn.execute("SELECT meta_json FROM projects WHERE id = ?", (pid,)).fetchone()
        if row is None:
            raise KeyError(pid)
        meta = _load_meta(row["meta_json"])
        meta["title"] = (title or "").strip() or f"{meta.get('title') or 'Untitled assessment'} (copy)"
        meta["copied_from"] = pid
        meta["created_at"] = time.time()
        conn.execute("INSERT INTO projects VALUES (?, ?)", (new_pid, json.dumps(meta)))
        conn.execute(
            "INSERT INTO artifacts (project_id, step, data_json, status, drafted_at, confirmed_at)"
            " SELECT ?, step, data_json, status, drafted_at, confirmed_at"
            " FROM artifacts WHERE project_id = ?",
            (new_pid, pid),
        )
        for key, html, created in conn.execute(
            "SELECT project_id, html, created_at FROM maps WHERE project_id = ? OR project_id LIKE ?",
            (pid, f"{pid}:%"),
        ).fetchall():
            conn.execute(
                "INSERT OR REPLACE INTO maps VALUES (?, ?, ?)",
                (new_pid + key[len(pid):], html, created),
            )
    src = LAYER_CACHE_DIR / pid
    if src.is_dir():
        try:
            shutil.copytree(src, LAYER_CACHE_DIR / new_pid, dirs_exist_ok=True)
        except Exception as exc:  # the copy is still whole; it just refetches
            logger.warning(f"copy_project: layer cache not copied ({exc!r})")
    return new_pid


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
        meta = _load_meta(r["meta_json"])
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
        "meta": _load_meta(row["meta_json"]),
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


def run_step(pid: str, step: int, db: Path | None = None, refresh: bool = False) -> dict:
    """Draft step `step`. The gate: every earlier step must be confirmed.

    `refresh` asks the data steps to download again rather than reuse saved
    layers (never an upload) — the "Re-fetch data" choice at step 3."""
    db = db or DB_PATH
    if not 1 <= step <= 8:
        raise ValueError(f"step must be 1-8, got {step}")
    project = get_project(pid, db)
    project["_refresh"] = bool(refresh)
    confirmed = {s["step"] for s in project["steps"] if s["status"] == "confirmed"}
    missing = [n for n in range(1, step) if n not in confirmed]
    if missing:
        raise ValueError(f"cannot run step {step}: steps {missing} not confirmed")

    mark_started(pid, step)
    try:
        ai_draft, model, usage = _draft(project, step)
    finally:
        clear_progress(pid, step)
    map_html = ai_draft.pop("_map_html", None)
    base_map_html = ai_draft.pop("_base_map_html", None)
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
        if base_map_html is not None:
            conn.execute(
                "INSERT OR REPLACE INTO maps VALUES (?, ?, ?)",
                (f"{pid}:base", base_map_html, time.time()),
            )
    from backend import hcas_ai

    hcas_ai.record(pid, step, "step_draft", {"refresh": bool(refresh)},
                   hcas_ai.summarize_draft(step, data["ai_draft"], model, usage),
                   hcas_ai.context_for(project), db=db)
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
    if step == 1:
        start_prefetch(pid, db=db)
    return {"step": step, "status": "confirmed", "stale_downstream": stale}


# --- background prefetch -----------------------------------------------------
# Prof. Shi 2026-09-16 #2 again: retrieval was the slow part, and it started only
# when step 3 did — after the analyst had already spent minutes choosing
# factors. Once the area is confirmed nothing about WHERE to download changes,
# so every map layer starts downloading then, in the background. Step 3 finds
# finished layers in the cache and waits for any still arriving (see
# _claim_inflight) instead of asking the server a second time. It fetches all
# eight layers, not only the ones the factors will need: at step 1 the factors
# do not exist yet, and step 7's map draws them all anyway.
PREFETCH_ENABLED = _os.environ.get("HCAS_PREFETCH", "1").strip().lower() not in ("0", "false", "no")
_prefetching: set[str] = set()
_prefetch_lock = threading.Lock()


def start_prefetch(pid: str, db: Path | None = None) -> bool:
    """Begin downloading every map layer for this project's area. Returns False
    when it is switched off or already running for this project."""
    if not PREFETCH_ENABLED:
        return False
    with _prefetch_lock:
        if pid in _prefetching:
            return False
        _prefetching.add(pid)

    def run():
        try:
            project = get_project(pid, db=db)
            _study, bbox, _describe = _resolve_study_area(project)
            names = sorted(LAYER_META)
            logger.info(f"prefetch {pid}: {len(names)} layers for {bbox}")
            _fetch_layers(pid, 0, names, bbox, LAYER_FETCH_BUDGET_S)
        except Exception as exc:  # a head start, never a failure anyone sees
            logger.warning(f"prefetch {pid} stopped: {exc!r}")
        finally:
            clear_progress(pid, 0)
            with _prefetch_lock:
                _prefetching.discard(pid)

    threading.Thread(target=run, name=f"hcas-prefetch-{pid}", daemon=True).start()
    return True


_INTAKE_SYSTEM = (
    "You are the intake assistant of HCAS (Healthy Community Assessment System), a "
    "step-by-step GIS + AI assessment of community health for a US county or an area "
    "within one — a town, ZIP code, census tracts or block groups, or a named community "
    "built from block groups — optionally compared with a base area. The analyst has not "
    "created a project yet, so you have no project data: answer questions about what the "
    "system does, what the eight steps are (define project, choose factors, gather data, "
    "set methodology, build database, plan workflow, run analysis, write report), what "
    "data it uses (OpenStreetMap built environment; CDC PLACES health outcomes and "
    "health-related social needs; the CDC Social Vulnerability Index for income, housing, "
    "education and access; County Health Rankings for air pollution, drinking water, "
    "health care providers and safety; EPA air-quality monitors; US Census boundaries "
    "and 2020 population), and how to define a study area. Be concise. Never invent measurements for any place — no assessment has "
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
    "empty string and explain in `caveat`. Also classify how big the described place is "
    "in `scale`: \"county\" (a whole county), \"town\" (a township or New England-style "
    "town that is a county subdivision, not an incorporated city), \"zip\" (a ZIP code — put the five digits in `zip`), "
    "\"place\" (a city, town or village that is a Census incorporated place, e.g. Gary, "
    "Indiana or Columbus, Ohio — put its plain name in `town`), or "
    "\"community\" (a neighborhood, community, campus, landmark surroundings or any other "
    "area smaller than a town). Put the town's plain name in `town` when scale is town or place; "
    "leave `zip` and `town` empty otherwise."
)

_AREA_SCHEMA = {
    "type": "object",
    "properties": {
        "county": {"type": "string"},
        "state": {"type": "string"},
        "interpretation": {"type": "string"},
        "caveat": {"type": "string"},
        "scale": {"type": "string", "enum": ["county", "town", "place", "zip", "community"]},
        "zip": {"type": "string"},
        "town": {"type": "string"},
    },
    "required": ["county", "state", "interpretation", "caveat", "scale", "zip", "town"],
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
        # How big the place is, so the form can pick the matching reporting
        # unit (2026-09-16 #1): a ZIP code becomes that ZIP area, a town its
        # county subdivision, anything smaller a customized area.
        "scale": draft.get("scale") or "county",
        "zip": "".join(ch for ch in (draft.get("zip") or "") if ch.isdigit())[:5],
        "town": draft.get("town") or "",
    }


def units_geojson(state_fips: str, county_fips: str, unit: str) -> dict:
    """The county's Census units of one kind as GeoJSON for the area picker —
    simplified for drawing, with name, 2020 population and land area on each."""
    from backend.gis.profile import AREAL_UNITS

    if lookup_county(state_fips, county_fips) is None:
        raise ValueError(f"Unknown county FIPS {state_fips}-{county_fips}")
    if unit == "county":
        geom = _county_geometry(state_fips, county_fips)
        if geom is None or geom.empty:
            raise RuntimeError("The county boundary could not be loaded from Census TIGERweb.")
        g = geom.to_crs("EPSG:4326")
        g["GEOID"] = f"{state_fips}{county_fips}"
        g["NAME"] = build_place_name(state_fips, county_fips)
        units = g
    elif unit in AREAL_UNITS:
        units = _unit_features(state_fips, county_fips, unit)
    else:
        raise ValueError(f"Unknown reporting unit {unit!r}")
    out = units[["GEOID", "geometry"]].copy()
    out["name"] = units["NAME"].astype(str) if "NAME" in units.columns else out["GEOID"]
    if "POP100" in units.columns:
        out["population"] = units["POP100"].map(lambda v: None if v != v else int(v))
    out["area_km2"] = (units.to_crs("EPSG:5070").geometry.area / 1e6).round(3)
    out["geometry"] = out.geometry.simplify(0.0001, preserve_topology=True)
    import json as _json

    return _json.loads(out.to_json(drop_id=True))


def suggest_area(text: str, state_fips: str, county_fips: str) -> dict:
    """AI-recommended block groups for a named community (2026-09-16 #1)."""
    from backend.gis import areas

    if lookup_county(state_fips, county_fips) is None:
        raise ValueError(f"Unknown county FIPS {state_fips}-{county_fips}")
    bgs = _unit_features(state_fips, county_fips, "block_group")
    minx, miny, maxx, maxy = [float(v) for v in bgs.to_crs("EPSG:4326").total_bounds]
    out = areas.suggest_block_groups(
        text, build_place_name(state_fips, county_fips), (minx, miny, maxx, maxy), bgs,
        llm=lambda system, prompt, schema: _llm_structured(system, prompt, schema,
                                                           model="claude-sonnet-5"),
    )
    pops = {str(g): p for g, p in zip(bgs["GEOID"], bgs["POP100"]) if p == p}
    chosen = out.get("geoids") or []
    if chosen:
        sel = bgs[bgs["GEOID"].astype(str).isin(chosen)]
        out["area_km2"] = round(float(sel.to_crs("EPSG:5070").geometry.area.sum()) / 1e6, 2)
        if all(g in pops for g in chosen):
            out["population"] = int(sum(pops[g] for g in chosen))
    return out


def upload_study_area(filename: str, content: bytes, unit: str = "block_group") -> dict:
    """A study-area polygon from the analyst's own file (Prof. Shi 2026-11-13 #12).

    The file is read and dissolved to one WGS84 polygon; the county holding most of
    it becomes the project's county (the area model has one county), and the units of
    `unit` lying mostly inside it (more than half of their own area) are selected.
    The analyst can still click units on or off afterwards."""
    from backend.gis import places, upload
    from backend.gis.counties import list_counties, list_states

    gdf, notes = upload.read_polygon(filename, content)
    try:
        counties = upload.counties_for_polygon(gdf)
    except Exception as exc:
        logger.warning("county lookup for an uploaded area failed: %r", exc)
        raise RuntimeError("The Census boundary service (TIGERweb) could not be reached to find "
                           "which county this polygon is in. Check the connection and try again.")
    if not counties:
        raise upload.AreaUploadError("The polygon does not overlap any US county. HCAS covers the "
                                     "United States; check the file's location and coordinate system.")
    top = counties[0]
    unit = unit if unit in ("county_subdivision", "zcta", "tract", "block_group") else "block_group"
    state = next((x for x in list_states() if x["state_fips"] == top["state_fips"]), {})
    cty = next((c for c in list_counties(top["state_fips"]) if c["county_fips"] == top["county_fips"]), {})
    units = _unit_features(top["state_fips"], top["county_fips"], unit)
    chosen = places.units_mostly_inside(units, gdf.geometry)
    word = _unit_label(unit, True)
    caveat = ""
    if len(counties) > 1 and counties[1]["share"] >= 0.005:
        parts = ", ".join(f"{c['name']} {round(c['share'] * 100)}%" for c in counties[:4]
                          if c["share"] >= 0.005)
        caveat = (f"The polygon crosses county lines ({parts}). A project area lies in one county, "
                  f"so {top['name']} (the county holding most of it) was used and only its {word} "
                  "were selected.")
    if not chosen:
        caveat = (caveat + " " if caveat else "") + (
            f"No {_unit_label(unit)} lies mostly inside the polygon, so nothing was selected. "
            "Try a smaller reporting unit, or click the units you want on the map.")
    area_km2 = round(float(gdf.to_crs("EPSG:5070").geometry.area.sum()) / 1e6, 2)
    return {
        "polygon": {"type": "Feature", "properties": {"name": Path(filename).stem},
                    "geometry": upload.simplify_for_storage(gdf)},
        "state_fips": top["state_fips"], "county_fips": top["county_fips"],
        "county_name": cty.get("county_name") or top["name"],
        "state_name": state.get("state_name", ""),
        "unit": unit, "geoids": chosen, "counties": counties, "notes": notes,
        "caveat": caveat, "area_km2": area_km2, "filename": filename,
        "method_text": f"{word} with more than half of their area inside the uploaded polygon",
    }


def place_area(name: str, state_fips: str, county_fips: str) -> dict:
    """Block groups inside a city or town (a Census incorporated place) — Prof. Shi
    2026-11-13 #17 ("Gary, Indiana" could not be delineated). The boundary is the
    Census TIGERweb place; a block group is in when more than half of it lies inside."""
    from backend.gis import places

    if lookup_county(state_fips, county_fips) is None:
        raise ValueError(f"Unknown county FIPS {state_fips}-{county_fips}")
    place = places.fetch_place(state_fips, name)
    if place is None:
        return {"geoids": [], "place": name, "method_text": "",
                "caveat": f"No Census city or town called “{name}” was found in this state, "
                          "so no block groups were selected. Pick them on the map."}
    bgs = _unit_features(state_fips, county_fips, "block_group")
    chosen = places.units_mostly_inside(bgs, place.geometry)
    nm = str(place.iloc[0]["NAME"])
    out = {"geoids": chosen, "place": nm,
           "method_text": f"block groups with more than half of their area inside the "
                          f"Census boundary of {nm}",
           "caveat": "" if chosen else f"No block group of this county lies mostly inside {nm}. "
                                      "Check the county, or pick block groups on the map."}
    if chosen:
        sel = bgs[bgs["GEOID"].astype(str).isin(chosen)]
        out["area_km2"] = round(float(sel.to_crs("EPSG:5070").geometry.area.sum()) / 1e6, 2)
        pops = {str(g): p for g, p in zip(bgs["GEOID"], bgs["POP100"]) if p == p}
        if all(g in pops for g in chosen):
            out["population"] = int(sum(pops[g] for g in chosen))
    return out


# Prof. Shi 2026-09-16 #6: "add pollution data" came back as "no change was
# needed. The metric registry has no pollution or air/water quality measure …
# Adding nothing", and "demographic and socioeconomic factors" the same. The old
# rule below said exactly that — "if the registry cannot measure it, add
# nothing" — so the refusal was the prompt working as written. A broad request
# now adds the measures that fit, and something no connected data can measure
# is added as awaiting data instead of refused.
_REVISE_SYSTEM = (
    "You revise an existing factor list for a healthy-community assessment in "
    "response to one instruction from the analyst. Do NOT redesign the list. Return "
    "only what to ADD and what to REMOVE. Add a factor only if the instruction asks "
    "for it; remove one only if the instruction asks for it. A broad request (for "
    "example \"add pollution data\" or \"add demographic and socioeconomic factors\") "
    "asks for the several available measures that cover that topic, each as its own "
    "factor — do not add a measure that is already in the list. Every added factor "
    "maps to exactly one of the available measures below. "
    + _NEEDS_DATA_RULE + " " + _PLAIN_WORDS_RULE
    + " Use `note` to tell the analyst in one or two plain sentences what you added or "
    "removed and why.\n\nAVAILABLE MEASURES:\n" + _measure_catalog()
)

_REVISE_SCHEMA = {
    "type": "object",
    "properties": {
        "add": {"type": "array", "items": _FACTOR_ITEM_SCHEMA},
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
        ctx.append("Study area and Census profile (JSON, for your reading only):\n" + _clip_json(area, 2500))
    if artifact is not None:
        ctx.append("Current step data (JSON, for your reading only; never quote its "
                   "keys or ids to the analyst):\n" + _clip_json(artifact, 6000))
    else:
        ctx.append("The current step has not been run yet (no artifact drafted).")

    convo = []
    for turn in (history or [])[-6:]:
        who = "User" if turn.get("role") == "user" else "Assistant"
        convo.append(f"{who}: {turn.get('content', '')}")
    convo.append(f"User: {message}")

    prompt = "\n\n".join(ctx) + "\n\n---\n\n" + "\n".join(convo) + "\n\nAssistant:"
    system = _CHAT_SYSTEM + ((" " + study_area_interface_note()) if step == 1 else "")
    reply, usage = _llm_text(system, prompt)
    from backend import hcas_ai

    hcas_ai.record(pid, step, "chat", {"message": message, "earlier_turns": len(history or [])},
                   {"reply": reply, "usage": usage}, hcas_ai.context_for(project), db=db)
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
    reply, usage = _llm_text(_INTAKE_SYSTEM + " " + study_area_interface_note(), "\n".join(convo) + "\n\nAssistant:")
    return {"reply": reply, "usage": usage}
