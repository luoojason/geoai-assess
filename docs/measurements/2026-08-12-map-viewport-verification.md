# 2026-08-12: Browser verification of viewport map rendering

**Current status (re-run against commits `b688bc6` + `25de62e`, the
whole-branch-review fix wave): PASS, all nine checks, plus the four
new/changed behaviors that wave introduced.** This is the third verification
pass. History, newest first:

1. **Round 3 (`b688bc6` + `25de62e`, this run):** PASS 9/9, layer control,
   no-double-draw, and boundary-containment all confirmed in a real browser.
2. **Round 2 (`8888d0b`, superseded):** reported PASS 9/9, but against a
   renderer whose detail tier was NOT clipped to the study area — a defect a
   later whole-branch review caught that round 2 had no way to see (its own
   Python-side count check replayed the same unclipped payload the page
   embedded, so it was self-consistent by construction and could not have
   caught this). Kept below for the record, not deleted.
3. **Round 1 (`0cfc704`, FAILED):** the map never loaded at all. Kept below.

## Round 3 verification (commits `b688bc6`, `25de62e`)

### What changed since round 2

A whole-branch review found five Important issues after round 2 passed.
Summarized from the coordinator's brief, confirmed against the actual diff:

1. **Detail tier now clipped to the study area.** It was being built from
   the raw bbox-fetched layers while the overview was clipped — so above
   the gate the map used to draw 11,293 features outside the county that
   the metrics never counted. `backend/hcas.py`'s `_run_step_7` now calls
   the same `_clip_to_study_area` the overview tier already used, on each
   layer, before building its detail payload.
2. **Layer attachment changed.** Each `HCASViewportLayer`'s canvas group
   now nests inside its overview `FeatureGroup` (`container.addLayer`)
   rather than attaching to the map directly, so `LayerControl` governs
   both tiers as one on/off unit.
3. **The overview's own GeoJson sublayer is now hidden above the gate**
   once the detail canvas draws successfully, so only one tier is visible
   at a time (round 2's renderer drew both, doubled, in identical styling
   — a "no double draw above the gate" claim round 2 never actually tested).
4. **Per-layer `try/catch`** around each instantiation and around
   `refresh()`, so one layer's exception can no longer kill the other two
   (this is what fixed the exact failure class round 1 found, made
   robust against a DIFFERENT trigger).
5. **Regression fix on top of that:** hiding the overview before drawing
   could leave a layer showing neither tier if the draw threw mid-way. The
   overview is now hidden only after a successful draw, and a mid-draw
   throw restores it.
6. **Detail payloads are now skipped for layers under the sampling cap**
   (`stride == 1`) — no duplicate bytes for a layer the overview already
   draws in full.

### Setup

Same real cached Delaware County pickles (`buildings.pkl` 26,393 /
`roads.pkl` 28,865 / `sidewalks.pkl` 7,135 / `study_area.pkl`), same
production call path (`backend.hcas._overview_geoms` →
`backend.assess.renderer._clip_to_study_area` → `backend.hcas._detail_payload`
→ `backend.assess.render_assessment_map`, matching `_run_step_7` exactly,
including the new `stride > 1` skip). Rendered size: **11,079,342 bytes
(11.08 MB)** — matches the coordinator's stated 11,077,515 bytes to within
0.02% (a few bytes of float-formatting noise across environments, not a
discrepancy worth chasing). Clipped feature counts: buildings 22,953 /
roads 22,759 / sidewalks 5,388 — exactly as stated. Headless
Playwright/Chromium, viewport 1200×800, no visible window.

### Results — all nine checks

