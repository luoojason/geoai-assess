# 2026-08-12: Browser verification of viewport map rendering — FAILED

**Status: the map is broken when opened the way the recipient will actually open it.**
This is not a partial pass. Loaded standalone (`file:///...`, no server) with
real Leaflet in a real headless Chromium browser, the map area never renders
anything — no tiles, no boundary, no buildings, no roads. The page is blank
except the floating metrics panel. Do not report the plan complete.

Everything below is real output from a real browser against the real
production render path, not a stub.

## Setup

- Branch `hcas`, worked in place (no worktree), per instructions.
- Interpreter: `.venv/Scripts/python.exe`. Installed `playwright` + Chromium
  and `Pillow` into that venv (neither was present before this task).
- Rendered through the **actual production call path**: loaded the cached
  Delaware County pickles (`buildings.pkl` 26,393 / `roads.pkl` 28,865 /
  `sidewalks.pkl` 7,135 / `study_area.pkl` 1 county polygon, EPSG:5070) and
  called `backend.hcas._overview_geoms`, `backend.hcas._detail_payload`, and
  `backend.assess.render_assessment_map` exactly as `_run_step_7` calls them
  — same functions, same argument shapes, same order. Script:
  `render_real_map.py` (scratch, not committed).
- Browser: Playwright, Chromium, **headless**, viewport 1200x800 (matching
  the pane size the gate-zoom measurement doc used). No visible window was
  ever opened.

## Real render measurements

```
raw feature counts: {'buildings': 26393, 'roads': 28865, 'sidewalks': 7135}
sample_info: {'buildings': {'shown': 3300, 'total': 26393, 'stride': 8},
              'roads': {'shown': 3208, 'total': 28865, 'stride': 9},
              'sidewalks': {'shown': 2379, 'total': 7135, 'stride': 3}}
detail totals: {'buildings': 26394, 'roads': 28865, 'sidewalks': 7135}
detail payload build time: 1.69s
render time: 1.50s
HTML size bytes: 12,714,953  (12.7150 MB — matches the brief's "roughly 12.71 MB")
```

This confirms the payload/render layer (Tasks 1–7, exercised via pure Python
+ pytest) produces exactly the artifact the brief described. **The failure
below is specifically in what a browser does with that artifact.**

## Checklist results

| # | Check | Result |
|---|-------|--------|
| 1 | Loads at all | **FAIL** — outer HTML document parses (`load` event ~0.6–3.6s), but the Leaflet map inside the iframe never initializes. Polled up to 260s across three separate runs; the map variable stayed `undefined` the entire time. This is a hang, not a slow load. |
| 2 | Zero console errors | **FAIL** — one uncaught `TypeError: Cannot read properties of undefined (reading 'addLayer')`, thrown from `HCASViewportLayer.attach()` on the very first instantiation (buildings). |
| 3 | Zero network requests for feature data | **PASS** — 11 total network requests, all CDN library assets (`leaflet@1.9.3`, `jquery-3.7.1`, `bootstrap@5.2.2`, `fontawesome`, `awesome-markers`). None reference buildings/roads/sidewalks/payload data. The embedding-not-fetching design works; this part of the standalone requirement holds. (No tile requests were reached either, because the map itself never initializes — CartoDB tiles are exonerated only by the control render below, see "Root cause".) |
| 4 | County zoom shows labelled overview | **Text present, but moot.** The exact expected sentence — `buildings: showing 3,300 of 26,393 (1 in 8) at this zoom. Zoom in and every one in view is drawn.` — is present in the DOM (it's static HTML added to folium's `html` section, unaffected by the script failure). But there is no map underneath it to zoom into: see screenshot. |
| 5 | Zoom past gate (z16) draws building detail | **BLOCKED** — cannot be tested; the map cannot be zoomed because it never exists. |
| 6 | Roads are not closed rings | **BLOCKED** — no road layer ever attaches (the exception aborts the injection script before the roads IIFE runs). |
| 7 | Buildings with holes render as voids | **BLOCKED** — same reason. |
| 8 | Rendered count vs. Python-computed count for a bbox | **BLOCKED** — zero features are ever drawn by `HCASViewportLayer`, so there is nothing to count. (The Python-side half of this check — replicating the client's grid-cell selection against `map_payload.py`'s `cells` index — was written and works; it has nothing to compare against.) |
| 9 | Pan/zoom responsiveness (<100ms/update, ≤5,000 draw budget) | **BLOCKED** — same reason. |

## Screenshot evidence

- `docs/measurements/screenshots-2026-08-12-viewport-verification/broken-blank-render.png`
  — the actual 12.71 MB production render, 8s after load, headless
  Chromium, `file://` with no server. Blank white page; only the metrics
  panel (static HTML) is visible. No tiles, no boundary, no zoom control, no
  layer control.
