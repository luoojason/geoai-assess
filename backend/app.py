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

from backend.config import settings
from backend.auth import require_basic_auth, require_geoai_header, require_rate_limit
from backend.gis.counties import lookup_county, list_states, list_counties
from backend.assess import (
    fetch_buildings,
    fetch_greenspace,
    fetch_road_network,
    compute_building_coverage,
    compute_greenspace_coverage,
    compute_road_density,
    render_assessment_map,
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
    return templates.TemplateResponse("index.html", {"request": request})


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


@app.post("/assess", response_model=AssessResponse, dependencies=_assess_deps)
async def assess(body: AssessRequest):
    """Run a structured sub-tract community assessment for a county or custom bbox.

    Fetches OSM buildings, greenspace, and road network; computes coverage and
    density metrics; renders a multi-layer Folium map. No LLM calls are made.
    """
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
        buildings, greenspace, roads = await asyncio.gather(
            asyncio.to_thread(fetch_buildings, osm_bbox),
            asyncio.to_thread(fetch_greenspace, osm_bbox),
            asyncio.to_thread(fetch_road_network, osm_bbox),
        )
        greenspace_metrics = compute_greenspace_coverage(greenspace, study_area)
        metrics = {
            "building_coverage_ratio": compute_building_coverage(buildings, study_area),
            "greenspace_coverage_ratio": greenspace_metrics["ratio"],
            "greenspace_area_m2": greenspace_metrics["area_m2"],
            "road_density_km_per_km2": compute_road_density(roads, study_area),
        }
        map_html = await asyncio.to_thread(
            render_assessment_map, study_area, buildings, greenspace, roads, metrics
        )
    except Exception as exc:
        logger.warning(f"/assess OSM fetch/render failed: {exc} — returning partial result")
        _empty = _gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")
        metrics = {
            "building_coverage_ratio": 0.0,
            "greenspace_coverage_ratio": 0.0,
            "greenspace_area_m2": 0.0,
            "road_density_km_per_km2": 0.0,
        }
        map_html = await asyncio.to_thread(
            render_assessment_map, study_area, _empty, _empty, _empty, metrics
        )

    return AssessResponse(metrics=metrics, map_html=map_html, study_area_display=study_area_display)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.app:app", host=settings.host, port=settings.port, reload=settings.debug)
