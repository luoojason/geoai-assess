"""GeoAssess — FastAPI application entry point."""

import asyncio
import logging
import sys
from pathlib import Path

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
    compute_far,
    compute_parking_coverage,
    compute_pedestrian_density,
    compute_transit_access,
    compute_facility_access,
    render_assessment_map,
    generate_interventions,
)

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


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


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
    bbox: list[float] | None = None  # [west, south, east, north] in EPSG:4326


class AssessResponse(BaseModel):
    metrics: dict
    map_html: str
    study_area_display: str
    interventions: list


@app.post("/assess", response_model=AssessResponse, dependencies=_assess_deps)
async def assess(body: AssessRequest):
    """Run a structured sub-tract community assessment for a county or custom bbox."""
    metrics, interventions, map_html, study_area_display = await _run_assessment(body)
    return AssessResponse(
        metrics=metrics,
        map_html=map_html,
        study_area_display=study_area_display,
        interventions=interventions,
    )


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


async def _run_assessment(body: AssessRequest) -> tuple[dict, list, str, str]:
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
        metrics = {
            "building_coverage_ratio": compute_building_coverage(buildings, study_area),
            "greenspace_coverage_ratio": greenspace_metrics["ratio"],
            "greenspace_area_m2": greenspace_metrics["area_m2"],
            "road_density_km_per_km2": compute_road_density(roads, study_area),
            "far": compute_far(buildings, study_area),
            "parking_coverage_ratio": compute_parking_coverage(parking, study_area),
            "pedestrian_density_km_per_km2": compute_pedestrian_density(sidewalks, study_area),
            "transit_stop_count": transit_metrics["stop_count"],
            "transit_stop_density_per_km2": transit_metrics["stop_density_per_km2"],
            "facility_count": facility_metrics["facility_count"],
            "facility_density_per_km2": facility_metrics["facility_density_per_km2"],
        }
        interventions = generate_interventions(metrics)
        map_html = await asyncio.to_thread(
            render_assessment_map, study_area, buildings, greenspace, roads, metrics,
            parking, sidewalks, transit_stops, facilities,
        )
    except Exception as exc:
        logger.warning(f"OSM fetch/render failed: {exc} — returning partial result")
        _empty = _gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")
        metrics = {
            "building_coverage_ratio": 0.0,
            "greenspace_coverage_ratio": 0.0,
            "greenspace_area_m2": 0.0,
            "road_density_km_per_km2": 0.0,
            "far": 0.0,
            "parking_coverage_ratio": 0.0,
            "pedestrian_density_km_per_km2": 0.0,
            "transit_stop_count": 0,
            "transit_stop_density_per_km2": 0.0,
            "facility_count": 0,
            "facility_density_per_km2": 0.0,
        }
        interventions = generate_interventions(metrics)
        map_html = await asyncio.to_thread(
            render_assessment_map, study_area, _empty, _empty, _empty, metrics,
            _empty, _empty, _empty, _empty,
        )

    return metrics, interventions, map_html, study_area_display


@app.post("/report", response_model=ReportResponse, dependencies=_assess_deps)
async def report(body: AssessRequest):
    """Run full assessment and return structured report JSON."""
    from datetime import datetime, timezone

    metrics, interventions, map_html, study_area_display = await _run_assessment(body)

    high = [i for i in interventions if i["severity"] == "high"]
    medium = [i for i in interventions if i["severity"] == "medium"]
    low = [i for i in interventions if i["severity"] == "low"]

    return ReportResponse(
        area=study_area_display,
        generated_at=datetime.now(timezone.utc).isoformat(),
        metrics=metrics,
        intervention_count=len(interventions),
        high_severity_count=len(high),
        medium_severity_count=len(medium),
        low_severity_count=len(low),
        top_issues=[i["finding"] for i in high],
        map_html=map_html,
    )


class CompareRequest(BaseModel):
    area_a: AssessRequest
    area_b: AssessRequest


class CompareResponse(BaseModel):
    area_a: AssessResponse
    area_b: AssessResponse
    diff: dict


@app.post("/compare", response_model=CompareResponse, dependencies=_assess_deps)
async def compare(body: CompareRequest):
    """Run two assessments concurrently and return a metric diff (area_b - area_a)."""
    (metrics_a, interventions_a, map_a, display_a), (metrics_b, interventions_b, map_b, display_b) = (
        await asyncio.gather(
            _run_assessment(body.area_a),
            _run_assessment(body.area_b),
        )
    )

    diff = {
        k: (metrics_b[k] - metrics_a[k])
        for k in metrics_a
        if isinstance(metrics_a[k], (int, float)) and k in metrics_b
    }

    return CompareResponse(
        area_a=AssessResponse(
            metrics=metrics_a,
            map_html=map_a,
            study_area_display=display_a,
            interventions=interventions_a,
        ),
        area_b=AssessResponse(
            metrics=metrics_b,
            map_html=map_b,
            study_area_display=display_b,
            interventions=interventions_b,
        ),
        diff=diff,
    )


# ---------------------------------------------------------------------------
# HCAS — 8-step gated assessment (backend/hcas.py)
# ---------------------------------------------------------------------------

@app.get("/hcas", response_class=HTMLResponse)
async def hcas_wizard(request: Request):
    return templates.TemplateResponse(request, "hcas.html")


class HcasProjectRequest(BaseModel):
    title: str
    state_fips: str
    county_fips: str
    gate_mode: str = "gated"


class HcasConfirmRequest(BaseModel):
    human_final: dict
    edit_seconds: float | None = None


@app.post("/hcas/projects", dependencies=_assess_deps)
async def hcas_create_project(body: HcasProjectRequest):
    try:
        pid = hcas.create_project(body.title, body.state_fips, body.county_fips, body.gate_mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"project_id": pid}


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


# No CSRF-header dep on the two GETs below: they are read-only and opened as
# plain browser links (a target=_blank anchor can't send X-Requested-With).
_hcas_link_deps = [Depends(require_rate_limit), Depends(require_basic_auth)]


@app.get("/hcas/projects/{pid}/map", response_class=HTMLResponse, dependencies=_hcas_link_deps)
async def hcas_map(pid: str):
    try:
        return HTMLResponse(hcas.get_map(pid))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/hcas/audit.csv", dependencies=_hcas_link_deps)
async def hcas_audit_csv():
    from fastapi.responses import Response

    return Response(hcas.export_audit_csv(), media_type="text/csv")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.app:app", host=settings.host, port=settings.port, reload=settings.debug)
