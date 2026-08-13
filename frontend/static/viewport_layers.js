/* Viewport rendering for HCAS assessment maps.
 *
 * The map embeds every feature of every layer and draws the grid cells the
 * camera currently covers. This is the only tier there is: gateZoom ships as
 * 0, so it draws at every zoom, and there is no sampled overview behind it.
 *
 * The gate USED to be 16, with a thinned overview below it, and that comment
 * told you not to delete it. Both are gone. The overview was capped at a fixed
 * 3,500 features per layer, so on a dense county it drew 1 in 58 — about one
 * building per two city blocks, reported by a user as the map failing to load.
 * The freeze the gate guarded against was then measured rather than assumed:
 * on New York County, all 96,294 clipped features build in 202 ms and pan in
 * 82-96 ms against a 100 ms budget
 * (docs/measurements/2026-08-12-map-viewport-verification.md). The gate is
 * kept as a parameter, not a policy — a caller can still gate a layer, and
 * the fail-closed guard below still refuses a malformed value.
 *
 * Coordinates in `payload.geoms` arrive as plain [lon, lat] pairs already
 * rounded to 6 decimal places by backend/assess/map_payload.py (see that
 * module for why there is no compact/binary encoder on the wire), so this
 * module does no decoding, only the [lon, lat] -> [lat, lng] axis swap
 * Leaflet requires.
 */
