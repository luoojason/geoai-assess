"""Grid-bucketed map payload for viewport rendering.

Pure data in, pure data out — no folium, no browser, no I/O — so the thing that
decides what the map contains is testable without rendering anything.

Geometry is stored once per feature in `geoms`. A feature touching several grid
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


def _rings(geom) -> list:
    """Flatten any geometry to a list of coordinate rings."""
    gtype = geom.geom_type
    if gtype == "Polygon":
        return [list(geom.exterior.coords)]
    if gtype in ("LineString", "LinearRing"):
        return [list(geom.coords)]
    if gtype == "Point":
        return [[(geom.x, geom.y)]]
    if gtype.startswith("Multi") or gtype == "GeometryCollection":
        out = []
        for part in geom.geoms:
            out.extend(_rings(part))
        return out
    return []


def build_payload(gdf: gpd.GeoDataFrame, cell_deg: float = GRID_CELL_DEG) -> dict:
    """Bucket every feature into a lat/lon grid. Returns geoms + cell index.

    `total` counts RINGS, not features: a multipart geometry (e.g. a
    MultiPolygon building) contributes one ring per part, each stored as its
    own entry in `geoms`. Measured 2026-08-12 against real Delaware County
    data, this only diverges from the feature count by a handful of parts
    (26,442 rings vs 26,393 buildings) — see
    docs/measurements/2026-08-12-map-payload-sizes.md.

    Coordinates are rounded to 6 decimal places before being stored. That
    rounding is load-bearing, not incidental: measured 2026-08-12, it is what
    keeps the full three-layer render (buildings + roads + sidewalks) under
    the 25 MB payload budget (21.88 MB rounded vs 25.39 MB unrounded) — see
    the same measurement doc. There is no separate compact/binary encoder;
    plain rounded GeoJSON-style coordinates are sufficient on their own.
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
        # One flat ring per entry keeps the client decoder trivial; a multipart
        # feature contributes its parts as separate entries, which is why
        # `total` counts rings and can exceed len(gdf).
        for ring in _rings(geom):
            i = len(geoms)
            pts = [[round(x, 6), round(y, 6)] for x, y in ring]
            geoms.append(pts)
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            for key in _cells_for_bounds(min(xs), min(ys), max(xs), max(ys), cell_deg):
                cells.setdefault(key, []).append(i)
    return {"geoms": geoms, "cells": cells, "total": len(geoms), "cell_deg": cell_deg}
