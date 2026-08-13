# HCAS Step-7 Map Viewport Rendering — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the step-7 assessment map show every building, road and sidewalk when the analyst zooms in, while staying a single self-contained HTML file that never misrepresents what it is drawing.

**Architecture:** All features are embedded in the page, bucketed into a lat/lon grid. Below a zoom gate the map draws a thinned overview with a legend line stating exactly what fraction is shown; above the gate a small inlined JS module looks up the grid cells intersecting the viewport and draws only those features onto a Leaflet canvas renderer. No server endpoint — the page must work when saved to disk.

**Tech Stack:** Python 3.9+, geopandas, shapely, folium 0.20.0, Leaflet (bundled by folium), pytest. No new dependencies. No node/JS toolchain exists in this repo and none is introduced.

## Global Constraints

- **Repo/branch:** `geoai-assess`, branch `hcas`. Spec: `docs/superpowers/specs/2026-08-12-hcas-map-viewport-rendering-design.md`.
- **Run the suite with:** `.venv/Scripts/python.exe -m pytest backend/tests -q` (Windows). Baseline before this plan: **279 passing**.
- **The map must work standalone.** No `fetch`, no XHR, no external URLs, no server endpoint. Everything inlined into the one HTML file.
- **No new Python or JS dependencies.**
- **Python 3.9 compatible** — `test_python39_compat.py` exists and must keep passing. No `match`, no `X | Y` runtime annotations in new code without `from __future__ import annotations`.
- **The map is a bonus, never fails the step.** Existing contract at `hcas.py:1574` — every new failure path degrades to the previous behaviour and logs, never raises.
- **Metrics must not change.** `metrics` and `by_unit` are computed from the full `layers` dict at `hcas.py:1543`. Nothing in this plan touches that path. Any task whose diff changes a metric value is wrong.
- **Layer scope:** buildings, roads, sidewalks only. greenspace, landuse, parking, facilities, transit_stops keep their existing `folium.GeoJson` path untouched.
- **Draw budget (the pass/fail for Task 2):** ≤ 5,000 features drawn per viewport update, and under 100 ms per update.
- **Hard file ceiling:** 25 MB of rendered HTML.

## File Structure

| File | Responsibility |
|---|---|
| `backend/assess/map_payload.py` | **new.** Pure data: bucket a GeoDataFrame into a grid, optionally encode coordinates. No folium, no I/O, no browser. |
| `frontend/static/viewport_layers.js` | **new.** Client: zoom gate, viewport→cell lookup, canvas draw, feature cache. |
| `backend/hcas.py:1580-1602` | Split `_slim_for_map` into `_overview_geoms` + `_detail_payload`. |
| `backend/assess/renderer.py` | Accept both payloads, inline the JS, emit the legend disclosure. |
| `backend/tests/test_map_payload_20260812.py` | **new.** Unit tests for `map_payload`. |
| `backend/tests/test_map_disclosure_20260812.py` | **new.** Tests that the legend states what the map is drawing. |
| `docs/measurements/2026-08-12-map-payload-sizes.md` | **new.** Task 2's recorded numbers. |

---

### Task 1: Legend states what the map is actually drawing

Ships the fix for the reported bug on its own. Independent of everything else — if the rest of this plan is descoped, this task alone stops a correct run from being read as a broken fetch.

**Do NOT write "zoom in for all" yet.** That is not true until Task 7 lands. This task ships only the count.

**Files:**
- Modify: `backend/hcas.py:1589-1602` (`_slim_for_map` returns counts)
- Modify: `backend/assess/renderer.py:79-99` (accept counts), `:262-272` (legend)
- Test: `backend/tests/test_map_disclosure_20260812.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `_slim_for_map(gdf) -> tuple[GeoDataFrame, dict]` where the dict is `{"shown": int, "total": int, "stride": int}`. `render_assessment_map(..., sample_info: dict | None = None)` where `sample_info` maps layer name to that dict.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_map_disclosure_20260812.py`:

```python
"""The step-7 map drew every Nth feature and never said so.

Delaware County 2026-08-12: 26,393 buildings fetched and measured, 3,300 drawn
(stride 8), and the legend showed only "not measured" / "not selected" — neither
of which applied. The metrics panel printed "Building Coverage: 47.2%" beside a
map showing 12.5% of the buildings, so the correct reading of that screen was
"the fetch broke". It had not.
"""
from __future__ import annotations

import geopandas as gpd
from shapely.geometry import Polygon

from backend import hcas


def _grid_gdf(n: int) -> gpd.GeoDataFrame:
    """n unit squares in a row, in metres (EPSG:5070, as the app uses)."""
    polys = [Polygon([(x, 0), (x + 1, 0), (x + 1, 1), (x, 1)]) for x in range(n)]
    return gpd.GeoDataFrame(geometry=polys, crs="EPSG:5070")


def test_slim_reports_how_many_it_kept():
    slim, info = hcas._slim_for_map(_grid_gdf(26393))
    assert info["total"] == 26393
    assert info["stride"] == 8
    assert info["shown"] == len(slim)
    assert info["shown"] < info["total"]


def test_slim_reports_no_sampling_when_under_the_cap():
    slim, info = hcas._slim_for_map(_grid_gdf(100))
    assert info == {"shown": 100, "total": 100, "stride": 1}
    assert len(slim) == 100


def test_legend_states_the_sample():
    from backend.assess.renderer import _sample_legend_line

    line = _sample_legend_line("buildings", {"shown": 3300, "total": 26393, "stride": 8})
    assert "3,300" in line
    assert "26,393" in line
    assert "1 in 8" in line
    # The promise the map cannot yet keep must not appear.
    assert "zoom in" not in line.lower()


def test_no_legend_line_when_nothing_was_dropped():
    from backend.assess.renderer import _sample_legend_line

    assert _sample_legend_line("buildings", {"shown": 100, "total": 100, "stride": 1}) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_map_disclosure_20260812.py -v`
