# Map payload sizes — Delaware County OH (FIPS 39041), 2026-08-12

## Headline

**Approach A (remove the 3,500-feature cap, drop `_MAP_SIMPLIFY_M` to 1.5,
pass `prefer_canvas=True` to Folium) almost satisfies the whole draw budget
on its own — but not quite.** Unrounded, the full three-layer render is
**25.39 MB**, about 1.5% over the 25 MB budget. Adding one trivial change —
rounding coordinates to 6 decimal places before serialization, a `round()`
call, not the grid/binary encoder Task 4 proposes — drops it to **21.88 MB**,
comfortably under budget. **`ENCODE = false`: Task 4 (the compact coordinate
encoder) is deleted from the plan.** The 45 MB Franklin County claim behind
the 3,500 cap predates `_slim_for_map`'s tag-column stripping and does not
hold for real 2026-08-12 Delaware data — full-resolution buildings + roads +
sidewalks, uncapped, is roughly half that, and rounded is closer to a fifth.

`GATE_ZOOM` and `GRID_CELL_DEG` are still reported below since Tasks 3 and 6
consume them independently of the encoding decision.

## Data provenance

Fetched 2026-08-12 through production's own fetchers (`backend.assess.osm_layers`),
via `overpass.kumi.systems` failover — the primary Overpass endpoint
connect-timed-out. Mirror counts run ~0.5% below the primary's raw count
(primary reported 26,532 buildings historically; this run's mirror pull is
26,393). Irrelevant to the byte measurements below (same real geometry,
same simplification code path), recorded per instruction.

| Layer | Features | Geometry type | Source |
|---|---|---|---|
| buildings | 26,393 | Polygon/MultiPolygon | `buildings.pkl` |
| roads | 28,865 | LineString | `roads.pkl` |
| sidewalks | 7,135 | LineString | `sidewalks.pkl` |
| study_area | 1 (1,184.45 km²) | Polygon, EPSG:5070 | `study_area.pkl` |

All match the brief's expected magnitudes (~26,393 / ~28,865 / ~7,135) —
proceeded with measurement rather than stopping.

## Step 2: per-layer, per-tier sizes

For each layer, geometry-only (`gdf[["geometry"]]`), simplified in EPSG:5070
then reprojected to EPSG:4326, `len(json.dumps(gdf.__geo_interface__))`.
"Ring count" = sum over features of (exterior + interior rings) for polygons,
or number of line parts for LineString/MultiLineString (all three layers here
are single-part, so ring count == feature count for roads/sidewalks; one
building is a MultiPolygon, hence 26,442 rings vs 26,393 features).

| Layer | Tier | simplify (m) | Features | Rings | Raw GeoJSON (bytes) | 6dp-rounded (bytes) | Savings |
|---|---|---:|---:|---:|---:|---:|---:|
| buildings | overview | 12.0 | 26,393 | 26,442 | 8,923,126 | 7,414,443 | 16.9% |
| buildings | detail | 1.5 | 26,393 | 26,442 | 11,577,571 | 9,355,414 | 19.2% |
| roads | overview | 12.0 | 28,865 | 28,865 | 8,252,672 | 7,129,931 | 13.6% |
| roads | detail | 1.5 | 28,865 | 28,865 | 9,808,860 | 8,287,570 | 15.5% |
| sidewalks | overview | 12.0 | 7,135 | 7,135 | 2,119,161 | 1,791,553 | 15.5% |
| sidewalks | detail | 1.5 | 7,135 | 7,135 | 2,815,581 | 2,301,393 | 18.3% |

Detail-tier total (geometry only, all three layers): 24,202,012 bytes raw →
**19,944,377 bytes (19.94 MB) at 6dp rounding.**

Note: feature/ring counts are identical between tiers because no cap is
applied here — `simplify()` at these tolerances did not collapse any full
building/road/sidewalk to an empty geometry in this county.

## Step 3: whole-map render, cap removed

Temporarily edited (both reverted before commit, `git diff --stat` clean —
confirmed below):
- `backend/hcas.py`: `_MAP_MAX_FEATURES = 3500` → `10**9`, `_MAP_SIMPLIFY_M = 12.0` → `1.5`
- `backend/assess/renderer.py`: `folium.Map(...)` in `render_assessment_map` gained `prefer_canvas=True`

