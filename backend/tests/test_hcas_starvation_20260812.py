"""Regression tests for the 2026-08-12 step-3 report (Delaware County, Ohio 39041).

Seven of eight layers came back empty, each saying "the OpenStreetMap server did
not answer for this area in time". Measured that morning, both halves of that
sentence were wrong:

  * The county answers fine. A whole-county buildings query returned 26,532
    elements (23.3 MB) in 12.9 s and roads 47,869 elements (42.8 MB) in 14.1 s,
    against overpass-api.de, cold, while the run was being diagnosed. Both
    "failed" layers completed when re-run alone (buildings 25,469 features in
    304 s, greenspace 2,126 in 283 s).

  * Five of the seven were never sent to Overpass at all. Step 3 runs eight
    layers through two workers under ONE shared wall-clock budget, so two slow
    layers hold both workers for the whole step and the remaining five never
    start — then get reported as server timeouts, with `fetch_seconds` set to
    the step's elapsed time rather than their own.

What made the two layers slow was the cascade: Overpass refuses a fraction of
requests under load (the identical query measured 504, then 200, then 200, then
429 within one minute, each refusal costing ~20 s), and the app answers a
refusal by QUARTERING the area — which `osm_layers` line 584 already documents
as the wrong response, because it multiplies the requests that were refused.
The reproduction logged it verbatim:

    Overpass query still failing ... after 2 attempts (ConnectTimeout(...))
    - splitting into 4 (depth 1)

A connection that was never established says nothing about how big the area is.

None of these tests touch the network.
"""
import time

import pytest
import requests
from osmnx._errors import ResponseStatusCodeError


def _status_error(code: int) -> ResponseStatusCodeError:
    """osmnx surfaces an HTTP failure by putting the status line in the message
    (`_http._parse_response`), so that is what a classifier has to read."""
    return ResponseStatusCodeError(
        f"'overpass-api.de' responded: {code} Gateway Timeout <!DOCTYPE html>"
    )


class TestRefusalIsNotEvidenceOfSize:
    """A refused request says nothing about how big the area is."""

    @pytest.mark.parametrize("exc", [
        requests.exceptions.ConnectTimeout("connect timeout=10"),
        requests.exceptions.ConnectionError("connection reset"),
        requests.exceptions.ReadTimeout("read timeout=90"),
        _status_error(429),
        _status_error(502),
        _status_error(503),
        _status_error(504),
    ])
    def test_transport_and_overload_failures_are_refusals(self, exc):
        from backend.assess import osm_layers as ol

        assert ol._is_refusal(exc) is True

    @pytest.mark.parametrize("exc", [
        _status_error(400),                      # Overpass rejected the QUERY
        ValueError("truncated response"),        # the documented oversize signal
    ])
    def test_a_bad_query_or_truncated_body_is_not_a_refusal(self, exc):
        """The 2026-08-07 Grand Isle measurement — a whole-county query that
        really was too large — failed with a truncated response, not a refused
        connection. That case must still be allowed to split."""
        from backend.assess import osm_layers as ol

        assert ol._is_refusal(exc) is False