Expected: FAIL — `_slim_for_map` returns a GeoDataFrame, not a tuple; `_sample_legend_line` does not exist.

- [ ] **Step 3: Make `_slim_for_map` report its sampling**

In `backend/hcas.py`, replace the body of `_slim_for_map` (currently at `:1589-1602`):

```python
def _slim_for_map(gdf):
    """Geometry-only, evenly capped, and simplified for a light browser payload.
    OSM tag columns (hundreds of mostly-NaN keys per feature) and full-precision
    vertices are what made raw Franklin County maps ~500 MB.

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
    stride = (n // _MAP_MAX_FEATURES) + 1 if n > _MAP_MAX_FEATURES else 1
    if stride > 1:
        # even stride keeps the sample spread across the area, not clustered
        g = g.iloc[::stride]
    g["geometry"] = g.geometry.simplify(_MAP_SIMPLIFY_M)
    g = g[~g.geometry.is_empty]
    return g, {"shown": len(g), "total": n, "stride": stride}
```

- [ ] **Step 4: Update the one existing call site**

In `backend/hcas.py`, replace line `1556`:

```python
        slim_pairs = {n: _slim_for_map(g) for n, g in layers.items()}
        slim = {n: pair[0] for n, pair in slim_pairs.items()}
        sample_info = {n: pair[1] for n, pair in slim_pairs.items()}
```

and add `sample_info=sample_info,` to the `render_assessment_map(...)` call that follows at `:1559-1569`.

- [ ] **Step 5: Add the legend line to the renderer**

In `backend/assess/renderer.py`, add above `render_assessment_map`:

```python
def _sample_legend_line(layer: str, info: dict | None) -> str:
    """One honest sentence about how much of a layer is drawn, or "" if all of it.

    Deliberately does NOT promise "zoom in for all" — the viewport renderer that
    would make that true is not built yet, and a legend that overstates the map
    is the bug this line exists to fix.
    """
    if not info or info.get("stride", 1) <= 1:
        return ""
    shown, total, stride = info["shown"], info["total"], info["stride"]
    return (f"<b>{layer}</b>: showing {shown:,} of {total:,} (1 in {stride}) to keep "
            f"the map fast. All {total:,} were measured and counted in the metrics above.")
```

Add `sample_info: dict | None = None,` as the last parameter of `render_assessment_map` (after `facilities`), and in the legend block at `:261-272` extend `keys`:

```python
    for _layer in ("buildings", "roads", "sidewalks"):
        _line = _sample_legend_line(_layer, (sample_info or {}).get(_layer))
        if _line:
            keys.append(_line)
```

- [ ] **Step 6: Run the new tests and the full suite**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_map_disclosure_20260812.py -v`
Expected: 4 passed.

Run: `.venv/Scripts/python.exe -m pytest backend/tests -q`
Expected: **283 passed** (279 baseline + 4). If any previously-passing test fails, it is calling `_slim_for_map` and expecting a bare GeoDataFrame — fix the call, not the test's intent.

- [ ] **Step 7: Commit**

```bash
git add backend/hcas.py backend/assess/renderer.py backend/tests/test_map_disclosure_20260812.py
git commit -m "fix(hcas): say on the map how much of each layer is drawn

Delaware 2026-08-12: 26,393 buildings measured, 3,300 drawn (stride 8), and
nothing on screen said so, beside a panel printing Building Coverage %. The
legend now states shown/total/stride for any sampled layer."
```

---

### Task 2: Measure — decide whether the compact encoding is needed at all

The 3,500 cap rests on a "~45 MB HTML, could freeze a low-spec laptop" claim about Franklin County that predates the tag-column stripping in `_slim_for_map` and has never been rechecked. Building a grid index and a coordinate encoder on top of an unverified premise is the same mistake that produced the tiling disaster (`osm_layers.py:307`, generalized from one measurement against a broken host).

**This task writes no production code.** Its deliverable is a committed measurement that decides the value of `GRID_CELL_DEG`, the gate zoom, and whether Task 4 gets built or deleted.

**Files:**
- Create: `docs/measurements/2026-08-12-map-payload-sizes.md`
- Create (throwaway, not committed): a scratch script under the session scratchpad

**Interfaces:**
- Consumes: nothing.
- Produces: three decided values used by Tasks 3, 4 and 6 — `GRID_CELL_DEG` (float, degrees), `GATE_ZOOM` (int), and `ENCODE` (bool: whether Task 4 is built).

- [ ] **Step 1: Fetch the three heavy layers for Delaware County once**

Delaware County OH is FIPS 39041. Use the app's own fetchers so the geometry matches what the map would really get:

```python
from backend.assess import osm_layers
from backend.gis.counties import county_boundary   # confirm the exact helper name first

