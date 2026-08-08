"""Per-areal-unit tabulation of the assessment metrics.

Prof. Shi 2026-08-07 #5 asked to choose the reporting unit. Choosing one that
changed nothing about the output would have been a control that does nothing —
the same objection he raised about the Re-run button in the very next item — so
the metrics are computed for each unit here, not only for the county as a whole.

The unit metrics are produced by **the same computers as the county figure**,
with the unit polygon standing in for the study area. That is deliberate: one
definition, so a tract's greenspace ratio cannot drift from the county's by
using different code, and every honesty property already built into those
functions (clipping to the boundary, the FAR tagged-coverage flag, no clamping
of impossible values) applies unchanged.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Above this many units, tabulating every one costs more than it informs on a
# first pass (a large urban county runs to several hundred tracts). The cap is
# reported in the artifact so a truncated table can never read as a complete one.
MAX_UNITS = 400


def _subset(layer, geom):
    """Features whose bounding box meets `geom`, via the spatial index.

    Metrics clip to the polygon anyway, so anything dropped here would have
    contributed exactly zero — this only avoids intersecting 60k county-wide
    buildings against every one of 28 tracts.
    """
    if layer is None or layer.empty:
        return layer
    try:
        idx = layer.sindex.query(geom, predicate="intersects")
        return layer.iloc[idx]
    except Exception:  # no index available: correctness does not depend on it
        return layer


def tabulate_by_unit(units, layers, computers, metrics_wanted, crs="EPSG:5070",
                     progress=None) -> dict:
    """Compute `metrics_wanted` for every polygon in `units`.

    units     GeoDataFrame with GEOID/NAME and geometry (any CRS)
    layers    {layer_name: GeoDataFrame} as fetched for the whole study area
    computers {metric_name: fn(layers_dict, study_area_gdf) -> {key: value}},
              passed in rather than imported so this module stays free of the
              step machine
    Returns {"rows": [...], "truncated": bool, "unit_count": int}.
    """
    import geopandas as gpd

    units = units.to_crs(crs)
    total = len(units)
    truncated = total > MAX_UNITS
    if truncated:
        logger.warning(
            f"{total} units exceeds MAX_UNITS={MAX_UNITS}; tabulating the first {MAX_UNITS}"
        )
        units = units.iloc[:MAX_UNITS]

    projected = {n: (g.to_crs(crs) if g is not None and not g.empty and g.crs != crs else g)
                 for n, g in layers.items()}

    rows = []
    for i, (_, u) in enumerate(units.iterrows()):
        geom = u.geometry
        if geom is None or geom.is_empty:
            continue
        cell = gpd.GeoDataFrame(geometry=[geom], crs=crs)
        near = {n: _subset(g, geom) for n, g in projected.items()}
        values: dict = {}
        for m in metrics_wanted:
            fn = computers.get(m)
            if fn is None:
                continue
            try:
                values.update(fn(near, cell))
            except Exception as exc:  # one bad unit must not lose the table
                logger.warning(f"metric {m} failed for unit {u.get('GEOID')}: {exc!r}")
        rows.append({
            "geoid": str(u.get("GEOID", "")),
            "name": str(u.get("NAME") or u.get("BASENAME") or u.get("GEOID", "")),
            "area_km2": round(float(geom.area) / 1_000_000, 4),
            "metrics": {k: (round(v, 6) if isinstance(v, float) else v)
                        for k, v in values.items()},
        })
        if progress is not None and (i % 5 == 0 or i == len(units) - 1):
            progress(i + 1, len(units))

    return {"rows": rows, "truncated": truncated, "unit_count": total}