| # | Check | Result |
|---|-------|--------|
| 1 | Loads at all | **PASS** — `load` event at 0.701s, map `_loaded===true` at 0.710s. |
| 2 | Zero console errors | **PASS** — 0 console errors, 0 uncaught page errors. |
| 3 | Zero network requests for feature data | **PASS** — 59 requests: CDN libraries + CartoDB tiles only, zero feature-data requests. |
| 4 | County zoom labelled overview | **PASS** — zoom 11, exact expected sentence for all three layers. |
| 5 | z16 dense subdivision, no gaps | **PASS** — screenshot `r3-z16-dense-subdivision.png`, blocks fill edge-to-edge. |
| 6 | Roads not closed rings | **PASS** — every drawn road/sidewalk `HCASViewportLayer` layer at z16 (777 + 161 = 938 layers) is `instanceof L.Polyline` and **not** `L.Polygon`, checked directly on the real Leaflet objects. |
| 7 | Buildings with holes render as voids | **PASS** — target building (2 interior rings). Pixel test: hole point RGB(237,237,237) vs. fill point RGB(214.5,214.5,214.5), delta 39.0. Visual crop `r3-building-hole-void.png` shows a clean rectangular void cut into the fill. |
| 8 | Rendered count == Python-computed count for the same bbox — **now a genuinely independent test** | **PASS, exact match: 3,556 == 3,556.** Per the coordinator's specific concern, this was NOT a replay of the embedded payload: a second, separate script (`rebuild_independent.py`) re-derived the buildings detail payload from the raw pickle via a fresh `_clip_to_study_area` + `_detail_payload` call, confirmed it byte-for-byte identical to what got embedded (determinism check), then computed the expected count for Leaflet's actual `getBounds()` from THAT independently-rebuilt payload. Additionally, per the coordinator's suggestion: checked every one of 293,946 embedded ring-0 vertices across all three layers against the real county polygon (loaded fresh from `study_area.pkl`) with an 0.0002°(~20m) buffer — **0 violations, 0 vertices outside**, max distance 0.0. Nothing draws outside the study area. |
| 9 | Pan/zoom responsiveness | **PASS** — 5 pans at z16 measured 15.5–20.1ms round-trip each, well under 100ms. Max drawn on any layer: 3,556, under the 5,000 budget. |

### The four attention items

- **Check 8 strengthened (see above):** independent rebuild + full boundary-containment scan, not a self-consistent replay. This is the test that would have caught the unclipped-detail-tier bug round 2 missed.
- **Layer control now matters:** unticked "Buildings" at z16 via the real checkbox (`.leaflet-control-layers-overlays` input, clicked through Playwright, not simulated) — `map.hasLayer(buildingsContainer)` went `true → false` and the screenshot (`r3-buildings-unticked.png`) shows every building gone (both tiers; the FeatureGroup nests the canvas group, so one uncheck removes both), roads still drawn. Sidewalks (`show=False` by default): confirmed `mapHasContainer: false` at z16 before any interaction; ticked the checkbox, confirmed `true` and 161 features rendered (`r3-sidewalks-ticked.png`).
- **No double-draw above the gate:** `HCASViewportLayer.overviewHidden` was `false` below the gate (zoom 11, canvas empty, overview visible — correct) and `true` for all three layers at z16 (canvas populated, overview sublayer genuinely removed from its FeatureGroup via `container.removeLayer`, not merely drawn under/over it) — checked via the live JS object state, not inferred from a screenshot alone.
- **`GeometryCollection`/`MultiLineString` at the clip boundary:** the real clip produced 3 MultiPolygon buildings, 68 MultiLineString roads, 101 MultiLineString sidewalks (no `GeometryCollection` in this particular county's data — the code path for it exists and is unit-tested per the fix-wave commit, just not exercised by this specific real dataset). `_detail_payload` logged zero silent drops. Screenshots at two real boundary-clipped MultiLineString road locations (`r3-county-edge-multilinestring.png` — "Smothers Road" running exactly along the county line) show the clipped segments rendering correctly, following the real road, no spurious jumps or malformed artifacts.

### Verification scripts (scratch, not committed)

`render_real_map.py` (updated to match the new clip-before-detail +
stride-skip logic), `rebuild_independent.py` (the independent rebuild +
containment check), `verify_map2.py` (main harness — layer control,
double-draw, and all nine checks), `analyze_payload.py` (re-run against the
new clipped payload; hole-building indices shifted from round 2 since 4 of
the 35 holed buildings were clipped out), `edge_check.py` (boundary
screenshots). All under the session scratchpad.

New screenshots under
`docs/measurements/screenshots-2026-08-12-viewport-verification/`:
`r3-county-overview.png`, `r3-z16-dense-subdivision.png`,
`r3-buildings-unticked.png`, `r3-sidewalks-ticked.png`,
`r3-building-hole-void.png`, `r3-county-edge-multilinestring.png`.

No production code was modified during this verification.

---

## Round 2 history: clip-before-detail fix re-measurement note (as originally written by the fix-wave commit, 2026-08-13)

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

## Round 2 (commit `8888d0b`, superseded by round 3 above)

**This run reported PASS 9/9, but against a renderer whose detail tier was
not yet clipped to the study area, and whose check 8 replayed the same
payload the page embedded rather than an independently-rebuilt one — the
exact combination that let the unclipped-tier defect ship undetected. Round
3 above is the current, trustworthy result.** Kept for the record.

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