study = county_boundary("39", "041")
bbox = tuple(study.to_crs("EPSG:4326").geometry.total_bounds)
layers = {name: osm_layers.fetch(name, bbox) for name in ("buildings", "roads", "sidewalks")}
```

If the helper name differs, find it with `grep -n "def .*county" backend/gis/counties.py` — do not guess.

Expected magnitudes from the 2026-08-12 live run, as a sanity check: buildings ~26,393, roads ~28,865, sidewalks ~7,135. If what arrives is wildly different, stop and report rather than measuring the wrong thing.

- [ ] **Step 2: Record raw sizes at both simplification tiers**

For each of the three layers, at `simplify(12.0)` (overview tier) and `simplify(1.5)` (detail tier), record:

- feature count
- `len(json.dumps(gdf[["geometry"]].to_crs("EPSG:4326").__geo_interface__))` in bytes
- the same after rounding coordinates to 6 decimal places

- [ ] **Step 3: Render one full map with the cap removed and time it**

Temporarily set `_MAP_MAX_FEATURES = 10**9` and `_MAP_SIMPLIFY_M = 1.5`, render Delaware through `render_assessment_map` with `prefer_canvas=True` on the `folium.Map(...)` call, and record: total HTML bytes, and — opening the file in a browser — cold load time to first paint and the frame time when panning at z16.

**Revert both constants afterwards.** This is a measurement, not a change.

- [ ] **Step 4: Write the decisions down**

Create `docs/measurements/2026-08-12-map-payload-sizes.md` containing the table of measured numbers and, explicitly:

- `ENCODE = true|false` — false if plain 6-dp GeoJSON for all three layers lands the total HTML under 25 MB **and** Step 3's pan frame time is under 100 ms. If false, **Task 4 is deleted from this plan, not implemented.**
- `GRID_CELL_DEG` — chosen so the densest cell in Delaware holds ≈500 features (a tenth of the 5,000 draw budget).
- `GATE_ZOOM` — the lowest zoom at which the densest viewport in Delaware holds ≤5,000 features.
- Whether Approach A (remove the cap + `prefer_canvas`, no grid, no gate) already satisfies the whole draw budget. **If it does, say so plainly and stop — Tasks 3 through 7 collapse into a 20-line change.**

- [ ] **Step 5: Commit**

```bash
git add docs/measurements/2026-08-12-map-payload-sizes.md
git commit -m "docs(hcas): measure real map payload sizes for Delaware County

Decides GRID_CELL_DEG, GATE_ZOOM, and whether the compact coordinate
encoding is needed. The 3,500 cap's size claim predates tag-column
stripping and had never been rechecked."
```

---

### Task 3: `map_payload.build_payload` — grid bucketing

**Files:**
- Create: `backend/assess/map_payload.py`
- Test: `backend/tests/test_map_payload_20260812.py` (create)

**Interfaces:**
- Consumes: `GRID_CELL_DEG` from Task 2.
- Produces:

```python
build_payload(gdf: gpd.GeoDataFrame, cell_deg: float = GRID_CELL_DEG) -> dict
# {"geoms": [ [[lon, lat], ...], ... ],   # one entry per feature, index-aligned
#  "cells": { "row_col": [feature_index, ...] },
#  "total": int,
#  "cell_deg": float}
```

Geometry is stored **once** per feature in `geoms`; a feature spanning several cells has its **index** repeated in each cell, not its coordinates. The client dedupes by index. This is what keeps a boundary-spanning building whole without paying for it twice.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_map_payload_20260812.py`:

