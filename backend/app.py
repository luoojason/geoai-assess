"""GeoAssess — FastAPI application entry point."""

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend import hcas
from backend.config import settings
from backend.auth import require_basic_auth, require_geoai_header, require_rate_limit
from backend.gis.counties import list_states, list_counties
from backend.assess import (
    fetch_buildings,
    fetch_greenspace,
    fetch_road_network,
    fetch_parking,
    fetch_sidewalks,
    fetch_transit_stops,
    fetch_facilities,
    compute_building_coverage,
    compute_greenspace_coverage,
    compute_road_density,
    compute_parking_coverage,
    compute_pedestrian_density,
    compute_transit_access,
    compute_facility_access,
    compute_far_detail,
    render_assessment_map,
    generate_interventions,
)
from backend.assess.metrics import unmeasured_metric_keys
from backend.assess.netdiag import collect_fetch_errors

logger = logging.getLogger(__name__)

app = FastAPI(title="GeoAssess", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="frontend/static"), name="static")
templates = Jinja2Templates(directory="frontend/templates")

_assess_deps = [
    Depends(require_geoai_header),
    Depends(require_rate_limit),
    Depends(require_basic_auth),
]

# The step-progress endpoint is polled once or twice a second while a step runs,
# so it must NOT count against the abuse rate limit — otherwise a single normal
# 30–60s step exhausts the per-minute budget and the app 429s its own polling and
# the next run/confirm. Same header/auth checks, no rate limit.
_hcas_poll_deps = [
    Depends(require_geoai_header),
    Depends(require_basic_auth),
]

# For routes a browser reaches by navigation rather than by fetch(): the wizard
# page itself, and the read-only GETs opened as plain links (a target=_blank
# anchor cannot send X-Requested-With, so no CSRF-header dep here).
#
# The page MUST carry require_basic_auth even though it only renders HTML. A
# browser prompts for Basic credentials on a 401 it gets while navigating, and
# never on one it gets from fetch(). Leave the page open and a hosted deploy
# hands the visitor a working-looking wizard in which every action fails 401
# with no way to enter the password.
_hcas_link_deps = [Depends(require_rate_limit), Depends(require_basic_auth)]


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    from fastapi.responses import FileResponse

    return FileResponse("frontend/static/favicon.ico", media_type="image/x-icon")


@app.get("/healthz", include_in_schema=False)
async def healthz():
    """Liveness probe for a hosted deploy (see DEPLOY.md).

    Deliberately dependency-free: a platform health check cannot send the
    X-Requested-With header or Basic credentials, and locking it behind them
    would make every deploy look dead. It reports nothing but that the process
    is up — no project data, no configuration, no key state.
    """
    return {"status": "ok"}


@app.get("/states")
async def get_states():
    return {"states": list_states()}


@app.get("/counties/{state_fips}")
async def get_counties(state_fips: str):
    counties = list_counties(state_fips)
    if not counties:
        raise HTTPException(status_code=404, detail=f"No counties found for state FIPS {state_fips}")
    return {"state_fips": state_fips, "counties": counties}


class AssessRequest(BaseModel):
    state_fips: str
    county_fips: str
    bbox: Optional[list[float]] = None  # [west, south, east, north] in EPSG:4326


class AssessResponse(BaseModel):
    metrics: dict
    map_html: str
    study_area_display: str
    interventions: list
    # Honest-degradation channel, same shape the HCAS wizard already uses:
    # fetch_errors maps a failed layer to a plain-language reason, and every
    # metric derived from it is null in `metrics` and named in
    # unmeasured_metrics. A null is never a measurement of zero.
    fetch_errors: dict = {}
    unmeasured_metrics: list[str] = []
    map_note: Optional[str] = None


@app.post("/assess", response_model=AssessResponse, dependencies=_assess_deps)
async def assess(body: AssessRequest):
    """Run a structured sub-tract community assessment for a county or custom bbox."""
    return (await _run_assessment(body)).response()


class ReportResponse(BaseModel):
    area: str
    generated_at: str
    metrics: dict
    intervention_count: int
    high_severity_count: int
    medium_severity_count: int
    low_severity_count: int
    top_issues: list[str]
    map_html: str
    fetch_errors: dict = {}
    unmeasured_metrics: list[str] = []
    map_note: Optional[str] = None


