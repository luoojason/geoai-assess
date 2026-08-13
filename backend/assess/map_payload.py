"""Grid-bucketed map payload for viewport rendering.

Pure data in, pure data out — no folium, no browser, no I/O — so the thing that
decides what the map contains is testable without rendering anything.

Geometry is stored once per feature-part in `geoms`. Each `geoms` entry is a
LIST OF RINGS — ring 0 the exterior, any remaining rings the interior holes —
so a polygon with a hole round-trips as a polygon with a void (Leaflet's
`L.polygon(rings)` renders that natively), never as a solid hole-shaped patch
sitting on top of the feature. A multipart geometry (e.g. a MultiPolygon)
contributes one `geoms` entry per part. A feature-part touching several grid
cells has its INDEX repeated in each cell, never its coordinates: that is what
lets a building straddling a cell boundary draw whole without doubling the file.
"""
from __future__ import annotations

import math

import geopandas as gpd

# Set by the 2026-08-12 payload measurement — see
# docs/measurements/2026-08-12-map-payload-sizes.md. Real Delaware County
# building centroids put 516 features in the densest cell at 0.007 deg, the
# closest of the candidates measured to the ~500 target (a tenth of the
# 5,000-feature draw budget).
GRID_CELL_DEG = 0.007


def _cell_key(row: int, col: int) -> str:
    return f"{row}_{col}"


def _cells_for_bounds(minx, miny, maxx, maxy, cell_deg: float) -> list:
    """Every cell key a bounding box touches."""
    c0, c1 = int(math.floor(minx / cell_deg)), int(math.floor(maxx / cell_deg))
    r0, r1 = int(math.floor(miny / cell_deg)), int(math.floor(maxy / cell_deg))
    return [_cell_key(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]


def _parts(geom) -> list:
    """Flatten any geometry to a list of parts.

    Each part is itself a list of coordinate rings: `[exterior, interior1,
    interior2, ...]` for a Polygon (interior rings are its holes), or a
    single-ring list `[ring]` for LineString/LinearRing/Point. A Multi* or
    GeometryCollection contributes one part per member — a 2-part
    MultiPolygon yields 2 parts here, each carrying its own exterior (and
    any holes), never merged into one.
    """
    gtype = geom.geom_type
    if gtype == "Polygon":
        rings = [list(geom.exterior.coords)]
        rings.extend(list(interior.coords) for interior in geom.interiors)
        return [rings]
    if gtype in ("LineString", "LinearRing"):
        return [[list(geom.coords)]]
    if gtype == "Point":
        return [[[(geom.x, geom.y)]]]
    if gtype.startswith("Multi") or gtype == "GeometryCollection":
        out = []
        for part in geom.geoms:
            out.extend(_parts(part))
        return out
    return []


def build_payload(gdf: gpd.GeoDataFrame, cell_deg: float = GRID_CELL_DEG) -> dict:
    """Bucket every feature into a lat/lon grid. Returns geoms + cell index.

    Each entry in `geoms` is a list of rings — `[exterior, hole1, hole2,
    ...]` — so a polygon's holes survive as interior rings on the same
    entry rather than being dropped or drawn as solid patches. `total`
    counts these entries, i.e. geometry PARTS, not input features: a
    multipart geometry (e.g. a MultiPolygon building) contributes one
    entry per part, each with its own exterior (and any holes) intact.
    Measured 2026-08-12 by running this function against the real
    Delaware County buildings layer (source data documented in
    docs/measurements/2026-08-12-map-payload-sizes.md): 26,393 building
    features produce 26,394 `geoms` entries (one building is a
    MultiPolygon), of which 35 entries carry a hole — 48 interior rings
    in total, none of them dropped.

    A part's grid-cell membership is computed from its exterior ring
    only (ring 0), not its holes: a hole is inside its exterior by
    definition, so it can never widen the bounding box, and skipping it
    is cheaper.

    Coordinates are rounded to 6 decimal places before being stored. Keeping
    the rounding: it measurably shrinks the payload on its own (see
    docs/measurements/2026-08-12-map-payload-sizes.md's Step 2 table — 14-19%
    smaller per layer at 6dp versus full float precision). This docstring
    used to also claim the rounding is "what keeps the full three-layer
    render under the 25 MB payload budget (21.88 MB rounded vs 25.39 MB
    unrounded)" — that pair of numbers was measured against a REJECTED
    design (the sampling cap removed entirely, rendered through plain
    folium.GeoJson with no clip, no grid, no gate), never against what
    ships, so the claim was true of a design nobody shipped. What actually
    ships (this grid-bucketed payload, drawn at every zoom by the viewport module,
    clipped to the study area) renders the same real Delaware County
    buildings + roads + sidewalks at 11,077,515 bytes (11.08 MB) total — see
    docs/measurements/2026-08-12-map-viewport-verification.md. There is no
    separate compact/binary encoder; plain rounded GeoJSON-style coordinates
    are sufficient on their own.
    """
    empty = {"geoms": [], "cells": {}, "total": 0, "cell_deg": cell_deg}
    if gdf is None or len(gdf) == 0:
        return empty
    g = gdf[["geometry"]].to_crs("EPSG:4326")
    g = g[~g.geometry.is_empty & g.geometry.notna()]
    if len(g) == 0:
        return empty

    geoms: list = []
    cells: dict = {}
    for geom in g.geometry:
        # One entry per geometry part keeps the client decoder trivial; a
        # multipart feature contributes its parts as separate entries, which
        # is why `total` counts parts and can exceed len(gdf).
        for rings in _parts(geom):
            i = len(geoms)
            entry = [[[round(x, 6), round(y, 6)] for x, y in ring] for ring in rings]
            geoms.append(entry)
            exterior = entry[0]
            xs = [p[0] for p in exterior]
            ys = [p[1] for p in exterior]
            for key in _cells_for_bounds(min(xs), min(ys), max(xs), max(ys), cell_deg):
                cells.setdefault(key, []).append(i)
    return {"geoms": geoms, "cells": cells, "total": len(geoms), "cell_deg": cell_deg}
