# 2026-08-12: Browser verification of viewport map rendering

**Current status (re-run after commit `8888d0b`): PASS, all nine checks.**
The map loads, is interactive, and every claim it makes about itself is now
true when opened the way the recipient will actually open it — a real
headless Chromium browser, `file:///...`, no server running. The first run
of this task (below, kept for the record) found the map completely broken;
that defect is fixed and re-verified here, not assumed fixed.

**Superseded byte size, 2026-08-13:** the 12,715,013-byte figure below (both
runs measured the same number) was rendered from the DETAIL tier built off
the raw bbox-fetched layers, unclipped to the study area — a defect a
whole-branch review caught after this doc was written (the overview tier was
always clipped; the detail tier never was). See the section immediately
below for the re-measurement after that fix. Everything else in this
doc — the browser-interactivity findings (load, console, shape, holes,
count-match, pan timing) — is unaffected by the clip fix and still holds.

## Clip-before-detail fix re-measurement (2026-08-13, final whole-branch review fix wave)

`backend/hcas.py`'s `_run_step_7` now clips each layer to the study area
(`backend.assess.renderer._clip_to_study_area`, the same function the
overview tier already used) BEFORE building its detail payload, not after.
Re-rendered the identical cached Delaware County pickles
(`buildings.pkl` 26,393 / `roads.pkl` 28,865 / `sidewalks.pkl` 7,135 /
`study_area.pkl`) through the fixed production call path.

| Layer | Raw (bbox-fetched) | Clipped to county | Dropped |
|---|---:|---:|---:|
| buildings | 26,393 | 22,953 | 3,440 |
| roads | 28,865 | 22,759 | 6,106 |
| sidewalks | 7,135 | 5,388 | 1,747 |
| **total** | **62,393** | **51,100** | **11,293** |

Those 11,293 dropped features are exactly the ones the whole-branch review
found drawing above zoom 16 outside the county boundary, uncounted by the
metrics — the same 2026-08-09 boundary defect reopened through the
unclipped detail tier.

**New rendered size: 11,077,515 bytes (11.08 MB)**, down from
12,715,013 bytes (12.72 MB) before the fix — a 1.64 MB (12.9%) reduction,
consistent with the ~18% average feature drop above (byte reduction is
smaller than the feature-count drop because clipping removes vertices from
partially-clipped boundary-crossing features as well as whole features, but
county-interior features — the majority — are untouched).

`build_payload` was also, for the first time, exercised directly against a
`GeometryCollection` and a `MultiLineString` — the geometry types
`.intersection()` (used by `_clip_to_study_area`) can return that neither
`fetch_buildings` (polygon-only) nor `fetch_sidewalks`/`fetch_road_network`
(LineString-only) had ever produced. `_parts`' `Multi*`/`GeometryCollection`
recursion was implemented but untested before this fix; two tests now cover
it directly in `backend/tests/test_map_payload_20260812.py`
(`test_geometry_collection_parts_are_all_reachable`,
`test_multilinestring_parts_are_all_reachable`), asserting every member/part
is reachable from the cell index and keeps its own point count.

## Re-verification run (commit `8888d0b`)

### Setup

Identical conditions to the first run: real cached Delaware County pickles
(`buildings.pkl` 26,393 / `roads.pkl` 28,865 / `sidewalks.pkl` 7,135 /
`study_area.pkl`), rendered through the actual production call path
(`backend.hcas._overview_geoms` → `backend.hcas._detail_payload` →
`backend.assess.render_assessment_map`, same functions and argument shapes
`_run_step_7` uses). Same output size class: **12,715,013 bytes (12.7150
MB)**. Headless Playwright/Chromium, viewport 1200×800, no visible window
opened. `sample_info` unchanged: buildings 3,300/26,393 (1 in 8), roads
3,208/28,865 (1 in 9), sidewalks 2,379/7,135 (1 in 3).

### What changed since the first run

Commit `8888d0b` wraps the injected viewport-layer init in
`window.addEventListener("load", function () { ... })`, so it runs only
after every inline script on the page — including wherever folium placed
its own `L.map(...)` call — has executed. This is placement-independent:
it doesn't matter anymore whether the injected script lands before or
after folium's own map-creation script in the document.

### Results — all nine checks