class _Assessment:
    """One assessment run plus what went wrong during it.

    `metrics` carries None for any metric whose source layer failed to fetch;
    `interventions` is generated only from the metrics that were actually
    measured, so a data outage can never manufacture a public-health finding.
    """

    def __init__(self, metrics, interventions, map_html, display, fetch_errors,
                 unmeasured, map_note):
        self.metrics = metrics
        self.interventions = interventions
        self.map_html = map_html
        self.display = display
        self.fetch_errors = fetch_errors
        self.unmeasured = unmeasured
        self.map_note = map_note

    def response(self) -> AssessResponse:
        return AssessResponse(
            metrics=self.metrics,
            map_html=self.map_html,
            study_area_display=self.display,
            interventions=self.interventions,
            fetch_errors=self.fetch_errors,
            unmeasured_metrics=self.unmeasured,
            map_note=self.map_note,
        )


async def _run_assessment(body: AssessRequest) -> "_Assessment":
    """Shared assessment logic used by /assess, /report, and /compare."""
    import geopandas as _gpd
    from shapely.geometry import box as _box

    if body.bbox is not None:
        if len(body.bbox) != 4:
            raise HTTPException(status_code=422, detail="bbox must be [west, south, east, north]")
        west, south, east, north = body.bbox
        study_area = _gpd.GeoDataFrame(
            geometry=[_box(west, south, east, north)], crs="EPSG:4326"
        ).to_crs("EPSG:5070")
        osm_bbox = (west, south, east, north)
        study_area_display = f"Custom area ({body.state_fips}{body.county_fips})"
    else:
        from backend.gis.collector_lite import collect_county_boundaries
        counties = await asyncio.to_thread(collect_county_boundaries, body.state_fips)
        target_geoid = f"{body.state_fips}{body.county_fips}"
        county_row = counties[counties["GEOID"] == target_geoid]
        if county_row.empty:
            raise HTTPException(
                status_code=404,
                detail=f"County GEOID '{target_geoid}' not found for state '{body.state_fips}'",
            )
        county_proj = county_row.to_crs("EPSG:5070")
        centroid = county_proj.geometry.centroid.iloc[0]
        study_area = _gpd.GeoDataFrame(geometry=[centroid.buffer(5000)], crs="EPSG:5070")
        buf_4326 = study_area.to_crs("EPSG:4326")
        bounds = buf_4326.geometry.total_bounds
        osm_bbox = (bounds[0], bounds[1], bounds[2], bounds[3])
        study_area_display = county_row.iloc[0].get("NAME", target_geoid)

    # Three separate concerns, three separate failure policies. They used to
    # share one try/except, so a map-render failure rebound `metrics` to an
    # all-zeros literal and shipped it as a complete 200 assessment — a
    # fabricated public-health finding. Never again: fetch+compute failing
    # makes the whole response meaningless, so it 502s; the map failing loses
    # only the map.
    with collect_fetch_errors() as fetch_errors:
        try:
            buildings, greenspace, roads, parking, sidewalks, transit_stops, facilities = await asyncio.gather(
                asyncio.to_thread(fetch_buildings, osm_bbox),
                asyncio.to_thread(fetch_greenspace, osm_bbox),
                asyncio.to_thread(fetch_road_network, osm_bbox),
                asyncio.to_thread(fetch_parking, osm_bbox),
                asyncio.to_thread(fetch_sidewalks, osm_bbox),
                asyncio.to_thread(fetch_transit_stops, osm_bbox),
                asyncio.to_thread(fetch_facilities, osm_bbox),
            )
            greenspace_metrics = compute_greenspace_coverage(greenspace, study_area)
            transit_metrics = compute_transit_access(transit_stops, study_area)
            facility_metrics = compute_facility_access(facilities, study_area)
            far_metrics = compute_far_detail(buildings, study_area)
            metrics = {
                "building_coverage_ratio": compute_building_coverage(buildings, study_area),
                "greenspace_coverage_ratio": greenspace_metrics["ratio"],
                "greenspace_area_m2": greenspace_metrics["area_m2"],
                "road_density_km_per_km2": compute_road_density(roads, study_area),
                "far": far_metrics["far"],
                "levels_tagged_pct": far_metrics["levels_tagged_pct"],
                "parking_coverage_ratio": compute_parking_coverage(parking, study_area),
                "pedestrian_density_km_per_km2": compute_pedestrian_density(sidewalks, study_area),
                "transit_stop_count": transit_metrics["stop_count"],
                "transit_stop_density_per_km2": transit_metrics["stop_density_per_km2"],
                "facility_count": facility_metrics["facility_count"],
                "facility_density_per_km2": facility_metrics["facility_density_per_km2"],
            }
        except Exception as exc:
            # No usable numbers exist, so there is nothing honest to return.
            logger.exception("assessment fetch/compute failed")
            raise HTTPException(
                status_code=502,
                detail=(
                    "The assessment could not be computed "
                    f"({type(exc).__name__}: {exc}). No metrics were produced — "
                    "nothing in this response would have been a measurement. "
                    "Check the network self-test (CHECK-NETWORK.bat) and retry."
                ),
            )

    # A layer that failed to fetch came back empty, and every metric derived
    # from it computed a plausible 0.0. Mark those null rather than publish a
    # zero a reader would take for "this county has no parks".
    unmeasured = [k for k in unmeasured_metric_keys(fetch_errors) if k in metrics]
    for key in unmeasured:
        metrics[key] = None
    measured = {k: v for k, v in metrics.items() if v is not None}
    interventions = generate_interventions(measured)

    map_note = None
    try:
        map_html = await asyncio.to_thread(
            render_assessment_map, study_area, buildings, greenspace, roads, metrics,
            parking, sidewalks, transit_stops, facilities,
        )
    except Exception as exc:
        # The map is an illustration of the metrics, not their source. Losing
        # it must not disturb a single number above.
        logger.exception("assessment map render failed")
        map_html = ""
        map_note = (
            f"The map could not be rendered ({type(exc).__name__}: {exc}). "
            "The metrics in this response are unaffected."
        )

    # Layer names read better than fetcher function names for a human.
    friendly_errors = {
        fn.replace("fetch_", "").replace("_network", ""): msg
        for fn, msg in fetch_errors.items()
    }
    return _Assessment(
        metrics, interventions, map_html, study_area_display,
        friendly_errors, unmeasured, map_note,
    )