(function (global) {
  "use strict";

  // payload.geoms[i] is a LIST OF RINGS: [exteriorRing, hole1, hole2, ...],
  // each ring an array of [lon, lat] pairs (backend/assess/map_payload.py is
  // the authority on this shape). Leaflet wants [lat, lng], so every ring's
  // points get swapped; ring order and hole membership are preserved as-is
  // so L.polygon can render holes natively.
  function toLatLngRings(rings) {
    var out = [];
    for (var r = 0; r < rings.length; r++) {
      var ring = rings[r], swapped = [];
      for (var p = 0; p < ring.length; p++) {
        swapped.push([ring[p][1], ring[p][0]]);
      }
      out.push(swapped);
    }
    return out;
  }

  function HCASViewportLayer(map, payload, opts, overviewGroup, overviewLayer) {
    opts = opts || {};
    this.map = map;
    this.payload = payload;
    // `overviewGroup` is the Leaflet layer (typically the overview
    // FeatureGroup folium's LayerControl already knows about) this layer's
    // canvas group attaches to instead of the map directly. Both L.Map and
    // L.LayerGroup/L.FeatureGroup expose addLayer/removeLayer, so the same
    // calls work whichever was passed. Falling back to `map` when omitted
    // keeps this constructor's 3-arg form (map, payload, opts) working
    // exactly as before for any other caller.
    this.container = overviewGroup || map;
    // `overviewLayer` is the specific overview GeoJson sublayer INSIDE that
    // FeatureGroup — the one that actually draws the thinned overview
    // shapes. Above the gate, refresh() removes just this sublayer from
    // `container` (not the whole FeatureGroup, whose on/off state belongs
    // to LayerControl) so the overview and the full-detail canvas group
    // never draw the same features on top of each other. `null`/omitted
    // when the caller has no overview counterpart to hide.
    this.overviewLayer = overviewLayer || null;
    this.overviewHidden = false;
    // A missing or malformed gate resolves to Infinity: "never above the gate",
    // so the layer draws nothing at all.
    //
    // ⚠ The trade-off inverted when the overview tier was deleted. This guard
    // was written when failing closed meant falling back to a labelled overview
    // — harmless. There is no overview now, so a malformed gateZoom yields a
    // BLANK layer rather than a degraded one. It is still the safer of the two
    // (the alternative, undefined, makes `getZoom() < undefined` false at every
    // zoom and draws everything on every tick), but it is no longer free, and a
    // caller that reaches it has a bug worth surfacing rather than absorbing.
    // gateZoom is required in intent; no default is documented.
    //
    // `typeof x === "number"` is NOT enough: `typeof NaN === "number"` is
    // true, and `getZoom() < NaN` is false at every zoom — the exact fail-open
    // this guard exists to close. Number.isFinite rejects NaN and both
    // Infinities while accepting every real zoom value; do not "simplify"
    // this back to typeof. NaN is not hypothetical — Task 7 builds this call
    // by string-formatting a value into JS, and a missing/non-numeric
    // measurement flowing into that template produces NaN, not undefined.
    this.gateZoom = Number.isFinite(opts.gateZoom) ? opts.gateZoom : Infinity;
    this.style = opts.style || {};
    // Optional per-class palette. When the payload carries a `classes`
    // array (landuse does; nothing else currently), each part draws with
    // styles[classes[i]] instead of the single `style` above — so one
    // LayerControl entry can show farmland, housing and a quarry in
    // different colours without becoming four separate checkboxes.
    this.styles = Array.isArray(opts.styles) ? opts.styles : null;
    this.classes = (payload && Array.isArray(payload.classes)) ? payload.classes : null;
    // Three values. 'polygon' (default) closes and fills each ring —
    // buildings, greenspace, landuse, parking, anything with possible holes.
    // 'polyline' draws only rings[0] as an open path — roads and sidewalks are
    // LineStrings, and a polygon would wrongly join each one's last point back
    // to its first. 'point' draws a circleMarker at rings[0][0] — transit
    // stops and facilities are Points, and polygon/polyline on a
    // one-coordinate ring draw nothing at all, losing the layer in silence.
    this.shape = opts.shape || "polygon";
    this.group = L.layerGroup();
    this.renderer = L.canvas({ padding: 0.2 });
    this.cache = {};
    this.drawn = null;
    this._onMove = this.refresh.bind(this);
    // A payload with no usable geometry/index is a no-op layer rather than a
    // reason to throw: the map is a bonus feature and a malformed or absent
    // payload must never take the rest of the page down with it.
    this._valid = !!(payload && Array.isArray(payload.geoms) &&
                      payload.cells && typeof payload.cells === "object" &&
                      typeof payload.cell_deg === "number" && payload.cell_deg > 0);
  }

  HCASViewportLayer.prototype.attach = function () {
    if (!this._valid) return;
    // addLayer on `this.container`, not addTo(this.map): nesting the canvas
    // group inside the overview FeatureGroup (when one was passed) is what
    // lets folium's existing LayerControl govern both tiers as a single
    // on/off unit, instead of the canvas group being invisible to the
    // control and impossible to switch off.
    this.container.addLayer(this.group);
    this.map.on("moveend zoomend", this._onMove);
    this.refresh();
  };

  HCASViewportLayer.prototype.detach = function () {
    if (!this._valid) return;
    this.map.off("moveend zoomend", this._onMove);
    this.container.removeLayer(this.group);
    if (this.overviewHidden && this.overviewLayer) {
      this.container.addLayer(this.overviewLayer);
      this.overviewHidden = false;
    }
  };

  HCASViewportLayer.prototype._latlngs = function (i) {
    if (!this.cache[i]) {
      this.cache[i] = toLatLngRings(this.payload.geoms[i]);
    }
    return this.cache[i];
  };

  HCASViewportLayer.prototype._buildLayer = function (i) {
    var rings = this._latlngs(i);
    // map_payload.py never emits an entry with zero rings today, but this
    // module's contract is that a malformed entry must never throw and take
    // the page down with it — so a ringless entry is skipped, not drawn.
    if (!rings || !rings.length) return null;
    // Per-class style when both halves are present, else the single style.
    // An index outside the palette falls back rather than yielding
    // `undefined` options, which Leaflet would draw with its default blue.
    var style = this.style;
    if (this.styles && this.classes) {
      var ci = this.classes[i];
      if (typeof ci === "number" && this.styles[ci]) style = this.styles[ci];
    }
    var opts = Object.assign({ renderer: this.renderer }, style);
    if (this.shape === "point") {
      // Transit stops and facilities are Points. build_payload emits a Point
      // part as a single ring holding one coordinate, so rings[0][0] is the
      // position. L.polygon/L.polyline on a one-point ring draw nothing at
      // all — which is how these two layers would silently vanish rather
      // than fail loudly.
      if (!rings[0] || !rings[0].length) return null;
      return L.circleMarker(rings[0][0], opts);
    }
    if (this.shape === "polyline") {
      // A LineString round-trips as a single ring; any further rings can't
      // occur for a part built from a LineString, so only rings[0] is used.
      return L.polyline(rings[0], opts);
    }
    // L.polygon(rings) renders rings[0] as the outline and every remaining
    // ring as a hole — exactly the shape build_payload emits for polygons.
    return L.polygon(rings, opts);
  };

  // Public entry point, bound once in the constructor and used as the
  // moveend/zoomend handler. An uncaught throw from INSIDE Leaflet's event
  // dispatch aborts dispatch to every OTHER handler registered for the same
  // event on the same map — including the other two HCASViewportLayer
  // instances' own moveend/zoomend handlers — which would desynchronise
  // buildings/roads/sidewalks mid-pan exactly the way one bad layer used to
  // abort its two siblings at page-load time. So the actual work lives in
  // _refresh and this wrapper's only job is to make sure nothing it throws
  // ever reaches Leaflet's dispatcher; logged to console, not swallowed
  // silently.
  HCASViewportLayer.prototype.refresh = function () {
    try {
      this._refresh();
    } catch (e) {
      console.error("HCASViewportLayer refresh failed:", e);
    }
  };

  HCASViewportLayer.prototype._refresh = function () {
    if (!this._valid) return;
    if (this.map.getZoom() < this.gateZoom) {
      if (this.drawn !== null) { this.group.clearLayers(); this.drawn = null; }
      // Below the gate: the overview is the only tier that should be
      // visible, so restore its sublayer if a prior above-gate refresh
      // hid it.
      if (this.overviewHidden && this.overviewLayer) {
        this.container.addLayer(this.overviewLayer);
        this.overviewHidden = false;
      }
      return;
    }
    // Above the gate, but the overview sublayer is NOT hidden here anymore.
    // It used to be hidden right at this point, before the detail group was
    // even touched — so a throw anywhere in the drawing below (a malformed
    // cell entry, a _buildLayer edge case) left the layer showing NEITHER
    // tier: overview already removed, detail group never (or only
    // partially) populated. Blank, under a legend that promises "every one
    // in view is drawn". The hide now happens only after a successful draw,
    // at the bottom of this function; see the catch block below for the
    // case where the overview was already hidden by a PRIOR successful
    // refresh and THIS one fails.
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
    // Not a numeric ordering — a stable set-equality key. Sorting only
    // reorders the tokens (integer indices can't contain ","), so two
    // distinct index sets can never serialize equal; that's all this needs.
    var key = Object.keys(wanted).sort().join(",");
    if (key === this.drawn) return;        // same features, nothing to redo

    try {
      this.group.clearLayers();
      var self = this;
      Object.keys(wanted).forEach(function (i) {
        var layer = self._buildLayer(i);
        if (layer) self.group.addLayer(layer);
      });
    } catch (e) {
      // Drawing failed partway. Whatever ended up in `this.group` is
      // untrustworthy (possibly empty, possibly a partial set), so drop it
      // rather than leave stale/partial features on screen next to a
      // restored overview. Restore the overview unconditionally — covers
      // both "we hadn't hidden it yet this call" (nothing to do, it was
      // never removed) and "a PRIOR successful refresh had already hidden
      // it" (this is the case that used to strand the layer blank). Reset
      // `drawn` to null so the next moveend/zoomend retries the full draw
      // instead of trusting a key that no longer matches what's on screen.
      this.group.clearLayers();
      this.drawn = null;
      if (this.overviewLayer && this.overviewHidden) {
        this.container.addLayer(this.overviewLayer);
        this.overviewHidden = false;
      }
      throw e;   // refresh()'s own try/catch logs it; not swallowed here too
    }

    // Hide the overview's own GeoJson sublayer (not the whole FeatureGroup —
    // that stays under LayerControl's on/off state) ONLY after the detail
    // group drew successfully, so the same sampled features are never drawn
    // twice (overview + canvas, byte-identical styling) but a failed draw
    // can never leave neither tier visible.
    if (this.overviewLayer && !this.overviewHidden) {
      this.container.removeLayer(this.overviewLayer);
      this.overviewHidden = true;
    }
    this.drawn = key;
  };

  global.HCASViewportLayer = HCASViewportLayer;
})(window);