class TestRefusalDoesNotQuarter:
    def test_a_refused_area_is_retried_not_split(self, monkeypatch):
        """One ConnectTimeout used to fan a single request out toward 85."""
        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_probe_overpass_once", lambda: None)
        asked = []

        def call_one(bbox):
            asked.append(tuple(round(v, 6) for v in bbox))
            raise requests.exceptions.ConnectTimeout("connect timeout=10")

        whole = (-83.25, 40.12, -82.74, 40.44)
        with pytest.raises(Exception):
            ol._tiled(whole, call_one)

        distinct = set(asked)
        assert distinct == {tuple(round(v, 6) for v in whole)}, (
            f"a refused connection was answered by splitting the area: {distinct}"
        )
        assert len(asked) > 1 + ol.OSM_TILE_RETRIES, (
            "a refusal should buy more attempts at the same area, not fewer"
        )

    def test_a_refusal_that_clears_returns_without_splitting(self, monkeypatch):
        """Refusals are intermittent — measured 504, 200, 200, 429 on one query
        inside a minute. Retrying the whole area is what gets the data."""
        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_probe_overpass_once", lambda: None)
        asked = []

        def call_one(bbox):
            asked.append(tuple(bbox))
            if len(asked) <= 2:
                raise _status_error(504)
            return "the whole county"

        whole = (-83.25, 40.12, -82.74, 40.44)
        assert ol._tiled(whole, call_one) == ["the whole county"]
        assert set(asked) == {whole}, "recovered, but only after splitting"

    def test_a_genuinely_oversize_area_still_splits(self, monkeypatch):
        """Grand Isle VT 2026-08-07: whole county failed, four quadrants worked.
        That path must survive this change."""
        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_probe_overpass_once", lambda: None)
        whole = (-83.25, 40.12, -82.74, 40.44)
        asked = []

        def call_one(bbox):
            asked.append(tuple(bbox))
            if tuple(bbox) == whole:
                raise ValueError("truncated response")
            return f"tile {len(asked)}"

        out = ol._tiled(whole, call_one)
        assert len(out) == 4, f"expected the area to be quartered, got {out}"
        assert len(set(asked)) == 5      # the whole county plus four quadrants


class TestUnattemptedLayersAreNotBlamedOnOverpass:
    def _run(self, monkeypatch, budget_s=8.0):
        from unittest import mock
        import threading

        import geopandas as gpd
        from backend import hcas

        monkeypatch.setenv("OSM_LAYER_WORKERS", "2")
        monkeypatch.setattr(hcas, "OSM_RETRY_PASSES", 0)

        entered, lock = [], threading.Lock()

        def make(name):
            def f(bbox):
                with lock:
                    entered.append(name)
                time.sleep(0.5 if name == "facilities" else 60)
                return gpd.GeoDataFrame(geometry=[], crs="EPSG:5070")
            f.__name__ = f"fetch_{name}"
            return f

        names = ["buildings", "facilities", "greenspace", "landuse",
                 "parking", "roads", "sidewalks", "transit_stops"]
        fetchers = {n: make(n) for n in names}
        with mock.patch.object(hcas, "_layer_fetchers", lambda: fetchers):
            results, errors, detail = hcas._fetch_layers(
                "starve-test", 3, names, (-83.25, 40.12, -82.74, 40.44),
                budget_s=budget_s,
            )
        with lock:
            return sorted(set(entered)), errors, detail

    def test_a_layer_that_never_ran_does_not_claim_overpass_failed(self, monkeypatch):
        ran, errors, _ = self._run(monkeypatch)
        never = sorted({"buildings", "facilities", "greenspace", "landuse",
                        "parking", "roads", "sidewalks", "transit_stops"} - set(ran))
        assert never, "the scenario did not starve any layer"
        for n in never:
            assert "did not answer" not in errors[n], (
                f"{n} was never sent to Overpass but blames it: {errors[n]!r}"
            )
            assert "not attempted" in errors[n].lower(), (
                f"{n} should say it never ran, got: {errors[n]!r}"
            )

    def test_a_layer_that_never_ran_reports_no_fetch_time(self, monkeypatch):
        """900.0 s against a layer that was never sent is the step's clock, not
        the layer's, and it is what made this look like eight dead requests."""
        ran, _, detail = self._run(monkeypatch)
        for slot in detail:
            if slot["layer"] in ran:
                continue
            assert not slot.get("elapsed_s"), (
                f"{slot['layer']} never ran but reports "
                f"{slot.get('elapsed_s')}s of fetch time"
            )

    def test_a_layer_that_did_run_is_timed_from_its_own_start(self, monkeypatch):
        """`facilities` returns in 0.5 s. It used to be credited with however
        long the whole step had been going when it happened to finish."""
        ran, _, detail = self._run(monkeypatch)
        slot = next(d for d in detail if d["layer"] == "facilities")
        assert "facilities" in ran
        assert slot["elapsed_s"] < 3.0, (
            f"facilities took ~0.5 s but reported {slot['elapsed_s']}s"
        )