| # | Check | Result |
|---|-------|--------|
| 1 | Loads at all | **PASS** — `load` event at 0.787s; the Leaflet map object was fully initialized (`_loaded === true`) at 0.794s. No hang. |
| 2 | Zero console errors | **PASS** — 0 console errors, 0 uncaught page errors. |
| 3 | Zero network requests for feature data | **PASS** — 59 total requests: 10 CDN library assets (Leaflet/jQuery/Bootstrap/FontAwesome/awesome-markers) + 49 CartoDB basemap tile requests (`*.basemaps.cartocdn.com`, expected and fine per the brief). Zero requests reference buildings/roads/sidewalks/payload data — everything feature-related is inline, none of it fetched. |
| 4 | County zoom shows labelled overview | **PASS** — initial zoom 11 (fit to county bounds). DOM text contains exactly: `buildings: showing 3,300 of 26,393 (1 in 8) at this zoom. Zoom in and every one in view is drawn.` (and the equivalent roads/sidewalks lines). Screenshot: `pass-county-overview.png`. |
| 5 | z16 over dense subdivision draws detail, no visible gaps | **PASS** — set view to the payload's densest building grid cell (Delaware, OH, near W Central Ave) at z16. Buildings fill blocks edge-to-edge with recognizable rectangular footprints, roads run through as thin lines, no gaps. Screenshot: `pass-z16-dense-subdivision.png`. |
| 6 | Roads are NOT closed rings | **PASS**, checked two ways. (a) Programmatically: every layer object Leaflet actually built for the roads (777 drawn) and sidewalks (158 drawn) `HCASViewportLayer` instances at z16 is an `L.Polyline` and **not** an `L.Polygon` (`instanceof` check on every drawn layer, not a code read) — `shape:"polyline"` really does produce open polylines in real Leaflet. (b) Visually: `pass-z16-dense-subdivision.png` shows roads as plain lines, no spurious fill or closing segments. |
| 7 | Buildings with holes render as voids | **PASS**, after fixing a bug in my own test script (see below), confirmed both by pixel sampling and direct visual crop. Target: building index 6 (2 interior rings — this building has two courtyards, more than the "1 hole" I originally assumed). Zoomed to z19 on it. Pixel sample: exterior-fill point = RGB(146,146,146) (building fill blended over basemap); corrected hole-centroid point = RGB(250,250,248) (essentially the basemap's own light color, i.e. genuinely a hole). Color delta 179.0. Screenshot crop `pass-building-hole-void.png` shows the building with two clean rectangular white notches cut out of the gray fill — real voids, not solid patches. |
| 8 | Rendered count at z16 matches Python-computed count for the same bbox | **PASS — exact match.** Took the actual `map.getBounds()` Leaflet computed after the z16 setView (south 40.296696, west -83.085201, north 40.308314, east -83.059795), replicated the client's own grid-cell-selection algorithm in Python against the identical `detail_payloads.json` that was embedded in the HTML (same `floor(coord/cell_deg)` cell math, same dedup-by-index), and got **3,556**. The browser's `HCASViewportLayer` instance for buildings reports `group.getLayers().length === 3556` at that same viewport. This is the plan's central promise, and it holds exactly, not approximately. |
| 9 | Pan/zoom responsiveness (<100ms/update, ≤5,000 draw budget) | **PASS**, measured honestly with one methodology caveat noted below. Five simulated pans at z16 (`panTo` + `fire('moveend')`, matching what a real drag-pan dispatches) measured **18.0–27.5 ms** round-trip each (mean ~23.5ms), all well under the 100ms budget — and this number includes Playwright's own CDP IPC overhead, so the true in-page cost is likely lower still. Max features drawn on any single layer through the whole test (county overview, z16 dense cell, and after panning): 3,556 buildings, comfortably under the 5,000 draw budget. |

### Methodology note on check 9

My first attempt instrumented `HCASViewportLayer.prototype.refresh` directly
to get a more precise in-page timing, but the wrapper was installed *after*
`attach()` had already done `this._onMove = this.refresh.bind(this)` —
the bound reference captured the original unwrapped function, so my
wrapper never actually fired and `hcas_refresh_ms_per_call` came back
empty. That's a bug in my test harness, not the product. I did not treat
the empty array as a finding; I relied instead on the Python-side
round-trip timing around `fire('moveend')`, which is a legitimate measure
since Leaflet's event dispatch (and therefore `refresh()`) runs
synchronously within `fire()` — by the time `frame.evaluate(...fire...)`
returns, the redraw has already happened.

### Methodology note on check 7 (the bug I found in my own script)

My first pass at this re-verification also reported `hole_appears_as_void:
false` (delta 0.0, identical pixel colors) — which would have been a real
defect. Before reporting that, I checked it: the target building actually
has **two** holes, not one (`geoms[6]` has 3 rings: 1 exterior + 2
interior), and my centroid calculation for the hole ring used the raw
shoelace formula on absolute lon/lat coordinates (~-83°, ~40°). For a tiny
ring (a few meters across) far from the coordinate origin, the shoelace
cross-product terms subtract two nearly-equal ~3,300-magnitude numbers —
catastrophic cancellation in float64 — and the computed "hole centroid"
landed about 12–15 meters outside the actual hole. Fixed by recentering
each ring on its own bounding-box minimum before the shoelace sum and
translating back after; re-verified the fix reproduces a sane centroid
(within ~1e-9 of an independent manual average-of-vertices check on this
near-rectangular hole) before re-running the pixel test. Recorded here
because a false failure reported as real would have been exactly the kind
of "green suite that isn't evidence" this task exists to prevent, just
inverted.

### Verification scripts and artifacts (scratch, not committed)

`render_real_map.py`, `analyze_payload.py` (now with the corrected
centroid), `verify_map.py` (main 9-point harness), `hole_check.py`
(focused re-check for #7). All under the session scratchpad. Screenshots
copied into this repo under
`docs/measurements/screenshots-2026-08-12-viewport-verification/`:
`pass-county-overview.png`, `pass-z16-dense-subdivision.png`,
`pass-building-hole-void.png`, `pass-after-pan.png`.

No production code was modified during this re-verification.

---

## First run (commit `0cfc704`) — FAILED

Kept in full for the record: the first verification attempt found the map
completely broken, and that defect (not this task) is what commit
`8888d0b` fixed.

Rendered the same real Delaware County pickles through the same production
call path. Got the expected artifact: 12,714,953 bytes (12.7150 MB),
correct `sample_info`. Opened it `file:///...` with headless Playwright,
no server running.

**The map never loaded.** The outer HTML document parsed in under 4
seconds, but the Leaflet map object inside folium's iframe never
initialized — its variable stayed hoisted-but-`undefined` for the full
duration of three independent polling runs (up to 260 seconds each). The
page rendered as a blank white screen with only the static metrics panel
floating on it. One uncaught error fired: `TypeError: Cannot read
properties of undefined (reading 'addLayer')`, thrown from
`HCASViewportLayer.attach()` at the very first instantiation.

**Root cause diagnosed:** `render_assessment_map`'s
`m.get_root().script.add_child(folium.Element(...))` call for the
detail-layer JS ran eagerly at Python build time, but folium defers the
actual `var map_xxx = L.map(...)` initialization line until
`_repr_html_()` traverses the Map macro element's own `.render()`. Since
both land in the same ordered `script` children dict and insertion order
is preserved, the eagerly-added block rendered **before** the map variable
was assigned. Verified directly against the unescaped `srcdoc`: the
`HCASViewportLayer` instantiation appeared at byte offset 5,047,723; the
`L.map(...)` assignment for the same variable appeared at byte offset
9,623,721 — 4.6 MB later. `this.map` was therefore `undefined` when
`.attach()` ran `this.group.addTo(this.map)`, and Leaflet's `addTo` threw
trying to call `.addLayer` on it. Because all three layers (buildings,
roads, sidewalks) were joined into one script element, the first exception
(buildings) aborted the rest of that script tag — roads and sidewalks
never even attempted to instantiate.

Isolated with a control render: calling the same `render_assessment_map`
with `detail_payloads=None` (the exact fallback branch `_run_step_7`
itself uses over 25 MB) loaded and became fully interactive in **0.6
seconds**, zero errors — proving the defect was isolated to the
`detail_payloads` script injection, not the renderer generally.

### First-run checklist (superseded)

1. Loads at all — FAIL (hung indefinitely)
2. Zero console errors — FAIL (1 uncaught TypeError)
3. Zero network requests for feature data — PASS (this part always worked)
4. County zoom legend text — text present in DOM but moot, no map underneath
5–9. **BLOCKED** — never evaluated, the map was never interactive enough to test them

### First-run screenshots (kept)

- `docs/measurements/screenshots-2026-08-12-viewport-verification/broken-blank-render.png`
  — the failing 12.71 MB production render, 8s after load: blank page,
  only the metrics panel visible.
- `docs/measurements/screenshots-2026-08-12-viewport-verification/control-overview-only-render.png`
  — the same data with `detail_payloads=None`, loading fine in 0.6s — the
  control that isolated the defect to the detail-layer injection.

No production code was modified during the first run either; the defect
was reported back to the coordinator, who dispatched commit `8888d0b`.