- `docs/measurements/screenshots-2026-08-12-viewport-verification/control-overview-only-render.png`
  — the same real Delaware data, same code path, **with `detail_payloads`
  omitted** (i.e. `render_assessment_map(..., detail_payloads=None)`, the
  exact fallback branch `_run_step_7` itself takes when the detail-embedded
  HTML would exceed 25 MB). This loads and becomes interactive in **0.6
  seconds**, zero console errors: full basemap, county boundary, buildings,
  roads, layer control, legend with the correct non-detail tail ("All 26,393
  were measured and counted in the metrics above."). This isolates the
  defect precisely to the `detail_payloads` script injection — the rest of
  the renderer is sound.

## Root cause (diagnosed, not fixed — reporting per instructions)

`backend/assess/renderer.py`, in `render_assessment_map`, when
`detail_payloads` is truthy, does:

```python
m.get_root().script.add_child(folium.Element("\n".join(blocks)))
```

This call runs **eagerly at Python build time**, before `m._repr_html_()` is
ever invoked. folium defers the actual Leaflet map creation code
(`var map_xxx = L.map(...)`) until render time: `folium.Map` is itself a
`MacroElement` whose `.render()` method emits that line into
`figure.script`'s children — but only when `.render()` runs, which happens
inside `_repr_html_()`, i.e. **after** our `add_child` call already occupied
a slot in that ordered dict. Python dicts (and folium's child registry)
preserve insertion order, so **our block, inserted first, is emitted into
the HTML before the map's own initialization script.**

Verified directly against the unescaped `srcdoc` content of the real
render (`html.unescape`'d, then searched by byte offset):

```
var map_38ee193326e4fa34c301fbbfea650d56 = L.map(...)   -> byte offset 9,623,721
new HCASViewportLayer(map_38ee193326e4fa34c301fbbfea650d56, ...) -> byte offset 5,047,723
```

The instantiation runs ~4.6 MB earlier in the document than the variable it
references is assigned. At that point `map_38ee...` exists (hoisted `var`,
per-script) but is `undefined`. `HCASViewportLayer.attach()` calls
`this.group.addTo(this.map)`; Leaflet's `addTo` immediately calls
`map.addLayer(this)` on the `undefined` map, throwing the observed
`TypeError`. Full stack trace, captured via Playwright's `pageerror` event:

```
Uncaught TypeError: Cannot read properties of undefined (reading 'addLayer')
    at addTo (https://cdn.jsdelivr.net/npm/leaflet@1.9.3/dist/leaflet.js:4:66354)
    at HCASViewportLayer.attach (about:srcdoc:158:15)
    at <anonymous> (about:srcdoc:228:5037486)
    at <anonymous> (about:srcdoc:228:5037497)
```

Because all three per-layer instantiations (buildings, roads, sidewalks)
are joined into **one** `folium.Element`/script tag, the buildings
instantiation throws first and the roads/sidewalks IIFEs after it in the
same script tag never run at all — confirmed by exactly one `pageerror`
event, not three.

This is the same failure class Task 7's own brief anticipated and tried to
guard against ("script section, not HTML section... `ReferenceError`") —
moving the injection to the `script` section was necessary but not
sufficient. It fixed the coarse html-vs-script ordering (the map variable
does exist as a name in scope by the time our code runs, so it's not a
`ReferenceError`) but did not account for folium's **deferred** rendering of
the map's own initialization script, which still lands after an eagerly
`add_child`-ed script even when both are nominally in the same `script`
section.

Separately unresolved: even beyond the immediate exception, the actual
`var map_xxx = L.map(...)` statement is never observed to execute within
260+ seconds of polling (three independent runs). Whether that is solely
downstream of the aborted script tag, or whether parsing/executing the
~12 MB single inline script (the `viewport_layers.js` module text plus
three JSON-literal payloads, roughly 500K+ small nested-array literals) is
independently pathologically slow in this configuration, was not
determined — diagnosing further would mean modifying or bisecting
production code, which is out of scope for this task per instructions.
Either way, the practical, user-facing result is identical: **the map does
not become usable within any tolerable wait.**

## What was NOT touched

No production code was modified. `backend/assess/renderer.py`,
`backend/hcas.py`, and `frontend/static/viewport_layers.js` are unchanged
from `hcas` branch commit `0cfc704`. This defect needs a fix dispatched
separately (e.g. rendering the detail-layer script via a call that runs
after the Map macro element's own script is emitted, or explicitly
appending after `m.render()`/inside a `DOMContentLoaded`/`window.onload`
guard rather than relying on child-insertion order).

## Verification scripts (scratch, not committed)

- `render_real_map.py` — production-path render, writes
  `delaware_map.html` (12.71 MB) + `detail_payloads.json`.
- `render_overview_only.py` — diagnostic control render with
  `detail_payloads=None`.
- `analyze_payload.py` — computed the densest building grid cell and the
  35 holed-building targets from the real payload (unused once the map
  failed to load, but written and correct — kept for whoever picks up the
  fix, so the re-verification pass doesn't have to redo this analysis).
- `verify_map.py` — the full 9-point Playwright verification harness
  (instance introspection via a `HCASViewportLayer` constructor proxy
  installed with `context.add_init_script`, per-viewport `refresh()` timing,
  pixel-level hole-vs-fill sampling with Pillow, Python-side replication of
  the client's grid-cell selection for the count cross-check). It never got
  past the readiness gate because the map never becomes ready — kept as-is
  so a fix can be verified by simply re-running it.
- `isolate_error.py` / `isolate_error2.py` / `isolate_error3.py` — root-cause
  isolation scripts.

All under the session scratchpad, not the repo — none of these ship.