@app.post("/report", response_model=ReportResponse, dependencies=_assess_deps)
async def report(body: AssessRequest):
    """Run full assessment and return structured report JSON."""
    from datetime import datetime, timezone

    run = await _run_assessment(body)
    interventions = run.interventions

    high = [i for i in interventions if i["severity"] == "high"]
    medium = [i for i in interventions if i["severity"] == "medium"]
    low = [i for i in interventions if i["severity"] == "low"]

    return ReportResponse(
        area=run.display,
        generated_at=datetime.now(timezone.utc).isoformat(),
        metrics=run.metrics,
        intervention_count=len(interventions),
        high_severity_count=len(high),
        medium_severity_count=len(medium),
        low_severity_count=len(low),
        top_issues=[i["finding"] for i in high],
        map_html=run.map_html,
        fetch_errors=run.fetch_errors,
        unmeasured_metrics=run.unmeasured,
        map_note=run.map_note,
    )


class CompareRequest(BaseModel):
    area_a: AssessRequest
    area_b: AssessRequest


class CompareResponse(BaseModel):
    area_a: AssessResponse
    area_b: AssessResponse
    diff: dict
    # Metrics absent from `diff` because at least one side was not measured.
    # Without this the reader sees a shorter diff and assumes the rest matched.
    undiffable_metrics: list[str] = []


@app.post("/compare", response_model=CompareResponse, dependencies=_assess_deps)
async def compare(body: CompareRequest):
    """Run two assessments concurrently and return a metric diff (area_b - area_a)."""
    run_a, run_b = await asyncio.gather(
        _run_assessment(body.area_a),
        _run_assessment(body.area_b),
    )
    metrics_a, metrics_b = run_a.metrics, run_b.metrics

    # A metric that was not measured on either side cannot be differenced.
    # Subtracting an unmeasured side would invent the entire delta.
    comparable = [k for k in metrics_a if k in metrics_b]
    diff = {
        k: (metrics_b[k] - metrics_a[k])
        for k in comparable
        if isinstance(metrics_a[k], (int, float)) and isinstance(metrics_b[k], (int, float))
    }
    undiffable = sorted(set(comparable) - set(diff))

    return CompareResponse(
        area_a=run_a.response(),
        area_b=run_b.response(),
        diff=diff,
        undiffable_metrics=undiffable,
    )


# ---------------------------------------------------------------------------
# HCAS — 8-step gated assessment (backend/hcas.py)
# ---------------------------------------------------------------------------

@app.get("/hcas", response_class=HTMLResponse, dependencies=_hcas_link_deps)
async def hcas_wizard(request: Request):
    return templates.TemplateResponse(request, "hcas.html")


class HcasProjectRequest(BaseModel):
    title: str
    state_fips: str
    county_fips: str
    gate_mode: str = "gated"
    areal_unit: Optional[str] = None
    location_query: str = ""
    notes: str = ""


class HcasScopeRequest(BaseModel):
    """Re-definition of an existing project (step-1 Back button)."""
    title: Optional[str] = None
    state_fips: Optional[str] = None
    county_fips: Optional[str] = None
    areal_unit: Optional[str] = None
    location_query: Optional[str] = None
    notes: Optional[str] = None