```python
"""Grid bucketing for the viewport-rendered map.

The test that matters is test_every_feature_is_reachable: nothing in the suite
before 2026-08-12 asserted that the map contains what was measured, which is why
87.5% of Delaware's buildings could go missing silently for two weeks.
"""
from __future__ import annotations

import geopandas as gpd
from shapely.geometry import Polygon

from backend.assess.map_payload import build_payload


def _squares(n: int, size: float = 0.001) -> gpd.GeoDataFrame:
    """n small squares in a row, in EPSG:4326 degrees."""
    polys = []
    for i in range(n):
        x = -83.0 + i * size * 2
        polys.append(Polygon([(x, 40.0), (x + size, 40.0), (x + size, 40.0 + size), (x, 40.0 + size)]))
    return gpd.GeoDataFrame(geometry=polys, crs="EPSG:4326")


def test_every_feature_is_reachable():
    gdf = _squares(500)
    p = build_payload(gdf, cell_deg=0.01)
    reachable = set()
    for idxs in p["cells"].values():
        reachable.update(idxs)
    assert reachable == set(range(500))
    assert p["total"] == 500


def test_geometry_is_stored_once_per_feature():
    gdf = _squares(500)
    p = build_payload(gdf, cell_deg=0.01)
    assert len(p["geoms"]) == 500


def test_feature_spanning_a_boundary_is_indexed_in_every_cell_it_touches():
    # A square straddling the x=-83.0 cell boundary with cell_deg=0.01.
    straddler = Polygon([(-83.005, 40.0), (-82.995, 40.0), (-82.995, 40.005), (-83.005, 40.005)])
    gdf = gpd.GeoDataFrame(geometry=[straddler], crs="EPSG:4326")
    p = build_payload(gdf, cell_deg=0.01)
    holding = [k for k, idxs in p["cells"].items() if 0 in idxs]
    assert len(holding) >= 2, f"straddling feature filed into only {holding}"


def test_empty_input_is_an_empty_payload_not_a_crash():
    gdf = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    p = build_payload(gdf)
    assert p["total"] == 0
    assert p["geoms"] == []
    assert p["cells"] == {}


def test_reprojects_to_4326():
    gdf = _squares(3).to_crs("EPSG:5070")
    p = build_payload(gdf, cell_deg=0.01)
    lon, lat = p["geoms"][0][0]
    assert -84.0 < lon < -82.0
    assert 39.0 < lat < 41.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_map_payload_20260812.py -v`
Expected: FAIL — `ModuleNotFoundError: backend.assess.map_payload`.

- [ ] **Step 3: Write the implementation**

Create `backend/assess/map_payload.py`:

```python
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
# docs/measurements/2026-08-12-map-payload-sizes.md
GRID_CELL_DEG = 0.02


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
    """Bucket every feature into a lat/lon grid. Returns geoms + cell index."""
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
```

- [ ] **Step 4: Run the tests**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_map_payload_20260812.py -v`
Expected: 5 passed.

Run: `.venv/Scripts/python.exe -m pytest backend/tests -q`
Expected: **288 passed** (283 + 5).

- [ ] **Step 5: Commit**

```bash
git add backend/assess/map_payload.py backend/tests/test_map_payload_20260812.py
git commit -m "feat(map): grid-bucketed payload so the map can draw only what is in view

Geometry stored once per feature; a feature touching several cells repeats
its index, not its coordinates. Tested for the property nothing asserted
before: every input feature is reachable from the payload."
```

---

### Task 4: Compact coordinate encoding — **build only if Task 2 set `ENCODE = true`**

If Task 2's measurement showed plain 6-dp GeoJSON fits the 25 MB ceiling and the 100 ms pan budget, **delete this task and proceed to Task 5.** Deleting it is the preferred outcome; do not build it to be thorough.

**Files:**
- Modify: `backend/assess/map_payload.py`
- Test: `backend/tests/test_map_payload_20260812.py`

**Interfaces:**
- Consumes: `build_payload` from Task 3.
- Produces: `encode_ring(ring: list) -> list[int]` and its inverse contract, and `build_payload(..., encode: bool = False)` returning `geoms` as delta-encoded integer arrays plus `"encoded": True` in the payload dict.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_map_payload_20260812.py`:

```python
from backend.assess.map_payload import encode_ring, decode_ring


def test_encode_decode_round_trip_within_tolerance():
    ring = [[-83.001234, 40.005678], [-83.000111, 40.006789], [-82.999000, 40.007000]]
    out = decode_ring(encode_ring(ring))
    assert len(out) == len(ring)
    for (ax, ay), (bx, by) in zip(ring, out):
        assert abs(ax - bx) <= 1e-6
        assert abs(ay - by) <= 1e-6


def test_encoded_is_flat_integers():
    enc = encode_ring([[-83.001234, 40.005678], [-83.000111, 40.006789]])
    assert all(isinstance(v, int) for v in enc)


def test_encoded_payload_still_reaches_every_feature():
    gdf = _squares(500)
    p = build_payload(gdf, cell_deg=0.01, encode=True)
    assert p["encoded"] is True
    reachable = set()
    for idxs in p["cells"].values():
        reachable.update(idxs)
    assert reachable == set(range(len(p["geoms"])))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_map_payload_20260812.py -v`
Expected: FAIL — `ImportError: cannot import name 'encode_ring'`.

- [ ] **Step 3: Write the implementation**

Add to `backend/assess/map_payload.py`:

