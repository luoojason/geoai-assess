"""Real OpenStreetMap geometry, on disk, so tests never touch the network.

`county_excerpt/` holds a 0.056° square carved out of Adams County, Ohio
(FIPS 39001) around West Union, fetched 2026-08-13 through the app's own
fetchers. It is real data — real building footprints with real holes, real
LineString road geometry, real Point facilities — at 2,783 features across
seven non-empty layers, which is enough to exercise every code path the map
has without waiting on Overpass.

Why this exists: Overpass is a shared public service that turns roughly a
third of requests away under load, and on 2026-08-13 a single Adams layer took
524 s and arrived from a mirror 28 days stale. A test suite that fetches is a
test suite that fails for reasons that have nothing to do with the code, so it
does not fetch.

The `landuse` layer keeps its `landuse` tag column; every other layer is
geometry-only. That column is what the per-class colouring reads, and a
fixture stripped of it would let a class-colouring bug pass unnoticed.

`transit_stops` is deliberately EMPTY: Adams genuinely has none tagged. An
empty layer is a real case the renderer has to survive, and keeping it in the
fixture means that case is covered by default rather than by remembering to
write it.

Stored as gzipped GeoJSON rather than GeoParquet because pyarrow is not a
dependency of this project and a test fixture is not a reason to add one.
Everything here is stdlib plus geopandas.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

_DIR = Path(__file__).parent / "county_excerpt"

LAYERS = (
    "buildings", "greenspace", "roads", "parking",
    "sidewalks", "transit_stops", "facilities", "landuse",
)


def _read(name: str):
    import geopandas as gpd

    path = _DIR / f"{name}.geojson.gz"
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        raw = json.load(fh)
    feats = raw.get("features") or []
    if not feats:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")
    # from_features loses the CRS (GeoJSON is nominally 4326), but these were
    # written from EPSG:5070 frames and the coordinates are metres, so the CRS
    # is asserted rather than converted. Reprojecting here would silently move
    # every feature into the Gulf of Guinea.
    return gpd.GeoDataFrame.from_features(feats, crs="EPSG:5070")


def load_county_excerpt() -> dict:
    """All eight layers plus the study area, keyed by name, in EPSG:5070.

    Returns `{"study_area": gdf, "buildings": gdf, ...}`. Callers that want
    only the data layers can drop `study_area`.
    """
    out = {"study_area": _read("study_area")}
    for name in LAYERS:
        out[name] = _read(name)
    return out
