/* Viewport rendering for HCAS assessment maps.
 *
 * The map embeds every feature (building/road/sidewalk) but draws only the
 * grid cells the camera currently covers, and only once zoomed in past the
 * gate. Below the gate a thinned overview is drawn by Folium in the normal
 * way and this module stays out of the way. Do not delete the gate: at low
 * zoom the viewport can cover the entire study area, which would mean
 * drawing every embedded feature at once — thousands of polygons rebuilt on
 * every pan/zoom tick — and that is the exact browser freeze this guards
 * against. gateZoom = 16 was measured, not guessed: see task-6-brief.md
 * correction #4 (z15's densest Delaware viewport already holds 5,332
 * buildings, over the 5,000 draw budget).
 *
 * Coordinates in `payload.geoms` arrive as plain [lon, lat] pairs already
 * rounded to 6 decimal places by backend/assess/map_payload.py — there is no
 * compact/delta encoding on the wire, so this module does no decoding, only
 * the [lon, lat] -> [lat, lng] axis swap Leaflet requires.
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

  function HCASViewportLayer(map, payload, opts) {
    opts = opts || {};
    this.map = map;
    this.payload = payload;
    // A missing or malformed gate must fail CLOSED. undefined would make
    // `getZoom() < this.gateZoom` false at every zoom, which silently disables
    // the gate and draws every embedded feature on every pan/zoom tick — the
    // freeze this module exists to prevent. Infinity means "never above the
    // gate", so the map falls back to the labelled overview instead of hanging.
    // This is a backstop against a caller bug, not a supported way to call the
    // module — gateZoom is still required in intent; no default is documented.
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
    // 'polygon' (default) closes and fills each ring — buildings, and
    // anything with possible holes. 'polyline' draws only rings[0] as an
    // open path — roads and sidewalks are LineStrings, and a polygon would
    // wrongly join each one's last point back to its first.
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
    this.group.addTo(this.map);
    this.map.on("moveend zoomend", this._onMove);
    this.refresh();
  };

  HCASViewportLayer.prototype.detach = function () {
    if (!this._valid) return;
    this.map.off("moveend zoomend", this._onMove);
    this.map.removeLayer(this.group);
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
    var opts = Object.assign({ renderer: this.renderer }, this.style);
    if (this.shape === "polyline") {
      // A LineString round-trips as a single ring; any further rings can't
      // occur for a part built from a LineString, so only rings[0] is used.
      return L.polyline(rings[0], opts);
    }
    // L.polygon(rings) renders rings[0] as the outline and every remaining
    // ring as a hole — exactly the shape build_payload emits for polygons.
    return L.polygon(rings, opts);
  };

  HCASViewportLayer.prototype.refresh = function () {
    if (!this._valid) return;
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
    // Not a numeric ordering — a stable set-equality key. Sorting only
    // reorders the tokens (integer indices can't contain ","), so two
    // distinct index sets can never serialize equal; that's all this needs.
    var key = Object.keys(wanted).sort().join(",");
    if (key === this.drawn) return;        // same features, nothing to redo
    this.group.clearLayers();
    var self = this;
    Object.keys(wanted).forEach(function (i) {
      var layer = self._buildLayer(i);
      if (layer) self.group.addLayer(layer);
    });
    this.drawn = key;
  };

  global.HCASViewportLayer = HCASViewportLayer;
})(window);