```python
_SCALE = 10 ** 6  # 6 dp ~ 0.11 m


def encode_ring(ring: list) -> list:
    """Flat delta-encoded integers: [x0, y0, dx1, dy1, dx2, dy2, ...].

    Quantising to 1e-6 degrees and storing deltas turns 20-character floats into
    small integers, which is most of the file size for dense layers.
    """
    out: list = []
    px = py = 0
    for x, y in ring:
        ix, iy = int(round(x * _SCALE)), int(round(y * _SCALE))
        out.append(ix - px)
        out.append(iy - py)
        px, py = ix, iy
    return out


def decode_ring(enc: list) -> list:
    """Inverse of encode_ring. Mirrored in frontend/static/viewport_layers.js —
    change both together."""
    out: list = []
    x = y = 0
    for i in range(0, len(enc), 2):
        x += enc[i]
        y += enc[i + 1]
        out.append([x / _SCALE, y / _SCALE])
    return out
```

Then add `encode: bool = False` to `build_payload`'s signature, and where it appends a ring:

```python
            pts = [[round(x, 6), round(y, 6)] for x, y in ring]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            geoms.append(encode_ring(pts) if encode else pts)
```

(the bounds are computed from `pts` before encoding), and add `"encoded": encode` to the returned dict and to `empty`.

- [ ] **Step 4: Run the tests**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_map_payload_20260812.py -v`
Expected: 8 passed.

Run: `.venv/Scripts/python.exe -m pytest backend/tests -q`
Expected: **291 passed**.

- [ ] **Step 5: Commit**

```bash
git add backend/assess/map_payload.py backend/tests/test_map_payload_20260812.py
git commit -m "feat(map): delta-encode payload coordinates

Quantised to 1e-6 deg and delta-coded within each ring. Decoder mirrored in
viewport_layers.js; the two must change together."
```

---

### Task 5: Split `_slim_for_map` into overview and detail tiers

`_MAP_SIMPLIFY_M = 12.0` applied to a 10–15 m house footprint produces a blob. Drawing all the buildings is pointless if each one is melted, so the detail tier gets its own tolerance.

**Files:**
- Modify: `backend/hcas.py:1580-1602`
- Test: `backend/tests/test_map_payload_20260812.py`

**Interfaces:**
- Consumes: `build_payload` (Task 3).
- Produces: `_overview_geoms(gdf) -> tuple[GeoDataFrame, dict]` (exactly today's `_slim_for_map`, renamed) and `_detail_payload(gdf) -> dict` (the `build_payload` result at detail simplification). `_slim_for_map` is retired; its callers move to `_overview_geoms`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_map_payload_20260812.py`:

```python
import geopandas as _gpd
from shapely.geometry import Polygon as _Poly

from backend import hcas


def _metre_squares(n: int, size: float = 12.0) -> _gpd.GeoDataFrame:
    polys = [_Poly([(x * size * 2, 0), (x * size * 2 + size, 0),
                    (x * size * 2 + size, size), (x * size * 2, size)]) for x in range(n)]
    return _gpd.GeoDataFrame(geometry=polys, crs="EPSG:5070")


def test_detail_payload_keeps_every_feature():
    gdf = _metre_squares(20000)
    p = hcas._detail_payload(gdf)
    assert p["total"] >= 20000, "the detail tier must not sample"


def test_detail_geometry_is_finer_than_overview():
    gdf = _metre_squares(50)
    overview, _info = hcas._overview_geoms(gdf)
    detail = hcas._detail_payload(gdf)
    overview_pts = sum(len(g.exterior.coords) for g in overview.geometry)
    detail_pts = sum(len(ring) for ring in detail["geoms"])
    assert detail_pts > overview_pts, "12 m simplification on a 12 m house is a blob"


def test_overview_still_samples_and_reports():
    gdf = _metre_squares(26393)
    _slim, info = hcas._overview_geoms(gdf)
    assert info["stride"] == 8
    assert info["total"] == 26393
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_map_payload_20260812.py -v`
Expected: FAIL — `AttributeError: module 'backend.hcas' has no attribute '_detail_payload'`.

- [ ] **Step 3: Write the implementation**

In `backend/hcas.py`, rename `_slim_for_map` to `_overview_geoms` (body unchanged from Task 1), add the detail constant beside `_MAP_SIMPLIFY_M`:

```python
# A typical house is 10-15 m across, so the 12 m overview tolerance turns each
# footprint into a blob. Showing every building is pointless if every building
# is melted, so the zoomed-in tier simplifies an order of magnitude less.
_DETAIL_SIMPLIFY_M = 1.5
```

and add:

```python
def _detail_payload(gdf):
    """Every feature, lightly simplified, bucketed for viewport rendering."""
    from backend.assess.map_payload import build_payload

    if gdf is None or gdf.empty:
        return build_payload(None)
    g = gdf[["geometry"]].copy()
    g = g[~g.geometry.is_empty & g.geometry.notna()]
    g["geometry"] = g.geometry.simplify(_DETAIL_SIMPLIFY_M)
    g = g[~g.geometry.is_empty]
    return build_payload(g)
```

Update the call site at `:1556` to use `_overview_geoms`.

- [ ] **Step 4: Run the tests**

