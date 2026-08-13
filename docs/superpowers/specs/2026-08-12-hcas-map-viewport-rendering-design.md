# HCAS Step-7 Map: Viewport Rendering

**Date:** 2026-08-12
**Status:** design approved, not implemented
**Repo:** `geoai-assess`, branch `hcas` (@ `4e075b2` at time of writing)

## Problem

The step-7 assessment map draws a fraction of the features it measured, and says
nothing about it.

`backend/hcas.py:1589`:

```python
_MAP_MAX_FEATURES = 3500
_MAP_SIMPLIFY_M = 12.0

def _slim_for_map(gdf):
    ...
    if n > _MAP_MAX_FEATURES:
        # even stride keeps the sample spread across the area, not clustered
        g = g.iloc[:: (n // _MAP_MAX_FEATURES) + 1]
    g["geometry"] = g.geometry.simplify(_MAP_SIMPLIFY_M)
```

Every *N*th feature survives, `N = (count // 3500) + 1`:

| county / layer | features | stride | drawn | invisible |
|---|---|---|---|---|
| Adams buildings | 6,953 | 2 | 3,477 | 50.0% |
| **Delaware buildings** | **26,393** | **8** | **3,300** | **87.5%** |
| Delaware roads | 28,865 | 9 | 3,208 | 88.9% |
| Delaware sidewalks | 7,135 | 3 | 2,379 | 66.7% |
| Franklin (est. 60k) | 60,000 | 18 | 3,334 | 94.4% |

This is not a regression. `_MAP_MAX_FEATURES = 3500` is byte-identical in
`55531d7` (2026-07-26), in all four shipped zips (`HCAS-setup.zip` 08-04,
`-fixed` 08-05, `-20260810`, `-20260811`), in all four unpacked builds
(`hcasship` … `hcasship4`), and at `HEAD`. What changed is county density: early
test areas were under 3,500 features and drew complete; rural Adams dropped every
*other* building invisibly; suburban Delaware draws 1 in 8 and reads as a broken
fetch.

**The metrics are unaffected.** `hcas.py:1543–1556` computes `metrics` and
`by_unit` from the full `layers` dict; `slim` is built afterward and passed only
to `render_assessment_map`.

**The defect is disclosure.** `renderer.py:262–272` has legend lines for
*"not measured"* and *"not selected"* and none for *"sampled"*, while the panel
at `:274–291` prints "Building Coverage: X%" beside a map showing 12.5% of the
buildings. The number and the picture contradict each other with no explanation.

## Goals

1. Every building, road and sidewalk is visible when the analyst zooms in.
2. The map stays a **single self-contained HTML file** that works with no server.
3. Draw cost is bounded by what is on screen, not by county size.
4. The map never silently misrepresents what it is drawing, at any zoom.

## Non-goals

- Server-side tile or feature endpoints. Ruled out: the map must work standalone.
- Changing any metric, computation, or fetch behaviour. Step 3 is untouched.
- Whether the existing stride sample is *spatially* clustered. It stops mattering
  for the zoomed-in view; it still governs the county-zoom overview and is
  tracked separately.

## Constraints (decided)

| Decision | Value | Why |
|---|---|---|
| Portability | Must work standalone | User decision — map may be saved or sent to Prof. Shi and opened without the app |
| Zoomed-out behaviour | Thinned overview + explicit label | Nothing looks broken at any zoom; fixes the disclosure bug in the same change |
| Layer scope | buildings, roads, sidewalks | The only three layers over the cap. greenspace (2,126), landuse (3,195), parking (1,050), facilities (203), transit (112) already draw in full and keep their existing path |

## Architecture

### Components

**`backend/assess/map_payload.py`** (new) — owns bucketing and encoding. Pure
data in, pure data out: no folium, no browser, no I/O. Fully unit-testable.

**`backend/hcas.py`** — `_slim_for_map` splits in two:

- `_overview_geoms(gdf)` — today's stride logic at `_MAP_SIMPLIFY_M` (12 m).
  Drawn below the gate. Returns `{shown, total, stride}` for the legend.
- `_detail_payload(gdf)` — *all* features at `_DETAIL_SIMPLIFY_M` (~1.5 m),
  delegated to `map_payload`.

**`frontend/static/viewport_layers.js`** (new) — a real file in the repo, inlined
into the map HTML at render time so the page stays self-contained. Registers one
Leaflet layer per heavy layer and owns the gate/cull logic.

**`backend/assess/renderer.py`** — wires both payloads in and gains the legend
text. The five small layers keep their existing `folium.GeoJson` path unchanged.

### Data flow

```
step 7 layers (full GeoDataFrames)
  ├─ metrics / by_unit ................ unchanged, full data
  └─ render_assessment_map
       ├─ greenspace, landuse, parking,
       │  facilities, transit .......... folium.GeoJson (unchanged)
       └─ buildings, roads, sidewalks ... _overview_geoms  -> below gate
                                          _detail_payload  -> above gate
                                          + inlined viewport_layers.js
```

### Payload format