Rendered Delaware buildings + roads + sidewalks (greenspace/parking/transit/
facilities empty — not part of this measurement's cached layers) through
`render_assessment_map`.

| Render | Total HTML | Render time |
|---|---:|---:|
| Approach A, full float precision | 25,385,043 bytes (25.39 MB) | 9.68 s |
| Approach A + 6dp coordinate rounding | 21,877,172 bytes (21.88 MB) | 5.20 s |

The 6dp-rounded run reprojects each layer to EPSG:4326 and rounds every
vertex to 6 decimal places (`shapely.ops.transform`) before handing the
GeoDataFrames to the unmodified `render_assessment_map` — this exercises the
real render path (Folium/Leaflet HTML+JS boilerplate, layer control, study
area outline, clip-to-study-area) rather than just the geometry-only bytes
from Step 2. The ~1.18 MB gap between Step 2's summed detail-tier bytes
(24.20 MB) and the unrounded render (25.39 MB) is Folium/Leaflet framework
overhead, roughly constant regardless of coordinate rounding.

Render and `_slim_for_map` time (9.68s / 0.78s) are server-side Python
timings on this dev machine, not browser timings — **not evidence about
cold-load or pan frame time.**

**Cold-load time to first paint and pan frame time at z16: UNMEASURED,
deferred to Task 8's browser check.** No number is estimated or invented
for either. Per controller instruction, `ENCODE` below is decided on size
alone.

Production files confirmed reverted after measurement:

```
$ git diff --stat
(no output — clean)
```

## GATE_ZOOM

Method: real building centroids (26,393), reprojected to EPSG:3857. For
each candidate zoom `z`, the projected-meters viewport extent uses standard
256px-tile Web Mercator resolution `156543.03392804097 / 2^z` and an assumed
1200×800 px browser map pane. The densest such window over the centroids is
found with a 2D integral-image (prefix-sum) search over a 400×400 fine grid
spanning the centroid extent (bin size ≈140 m × 116 m) — this finds the true
max sliding-window count at that grid resolution, not just a fixed-origin
histogram.

| Zoom | Resolution (m/px) | Viewport (km) | Densest window (features) |
|---:|---:|---|---:|
| 8 | 611.50 | 733.8 × 489.2 | 26,393 |
| 9 | 305.75 | 366.9 × 244.6 | 26,393 |
| 10 | 152.87 | 183.5 × 122.3 | 26,393 |
| 11 | 76.44 | 91.7 × 61.2 | 26,393 |
| 12 | 38.22 | 45.9 × 30.6 | 24,429 |
| 13 | 19.11 | 22.9 × 15.3 | 10,838 |
| 14 | 9.56 | 11.5 × 7.6 | 8,290 |
| 15 | 4.78 | 5.7 × 3.8 | 5,332 |
| 16 | 2.39 | 2.9 × 1.9 | 2,427 |
| 17 | 1.19 | 1.4 × 1.0 | 986 |
| 18 | 0.60 | 0.7 × 0.5 | 364 |
| 19 | 0.30 | 0.4 × 0.2 | 178 |

z15's densest viewport (5,332) still exceeds the 5,000 budget; z16's (2,427)
does not.

**`GATE_ZOOM = 16`**

## GRID_CELL_DEG

Method: real building centroids reprojected to EPSG:4326 (lon range
-83.2496 to -82.7466, lat range 40.1256 to 40.4441). For each candidate cell
size, a fixed-origin grid (origin at the data's min lon/lat) is built with
`numpy.histogram2d`; the densest (max) cell count is recorded along with the
mean, p95, and p99 of nonempty cells to show the full distribution, not just
the max.

| Cell (deg) | Grid (nx × ny) | Densest cell | Mean nonempty | p95 | p99 |
|---:|---|---:|---:|---:|---:|
| 0.0200 | 27 × 17 | 1,608 | 101.5 | 369.2 | 1,209.6 |
| 0.0100 | 52 × 33 | 769 | 40.4 | 181.1 | 430.2 |
| 0.0080 | 64 × 41 | 645 | 30.7 | 147.1 | 303.8 |
| 0.0070 | 73 × 47 | 516 | 26.2 | 118.6 | 282.8 |
| 0.0065 | 79 × 50 | 446 | 24.0 | 108.0 | 252.1 |
| 0.0060 | 85 × 55 | 403 | 21.7 | 96.3 | 201.7 |
| 0.0055 | 93 × 59 | 326 | 20.3 | 88.0 | 198.0 |
| 0.0050 | 102 × 65 | 310 | 17.9 | 80.5 | 177.6 |
| 0.0040 | 127 × 81 | 199 | 13.6 | 62.0 | 123.6 |
| 0.0030 | 169 × 108 | 157 | 10.0 | 42.0 | 80.0 |
| 0.0020 | 253 × 161 | 71 | 6.4 | 25.0 | 43.0 |
| 0.0015 | 337 × 214 | 65 | 4.7 | 17.0 | 28.0 |
| 0.0010 | 505 × 320 | 34 | 3.1 | 10.0 | 14.0 |
| 0.0005 | 1008 × 638 | 17 | 1.6 | 3.0 | 5.0 |

0.007° gives a densest cell of 516 features, the closest of the candidates
tested to the ≈500 target (a tenth of the 5,000 draw budget). Note: a
fixed-origin grid can undercount the true densest window by up to the cell
size in each dimension if the real hotspot straddles a bin boundary (same
caveat as any non-sliding grid index); 0.007° is a reasonable working value,
not a tight bound.

**`GRID_CELL_DEG = 0.007`**

## Decisions

- **`ENCODE = false`** — plain 6-dp-rounded GeoJSON for all three layers,
  rendered through the real map pipeline, lands at 21.88 MB, under the 25 MB
  budget. Decided on size alone per controller instruction; pan frame time
  is UNMEASURED here (deferred to Task 8's browser check), not estimated.
  **Task 4 (compact coordinate encoder) is deleted from this plan.**
- **`GRID_CELL_DEG = 0.007`** (degrees) — densest cell ≈516 features, close
  to the ≈500 target.
- **`GATE_ZOOM = 16`** — lowest zoom whose densest viewport (1200×800 px
  assumed pane) holds ≤5,000 building features.
- **Approach A alone (cap removed + `prefer_canvas=True`, no rounding, no
  grid, no gate) does NOT quite satisfy the 25 MB budget** — it lands at
  25.39 MB, ~1.5% over. It satisfies the budget once combined with a
  one-line coordinate-rounding change (not Task 4's encoder). Whether that
  rounding gets folded into `_slim_for_map` directly, or whether the cap
  alone is judged close enough with a small safety margin (e.g. leaving
  `_MAP_MAX_FEATURES` at some large-but-finite value), is left to whoever
  scopes the remaining tasks — the point of this measurement is that the
  20-line "remove cap, add prefer_canvas, round coordinates" change is
  sufficient, and the multi-task grid/binary-encoding machinery (Task 4) is
  not needed.