Run: `.venv/Scripts/python.exe -m pytest backend/tests -q`
Expected: all green, count = previous + 3.

- [ ] **Step 5: Commit**

```bash
git add backend/hcas.py backend/tests/test_map_payload_20260812.py
git commit -m "feat(hcas): two-tier map geometry, overview and detail

12 m simplification on a 12 m house is a blob, so the zoomed-in tier uses
1.5 m and never samples."
```

---

### Task 6: `viewport_layers.js` — the client module

There is no node or JS test tooling in this repo and this plan does not add any. The decoder contract is pinned on the Python side (Task 4); the JS is verified end-to-end by the browser check in Task 8. Say so rather than pretending it is unit-tested.

**Files:**
- Create: `frontend/static/viewport_layers.js`

**Interfaces:**
- Consumes: the payload dict shape from Tasks 3/4.
- Produces: a global `HCASViewportLayer(map, payload, opts)` where `opts = {gateZoom, style, encoded}`, with methods `attach()` and `detach()`.

- [ ] **Step 1: Write the module**

Create `frontend/static/viewport_layers.js`:

```javascript
/* Viewport rendering for HCAS assessment maps.
 *
 * The map embeds every feature but draws only the grid cells the camera covers,
 * and only once zoomed past the gate. Below the gate a thinned overview is drawn
 * by Folium in the normal way and this module stays out of the way.
 *
 * decodeRing MIRRORS decode_ring in backend/assess/map_payload.py.
 * Change the two together or the map silently draws garbage.
 */
(function (global) {
  "use strict";

  var SCALE = 1e6;

  function decodeRing(enc) {
    var out = [], x = 0, y = 0;
    for (var i = 0; i < enc.length; i += 2) {
      x += enc[i];
      y += enc[i + 1];
      out.push([y / SCALE, x / SCALE]);   // Leaflet wants [lat, lng]
    }
    return out;
  }

  function plainRing(ring) {
    var out = [];
    for (var i = 0; i < ring.length; i++) out.push([ring[i][1], ring[i][0]]);
    return out;
  }

  function HCASViewportLayer(map, payload, opts) {
    this.map = map;
    this.payload = payload;
    this.gateZoom = opts.gateZoom;
    this.style = opts.style || {};
    this.encoded = !!opts.encoded;
    this.group = L.layerGroup();
    this.renderer = L.canvas({ padding: 0.2 });
    this.cache = {};
    this.drawn = null;
    this._onMove = this.refresh.bind(this);
  }

  HCASViewportLayer.prototype.attach = function () {
    this.group.addTo(this.map);
    this.map.on("moveend zoomend", this._onMove);
    this.refresh();
  };

  HCASViewportLayer.prototype.detach = function () {
    this.map.off("moveend zoomend", this._onMove);
    this.map.removeLayer(this.group);
  };

  HCASViewportLayer.prototype._latlngs = function (i) {
    if (!this.cache[i]) {
      var raw = this.payload.geoms[i];
      this.cache[i] = this.encoded ? decodeRing(raw) : plainRing(raw);
    }
    return this.cache[i];
  };

  HCASViewportLayer.prototype.refresh = function () {
    if (this.map.getZoom() < this.gateZoom) {
      if (this.drawn !== null) { this.group.clearLayers(); this.drawn = null; }
      return;
    }
    var b = this.map.getBounds(), d = this.payload.cell_deg;
    var c0 = Math.floor(b.getWest() / d), c1 = Math.floor(b.getEast() / d);
    var r0 = Math.floor(b.getSouth() / d), r1 = Math.floor(b.getNorth() / d);

    var wanted = {};                       // feature index -> true, deduped
    for (var r = r0; r <= r1; r++) {
      for (var c = c0; c <= c1; c++) {
        var idxs = this.payload.cells[r + "_" + c];
        if (!idxs) continue;
        for (var k = 0; k < idxs.length; k++) wanted[idxs[k]] = true;
      }
    }
    var key = Object.keys(wanted).sort().join(",");
    if (key === this.drawn) return;        // same features, nothing to redo
    this.group.clearLayers();
    var style = this.style, renderer = this.renderer, self = this;
    Object.keys(wanted).forEach(function (i) {
      var opts = Object.assign({ renderer: renderer }, style);
      self.group.addLayer(L.polygon(self._latlngs(i), opts));
    });
    this.drawn = key;
  };

  global.HCASViewportLayer = HCASViewportLayer;
})(window);
```

- [ ] **Step 2: Syntax-check it**

There is no JS runner here, so check it parses rather than claiming it is tested:

Run: `node --check frontend/static/viewport_layers.js`
Expected: no output, exit 0. If `node` is unavailable, open the file in the browser console via Task 8 and confirm no `SyntaxError`.

- [ ] **Step 3: Commit**

```bash
git add frontend/static/viewport_layers.js
git commit -m "feat(map): client module drawing only the grid cells in view

Above the gate zoom, look up the cells the camera covers and draw those
features onto a canvas renderer; below it, stay out of Folium's way.
decodeRing mirrors decode_ring in map_payload.py."
```