- Grid cells keyed `"{row}_{col}"`, cell size in degrees set by measurement so a
  typical cell holds a few hundred features.
- A feature is filed into **every cell its bbox touches**, so it never renders
  half-drawn at a boundary. The client dedupes on feature index.
- Coordinates quantized to 6 decimal places (~0.11 m) and delta-encoded within
  each ring, as flat integer arrays rather than per-feature JSON objects.

**The delta encoding is conditional.** If the step-0 measurement shows plain
GeoJSON lands within the size budget, the encoding is dropped rather than built.
Deleting it is the preferred outcome.

### Client behaviour

`moveend` / `zoomend`:

- `zoom < GATE` — draw the overview set.
- `zoom >= GATE` — compute intersecting cells, decode and draw their features
  onto an `L.canvas()` renderer, drop features that leave view. A decoded-feature
  cache keyed by feature index means panning back does not re-decode.

### Simplification is tiered

`_MAP_SIMPLIFY_M = 12.0` applied to a 10–15 m house footprint produces a blob.
The overview keeps 12 m; detail geometry uses ~1.5 m. Without this, zooming in
yields all the buildings and they all look melted.

## Legend copy

Below gate:
> buildings: showing 3,300 of 26,393 (1 in 8) — zoom in for all. All 26,393 were measured.

Above gate:
> buildings: all 1,204 in view.

This ships **even if everything else is descoped.** It is the part that caused a
correct run to be read as a broken fetch.

## Gate zoom and cell size

Derived from the step-0 measurement, not picked. The draw budget, stated
explicitly so the measurement has a pass/fail: **≤ 5,000 features drawn per
viewport update, and pan/zoom staying under 100 ms per update** on the reference
machine. Gate zoom is the lowest zoom at which the densest viewport in the county
still satisfies both; cell size is then chosen so a single cell holds roughly a
tenth of that budget. Candidate z15, to be confirmed against real Delaware data.

## Error handling

Every failure degrades to something truthful, never to a blank or a lie.

| Failure | Behaviour |
|---|---|
| Payload build throws | Fall back to today's decimated map + logged warning. Preserves the existing "map is a bonus, never fails the step" contract (`hcas.py:1574`) |
| Total rendered HTML over hard ceiling (25 MB — the file the browser must open, not just the encoded payload) | Coarsen detail simplification and retry, state the coarsening in the legend, rather than ship a file that will not open |
| JS decode error | Overview stays drawn |

## Testing

**The test that matters:** the payload contains every input feature —
`sum(features across all cells, deduped) == len(gdf)`. Nothing in the suite today
asserts the map shows what was measured, which is exactly why 87.5% could go
missing silently for two weeks.

Also:

- Grid assignment — a feature spanning four cells appears in all four, and
  dedupes to one on read.
- Quantize and delta round-trip within stated tolerance.
- Size-budget test on a synthetic dense county.
- Tiered simplification — detail geometry retains more vertices than overview.
- Browser check (webapp-testing skill): load a real Delaware map, assert the
  building count in view at z16 matches expectation and that z10 shows the
  overview with its label.

## Step 0 — measurement, before implementation

The 3,500 cap was justified by a "~45 MB HTML / could freeze a low-spec laptop"
claim about Franklin County that predates the tag-column stripping and has not
been rechecked. Building a second layer of machinery on top of an unverified
premise is the same mistake that produced the tiling disaster
(`osm_layers.py:307`, generalized from one measurement against a broken host).

Fetch Delaware buildings/roads/sidewalks once and record:

- raw geometry-only GeoJSON bytes, per layer
- encoded bytes, per layer
- total HTML size
- cold load time and pan/zoom frame time on a representative machine

Known reference point: the stored Adams map is **1.00 MB** of HTML at ~3,477
buildings + 3,363 roads (`backend/hcas.db`, project `0d4b078addc9`).

This measurement decides three things: the gate zoom, the cell size, and whether
the compact encoding is needed at all — i.e. whether simply removing the cap with
`prefer_canvas=True` was sufficient the whole time.

## Rejected alternatives

**Remove the cap, set `prefer_canvas=True`, ship it.** ~20 lines. Leaflet still
constructs a `Path` per feature regardless of visibility, so 65k features cost
memory and layout at load, and there is no zoom gate — the county view attempts
everything at once. Retained as the step-0 baseline to measure, not as the plan.

**Server-side bbox/feature endpoint.** Cleanest result and the app does serve the
map live (`/hcas/projects/{pid}/map`, `app.py:626`), but it requires persisting
layers (they are re-fetched at `hcas.py:1498` via the osmnx disk cache, never
stored) and breaks the standalone requirement. Ruled out by the portability
decision.

**Embedded raster tiles as data URIs.** Constant draw cost, but loses click and
popup, cannot be restyled, and a county of street-zoom tiles far exceeds the
vector payload.

## Related

- Vault: `Analysis/HCAS Map Decimation 2026-08-12.md`
- Vault: `Analysis/HCAS Step 3 Layer Starvation 2026-08-12.md`
- Vault: `Analysis/HCAS Step 3 Overpass Stall 2026-08-11.md`