class HcasConfirmRequest(BaseModel):
    human_final: dict
    edit_seconds: Optional[float] = None


class HcasChatRequest(BaseModel):
    message: str
    step: int = 1
    history: list[dict] = []


class HcasIntakeChatRequest(BaseModel):
    message: str
    history: list[dict] = []


class HcasAreaRequest(BaseModel):
    text: str


class HcasReviseRequest(BaseModel):
    artifact: dict
    instruction: str
    place: str = "the study area"


@app.get("/hcas/areal-units", dependencies=_assess_deps)
async def hcas_areal_units():
    """The reporting units a project may choose (Prof. Shi 2026-08-07 #5)."""
    from backend.gis.profile import AREAL_UNITS, DEFAULT_AREAL_UNIT

    return {
        "default": DEFAULT_AREAL_UNIT,
        "units": [{"id": k, "label": v["label"]} for k, v in AREAL_UNITS.items()],
    }


@app.post("/hcas/projects", dependencies=_assess_deps)
async def hcas_create_project(body: HcasProjectRequest):
    try:
        pid = hcas.create_project(
            body.title, body.state_fips, body.county_fips, body.gate_mode,
            areal_unit=body.areal_unit, location_query=body.location_query,
            notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"project_id": pid}


@app.patch("/hcas/projects/{pid}", dependencies=_assess_deps)
async def hcas_update_project(pid: str, body: HcasScopeRequest):
    """Re-define a project's scope in place. Marks every drafted step stale."""
    try:
        return hcas.update_project_scope(
            pid, title=body.title, state_fips=body.state_fips,
            county_fips=body.county_fips, areal_unit=body.areal_unit,
            location_query=body.location_query, notes=body.notes,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown project {pid}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/hcas/resolve-area", dependencies=_assess_deps)
async def hcas_resolve_area(body: HcasAreaRequest):
    """Free-text location -> study-area county (Prof. Shi 2026-08-07 #4)."""
    try:
        return await asyncio.to_thread(hcas.resolve_area, body.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/hcas/chat", dependencies=_assess_deps)
async def hcas_intake_chat(body: HcasIntakeChatRequest):
    """Assistant before a project exists (Prof. Shi 2026-08-07 #3)."""
    try:
        return await asyncio.to_thread(hcas.chat_intake, body.message, body.history)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        logger.exception("intake chat failed")
        raise HTTPException(
            status_code=502,
            detail=f"The assistant hit an unexpected error ({type(exc).__name__}).",
        )


@app.post("/hcas/revise-factors", dependencies=_assess_deps)
async def hcas_revise_factors(body: HcasReviseRequest):
    """Incremental edit of the step-2 factor list (Prof. Shi 2026-08-07 #11)."""
    try:
        return await asyncio.to_thread(
            hcas.revise_factors, body.artifact, body.instruction, body.place
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/hcas/projects", dependencies=_assess_deps)
async def hcas_list_projects():
    """Saved projects, newest first — what the Open dialog lists.

    Work was always persisted step by step, but reopening required typing the
    12-character project id, so closing the tab without noting it lost the
    project in practice even though nothing was lost on disk.
    """
    return {"projects": hcas.list_projects()}


@app.get("/hcas/projects/{pid}", dependencies=_assess_deps)
async def hcas_get_project(pid: str):
    try:
        return hcas.get_project(pid)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Unknown project {pid}")


@app.post("/hcas/projects/{pid}/steps/{step}/run", dependencies=_assess_deps)
async def hcas_run_step(pid: str, step: int):
    try:
        # to_thread: LLM-backed steps block for seconds-to-minutes
        return {"ai_draft": await asyncio.to_thread(hcas.run_step, pid, step)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except json.JSONDecodeError as exc:
        # A JSONDecodeError IS a ValueError, so without this it became a 400
        # reading "Expecting value: line 1 column 1 (char 0)" — the AI provider
        # returning something unparseable, reported as if the analyst had sent a
        # bad request. Caught before ValueError, which is a real 400 here.
        logger.error("step %s: unparseable AI response: %s", step, exc)
        raise HTTPException(
            status_code=502,
            detail="The AI provider returned a reply that could not be read. "
                   "Run the step again; if it keeps happening, the terminal "
                   "window has the raw response.",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        # The chat route already had this guard, added because an unhandled
        # exception reached Prof. Shi as a bare ASGI 500 with no message. The run
        # route is the one that matters more and did not have it: a schema
        # validation failure on the CLI path raises jsonschema.ValidationError,
        # which is none of the types above.
        logger.exception("step %s run failed with an unexpected error", step)
        raise HTTPException(
            status_code=502,
            detail=f"Step {step} hit an unexpected error ({type(exc).__name__}). "
                   "The terminal window has the full traceback.",
        )


@app.get("/hcas/projects/{pid}/steps/{step}/progress", dependencies=_hcas_poll_deps)
async def hcas_step_progress(pid: str, step: int):
    """Live progress of an in-flight step run (pct 0-100 + label + optional
    per-layer detail); exactly {"pct": 0, "label": ""} when idle. The `detail`
    key is omitted while empty so idle polls stay minimal."""
    out = hcas.get_progress(pid, step)
    detail = hcas.get_progress_detail(pid, step)
    if detail:
        out["detail"] = detail
    return out


@app.post("/hcas/projects/{pid}/run_all", dependencies=_assess_deps)
async def hcas_run_all(pid: str):
    """AI-only baseline: walk all 8 steps with auto-confirm (gate_mode='auto' only)."""
    try:
        return await asyncio.to_thread(hcas.run_auto, pid)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/hcas/projects/{pid}/steps/{step}/confirm", dependencies=_assess_deps)
async def hcas_confirm_step(pid: str, step: int, body: HcasConfirmRequest):
    try:
        return hcas.confirm_step(pid, step, body.human_final, body.edit_seconds)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/hcas/projects/{pid}/chat", dependencies=_assess_deps)
async def hcas_chat(pid: str, body: HcasChatRequest):
    """Live per-step assistant. A side-channel to the gated pipeline: it never
    writes an audit tuple, so the LLM-at-steps-2&4-only ablation dataset is
    unaffected by chat usage."""
    try:
        return await asyncio.to_thread(hcas.chat, pid, body.step, body.message, body.history)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        # Assistant is a side-channel — never let it crash as a raw ASGI 500.
        logger.exception("chat failed with an unexpected error")
        raise HTTPException(
            status_code=502,
            detail=(
                "The assistant hit an unexpected error "
                f"({type(exc).__name__}). The terminal window has the full traceback."
            ),
        )


@app.get("/hcas/projects/{pid}/map", response_class=HTMLResponse, dependencies=_hcas_link_deps)
async def hcas_map(pid: str):
    try:
        return HTMLResponse(hcas.get_map(pid))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get(
    "/hcas/basemap/{state_fips}/{county_fips}",
    response_class=HTMLResponse,
    dependencies=_hcas_link_deps,
)
async def hcas_basemap(state_fips: str, county_fips: str):
    """Study-area locator map (county boundary over base tiles), shown from step
    1. FIPS-scoped so the step-1 create form can preview it before a project
    exists. Opened in an <iframe>, so it carries no CSRF header (see link deps)."""
    try:
        return HTMLResponse(await asyncio.to_thread(hcas.basemap_for_county, state_fips, county_fips))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/hcas/audit.csv", dependencies=_hcas_link_deps)
async def hcas_audit_csv():
    from fastapi.responses import Response

    return Response(hcas.export_audit_csv(), media_type="text/csv")


@app.get("/hcas/projects/{pid}/data", dependencies=_assess_deps)
async def hcas_data_list(pid: str):
    """Per-layer cache/upload status — what step 3/7 would use right now
    without fetching, and what a download would contain."""
    try:
        return hcas.list_data_bundle(pid)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/hcas/projects/{pid}/data.zip", dependencies=_hcas_link_deps)
async def hcas_data_export(pid: str):
    """Download every layer this project has fetched or the user has uploaded
    — Xun Shi 2026-08-14 (#all-aigis): "allow the user to download and upload
    the data ... use their own special data if they want to.\""""
    from fastapi.responses import Response

    try:
        blob = hcas.export_data_bundle(pid)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return Response(
        blob, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="hcas-{pid}-data.zip"'},
    )


@app.post("/hcas/projects/{pid}/data", dependencies=_assess_deps)
async def hcas_data_import(pid: str, request: Request, filename: str):
    """Upload a .zip bundle (as produced by the export above) or a single
    .geojson file named after the layer it replaces, e.g. facilities.geojson.
    Read as a raw body rather than multipart/form-data so this needs no new
    dependency for what is, for most users, a one-off upload."""
    content = await request.body()
    if not content:
        raise HTTPException(status_code=400, detail="empty upload")
    try:
        return await asyncio.to_thread(hcas.import_data_bundle, pid, filename, content)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.delete("/hcas/projects/{pid}/data/{layer}", dependencies=_assess_deps)
async def hcas_data_clear(pid: str, layer: str):
    try:
        return hcas.clear_data_override(pid, layer)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.app:app", host=settings.host, port=settings.port, reload=settings.debug)