---

### Task 7: Wire the payload into the renderer and make the legend truthful

Only now does "zoom in for all" become a true statement, so only now does it go on the map.

**Files:**
- Modify: `backend/assess/renderer.py`
- Modify: `backend/hcas.py:1552-1572`
- Test: `backend/tests/test_map_disclosure_20260812.py`

**Interfaces:**
- Consumes: `_overview_geoms`, `_detail_payload`, `HCASViewportLayer`.
- Produces: `render_assessment_map(..., sample_info=None, detail_payloads=None, gate_zoom=GATE_ZOOM)`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_map_disclosure_20260812.py`:

```python
def test_legend_promises_zoom_only_when_detail_is_embedded():
    from backend.assess.renderer import _sample_legend_line

    info = {"shown": 3300, "total": 26393, "stride": 8}
    assert "zoom in" not in _sample_legend_line("buildings", info).lower()
    assert "zoom in" in _sample_legend_line("buildings", info, has_detail=True).lower()


def test_rendered_map_embeds_the_viewport_module_and_no_external_urls():
    import geopandas as gpd
    from shapely.geometry import Polygon
    from backend.assess.renderer import render_assessment_map
    from backend.assess.map_payload import build_payload

    sa = gpd.GeoDataFrame(
        geometry=[Polygon([(-83.1, 40.0), (-82.9, 40.0), (-82.9, 40.2), (-83.1, 40.2)])],
        crs="EPSG:4326",
    ).to_crs("EPSG:5070")
    b = gpd.GeoDataFrame(
        geometry=[Polygon([(-83.0, 40.1), (-82.99, 40.1), (-82.99, 40.11), (-83.0, 40.11)])],
        crs="EPSG:4326",
    ).to_crs("EPSG:5070")
    empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")

    html = render_assessment_map(
        sa, b, empty, empty, {"building_coverage_ratio": 0.1},
        detail_payloads={"buildings": build_payload(b)},
    )
    assert "HCASViewportLayer" in html
    # The page must not reach the network for its own feature data.
    assert "fetch(" not in html
    assert "XMLHttpRequest" not in html
    # The payload itself is inline, not referenced.
    assert "cell_deg" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest backend/tests/test_map_disclosure_20260812.py -v`
Expected: FAIL — `_sample_legend_line() got an unexpected keyword argument 'has_detail'`.

- [ ] **Step 3: Implement**

In `backend/assess/renderer.py`, extend the legend helper:

```python
def _sample_legend_line(layer: str, info: dict | None, has_detail: bool = False) -> str:
    if not info or info.get("stride", 1) <= 1:
        return ""
    shown, total, stride = info["shown"], info["total"], info["stride"]
    tail = (" Zoom in and every one in view is drawn."
            if has_detail else
            f" All {total:,} were measured and counted in the metrics above.")
    return (f"<b>{layer}</b>: showing {shown:,} of {total:,} (1 in {stride}) at this "
            f"zoom.{tail}")
```

Add parameters `detail_payloads: dict | None = None` and `gate_zoom: int = 15` to `render_assessment_map`, pass `prefer_canvas=True` to the `folium.Map(...)` call at `:103`, and before the return inline the module and register a layer per heavy layer:

```python
    if detail_payloads:
        import json
        from pathlib import Path

        js = (Path(__file__).resolve().parents[2]
              / "frontend" / "static" / "viewport_layers.js").read_text(encoding="utf-8")
        blocks = [f"<script>{js}</script>"]
        _styles = {
            "buildings": {"color": "#333333", "weight": 0.5,
                          "fillColor": "#555555", "fillOpacity": 0.6},
            "roads": {"color": "#3a7abf", "weight": 1, "fill": False},
            "sidewalks": {"color": "#7b4fa6", "weight": 1.5, "fill": False},
        }
        for name, payload in detail_payloads.items():
            if not payload or not payload.get("total"):
                continue
            blocks.append(
                "<script>(function(){var p=%s;new HCASViewportLayer(%s,p,"
                "{gateZoom:%d,encoded:%s,style:%s}).attach();})();</script>"
                % (json.dumps(payload), m.get_name(), gate_zoom,
                   "true" if payload.get("encoded") else "false",
                   json.dumps(_styles.get(name, {})))
            )
        m.get_root().html.add_child(folium.Element("".join(blocks)))
```

Pass `has_detail=bool(detail_payloads)` in the legend loop from Task 1.

In `backend/hcas.py`, at the render block (`:1552-1572`), build and pass the detail payloads:

```python
        detail = {}
        for _n in ("buildings", "roads", "sidewalks"):
            if _n in layers:
                try:
                    detail[_n] = _detail_payload(layers[_n])
                except Exception as _exc:      # a drawing aid never fails the step
                    logger.warning(f"detail payload for {_n} failed: {_exc}")
```

and add `detail_payloads=detail,` to the `render_assessment_map(...)` call.

- [ ] **Step 4: Add the size-ceiling guard**

Immediately after `html = render_assessment_map(...)` in `hcas.py`, before the existing `150_000_000` check, add:

```python
        if len(html) > 25_000_000 and detail:
            logger.warning(
                f"map HTML {len(html)/1e6:.0f} MB over the 25 MB ceiling — "
                "re-rendering without embedded detail layers")
            html = render_assessment_map(
                study_area,
                slim.get("buildings", empty), slim.get("greenspace", empty),
                slim.get("roads", empty), metrics,
                parking=slim.get("parking"), sidewalks=slim.get("sidewalks"),
                transit_stops=slim.get("transit_stops"),
                facilities=slim.get("facilities"), sample_info=sample_info,
            )
```

- [ ] **Step 5: Run the tests**

Run: `.venv/Scripts/python.exe -m pytest backend/tests -q`
Expected: all green, count = previous + 2.

- [ ] **Step 6: Commit**

```bash
git add backend/assess/renderer.py backend/hcas.py backend/tests/test_map_disclosure_20260812.py
git commit -m "feat(hcas): embed full layers and draw only what the camera covers

Above the gate zoom every building in view is drawn; below it the labelled
overview stands. Falls back to the overview-only map if the file would
exceed 25 MB."
```

---

### Task 8: Verify in a real browser on a real county

Nothing above proves the map works. A green suite is not evidence that the artifact runs — verify the shipping thing the way the recipient will.

**Files:**
- Create: `docs/measurements/2026-08-12-map-viewport-verification.md`

**Interfaces:**
- Consumes: everything.
- Produces: a recorded verification, or a defect list.

- [ ] **Step 1: Render a real Delaware map**

Run step 7 for Delaware County OH (FIPS 39041) through the app, then write the stored HTML out:

```python
from backend import hcas
open("delaware_map.html", "w", encoding="utf-8").write(hcas.get_map("<pid>"))
```

- [ ] **Step 2: Open the saved file directly from disk, with no server running**

This is the whole point of the standalone constraint. Open `file:///.../delaware_map.html` with the HCAS app stopped.

Confirm, using the webapp-testing skill (Playwright):

- At county zoom: the overview draws, and the legend reads "showing 3,300 of 26,393 (1 in 8) at this zoom. Zoom in and every one in view is drawn."
- At z16 over a dense subdivision: buildings fill the block with no visible gaps, and footprints are recognisably rectangular rather than blobs.
- Panning at z16 stays under 100 ms per update.
- The browser console shows **zero** network requests for feature data and no errors.

- [ ] **Step 3: Count what is drawn against what was measured**

In the browser console, count the rendered paths in view at z16 and compare against the building count for that bbox computed from the payload in Python. They must match. A mismatch here is the bug this whole plan exists to prevent.

- [ ] **Step 4: Record the result**

Write `docs/measurements/2026-08-12-map-viewport-verification.md` with the measured numbers, screenshots, and anything that failed. If something failed, say so plainly and stop — do not report the plan complete.

- [ ] **Step 5: Commit**

```bash
git add docs/measurements/2026-08-12-map-viewport-verification.md
git commit -m "docs(hcas): browser verification of viewport map rendering"
```

---

## Self-Review

**Spec coverage.** Goals 1–4 → Tasks 7, 6/7 (standalone, asserted by `test_rendered_map_embeds_the_viewport_module_and_no_external_urls` and Step 2 of Task 8), 6, 1/7. Constraints table → Tasks 2 (portability measurement), 1/7 (labelled overview), 5/7 (layer scope). Payload format → Tasks 3/4. Client behaviour → Task 6. Tiered simplification → Task 5. Legend copy → Tasks 1 and 7. Gate zoom/cell size → Task 2. Error handling, all three rows → Task 7 Steps 3/4 and Task 6's gate branch. Testing section, all six bullets → Tasks 1, 3, 4, 5, 7, 8. Step 0 → Task 2. Rejected alternatives → Task 2 Step 4 explicitly re-tests Approach A and can collapse the plan.

**Placeholder scan.** One deliberate conditional remains: Task 4 is built only if Task 2 sets `ENCODE = true`, and Task 2 Step 4 defines that decision with a numeric threshold rather than leaving it to judgement. `GRID_CELL_DEG = 0.02` ships as a named default in code and is corrected by Task 2's measurement. The `county_boundary` helper in Task 2 Step 1 is flagged as needing confirmation with an exact grep rather than guessed.

**Type consistency.** `_slim_for_map` returns a tuple from Task 1 and is renamed `_overview_geoms` in Task 5, with the call site updated in both tasks. `build_payload` keys (`geoms`, `cells`, `total`, `cell_deg`, plus `encoded` from Task 4) are used identically in Tasks 5, 6 and 7. `_sample_legend_line` gains `has_detail` in Task 7 with a default, so Task 1's two-argument calls keep working. `decode_ring` (Python) and `decodeRing` (JS) are cross-referenced in both files, with the lat/lng axis swap living only on the JS side.
