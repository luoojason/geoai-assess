"""Regression tests for the 2026-08-11 step-3 stall (Adams County, Ohio 39001).

Step 3 sat at 2% and no layer ever completed inside the 420 s budget. Measured
that day from a clean install of the shipped archive:

  * overpass-api.de publishes TWO A records. 162.55.144.139 completed a TCP
    handshake 5/5 times in 0.1 s; 65.109.112.52 timed out 5/5 times at 8.0 s.
    DNS round-robins between them, so roughly half of all connections hung for
    the full connect timeout and were recorded as network failures — regardless
    of query, tile size, or server load. Pinned to the healthy address, the
    whole-county buildings query answered in 1.8 s (51,461 elements) and landuse
    in 2.3 s (69,863); unpinned, the same queries failed 14 times out of 14.

  * The step abandoned its futures at the budget but never stopped the threads:
    an idle server (progress cleared, no run active) was holding 14 Overpass
    sockets and still opening more. Those zombies consumed the app's whole
    per-IP Overpass allowance, so every RE-run started starved — which is why
    the stall reproduced every time and never got further.

These tests pin both behaviours. Neither touches the network.
"""
import time

import pytest


class TestUnreachableAddressRecords:
    """A host whose DNS hands out a dead address must not cost a full connect
    timeout on every other request."""

    def test_an_address_that_never_connects_is_dropped(self):
        from backend.assess import osm_layers as ol

        seen = []

        def reachable(ip, port=443, timeout=None):
            seen.append(ip)
            return ip != "65.109.112.52"

        dead = ol._unreachable_addresses(
            "overpass-api.de",
            resolve=lambda h: ["162.55.144.139", "65.109.112.52"],
            reachable=reachable,
        )
        assert dead == {"65.109.112.52"}
        assert sorted(seen) == ["162.55.144.139", "65.109.112.52"]

    def test_it_fails_open_when_nothing_answers(self):
        """If every address is unreachable the host is simply down. Dropping
        them all would turn a transient outage into a permanent one, so keep
        the list intact and let the real request report the failure."""
        from backend.assess import osm_layers as ol

        dead = ol._unreachable_addresses(
            "overpass-api.de",
            resolve=lambda h: ["162.55.144.139", "65.109.112.52"],
            reachable=lambda ip, port=443, timeout=None: False,
        )
        assert dead == set()

    def test_a_fully_healthy_host_loses_nothing(self):
        from backend.assess import osm_layers as ol

        dead = ol._unreachable_addresses(
            "overpass.kumi.systems",
            resolve=lambda h: ["193.219.97.30"],
            reachable=lambda ip, port=443, timeout=None: True,
        )
        assert dead == set()

    def test_resolution_drops_addresses_proven_dead(self, monkeypatch):
        from backend.assess import osm_layers as ol

        infos = [
            (2, 1, 6, "", ("162.55.144.139", 443)),
            (2, 1, 6, "", ("65.109.112.52", 443)),
        ]
        monkeypatch.setattr(ol, "_real_getaddrinfo", lambda *a, **k: infos)
        monkeypatch.setattr(ol, "_dead_addresses", {"65.109.112.52"})

        got = ol._overpass_getaddrinfo("overpass-api.de", 443)
        assert [i[4][0] for i in got] == ["162.55.144.139"]

    def test_a_host_we_do_not_own_is_never_filtered(self, monkeypatch):
        """The resolver hook is process-wide, so it must be inert for every
        hostname that is not an Overpass endpoint of ours."""
        from backend.assess import osm_layers as ol

        infos = [(2, 1, 6, "", ("65.109.112.52", 443))]
        monkeypatch.setattr(ol, "_real_getaddrinfo", lambda *a, **k: infos)
        monkeypatch.setattr(ol, "_dead_addresses", {"65.109.112.52"})

        assert ol._overpass_getaddrinfo("api.census.gov", 443) == infos

    def test_resolution_fails_open_when_every_address_is_dead(self, monkeypatch):
        from backend.assess import osm_layers as ol

        infos = [(2, 1, 6, "", ("65.109.112.52", 443))]
        monkeypatch.setattr(ol, "_real_getaddrinfo", lambda *a, **k: infos)
        monkeypatch.setattr(ol, "_dead_addresses", {"65.109.112.52"})

        assert ol._overpass_getaddrinfo("overpass-api.de", 443) == infos

    def test_the_verdict_expires_so_the_roles_can_swap(self, monkeypatch):
        """Measured 2026-08-11: 162.55.144.139 was reachable 5/5 and
        65.109.112.52 dead 5/5; half an hour later the two had traded places.
        A verdict cached for the life of the process would pin the app to
        whichever address had since died — the exact failure this prevents."""
        from backend.assess import osm_layers as ol

        both = [(2, 1, 6, "", ("162.55.144.139", 443)),
                (2, 1, 6, "", ("65.109.112.52", 443))]
        monkeypatch.setattr(ol, "_real_getaddrinfo", lambda *a, **k: both)
        monkeypatch.setattr(ol, "_dead_addresses", set())
        monkeypatch.setattr(ol, "_addr_checked", {})

        dead_now = {"65.109.112.52"}
        monkeypatch.setattr(ol, "_unreachable_addresses",
                            lambda host, **k: set(dead_now))

        ol._learn_dead_addresses("overpass-api.de")
        assert ol._dead_addresses == {"65.109.112.52"}

        # Same window: no re-check, verdict stands.
        dead_now = {"162.55.144.139"}
        ol._learn_dead_addresses("overpass-api.de")
        assert ol._dead_addresses == {"65.109.112.52"}, "re-checked too eagerly"

        # Verdict goes stale -> the roles are allowed to swap.
        monkeypatch.setattr(ol, "_addr_checked", {"overpass-api.de": time.time() - 1})
        ol._learn_dead_addresses("overpass-api.de")
        assert ol._dead_addresses == {"162.55.144.139"}, (
            "the recovered address is still shut out")


class TestFetchesStopAtTheDeadline:
    """Work abandoned by the step must actually stop. Before this, `_tiled` and
    `_with_overpass_failover` had no notion of the budget, so a layer the step
    had already given up on kept quartering and retrying for hours."""

    @pytest.fixture(autouse=True)
    def _restore_endpoint(self):
        """`_with_overpass_failover` rotates the process-wide endpoint on
        failure. Leaving it rotated is how a test in this suite poisons the
        next one, so put it back."""
        import osmnx as ox

        from backend.assess import osm_layers as ol

        before = ox.settings.overpass_url
        try:
            yield
        finally:
            ox.settings.overpass_url = before
            ol.clear_deadline()

    def _gdf(self, ids):
        import geopandas as gpd
        from shapely.geometry import Point

        return gpd.GeoDataFrame(
            {"geometry": [Point(0, i) for i, _ in enumerate(ids)]},
            index=list(ids), crs="EPSG:4326")

    def test_no_deadline_means_no_limit(self, monkeypatch):
        """Default behaviour is unchanged for every caller that sets nothing."""
        from backend.assess import osm_layers as ol

        ol.clear_deadline()
        monkeypatch.setattr(ol, "_probe_overpass_once", lambda: None)
        out = ol._tiled((0.0, 0.0, 0.1, 0.1), lambda b: self._gdf([1]))
        assert len(ol._combine(out)) == 1

    def test_tiling_stops_once_the_budget_has_expired(self, monkeypatch):
        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_probe_overpass_once", lambda: None)
        monkeypatch.setattr(ol, "OSM_TILE_MAX_DEPTH", 3)
        calls = []

        def always_fail(bbox):
            calls.append(bbox)
            raise OSError("timed out")

        ol.set_deadline(time.time() - 1)          # already past
        try:
            with pytest.raises(ol.BudgetExpired):
                ol._tiled((0.0, 0.0, 0.1, 0.1), always_fail)
        finally:
            ol.clear_deadline()
        assert calls == [], "not one request after the budget was gone"

    def test_it_does_not_quarter_a_tile_after_the_budget_expires(self, monkeypatch):
        """One failure used to fan out to as many as 85 sub-requests. If the
        budget dies during the first one, none of that fan-out may happen."""
        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_probe_overpass_once", lambda: None)
        monkeypatch.setattr(ol, "OSM_TILE_MAX_DEPTH", 3)
        calls = []

        def fail_and_burn_the_budget(bbox):
            calls.append(bbox)
            ol.set_deadline(time.time() - 1)
            raise OSError("timed out")

        ol.set_deadline(time.time() + 60)
        try:
            with pytest.raises(Exception):
                ol._tiled((0.0, 0.0, 0.1, 0.1), fail_and_burn_the_budget)
        finally:
            ol.clear_deadline()
        assert len(calls) == 1, f"quartered anyway: {len(calls)} requests"

    def test_start_tiles_stop_when_the_budget_dies_midway(self, monkeypatch):
        """A county is 9 start tiles. Dying on the second must not walk the
        remaining seven."""
        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_probe_overpass_once", lambda: None)
        monkeypatch.setattr(ol, "OSM_TILE_MAX_DEG", 0.05)
        calls = []

        def call_one(bbox):
            calls.append(bbox)
            if len(calls) == 2:
                ol.set_deadline(time.time() - 1)
            return self._gdf([f"{bbox[0]}:{bbox[1]}"])

        ol.set_deadline(time.time() + 60)
        try:
            out = ol._tiled((0.0, 0.0, 0.15, 0.15), call_one)
        finally:
            ol.clear_deadline()
        assert len(calls) == 2, f"kept going: {len(calls)} of 9 tiles"
        assert len(ol._combine(out)) == 2, "kept the tiles that did arrive"

    def test_failover_does_not_retry_once_the_budget_is_gone(self, monkeypatch):
        """The retry costs another full connect+read timeout on a step that has
        already been abandoned."""
        from backend.assess import osm_layers as ol

        calls = []

        def call():
            calls.append(1)
            ol.set_deadline(time.time() - 1)
            raise OSError("connect timeout")

        ol.set_deadline(time.time() + 60)
        try:
            with pytest.raises(OSError):
                ol._with_overpass_failover(call)
        finally:
            ol.clear_deadline()
        assert len(calls) == 1, "retried a request nobody is waiting for"

    def test_failover_still_retries_inside_the_budget(self, monkeypatch):
        """The fix must not disarm the failover that a live step depends on."""
        from backend.assess import osm_layers as ol

        calls = []

        def call():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("connect timeout")
            return "ok"

        ol.set_deadline(time.time() + 60)
        try:
            assert ol._with_overpass_failover(call) == "ok"
        finally:
            ol.clear_deadline()
        assert len(calls) == 2

    def test_the_deadline_is_per_thread(self):
        """Layers run concurrently; one layer's budget must not leak into
        another thread that never set one."""
        import threading

        from backend.assess import osm_layers as ol

        seen = {}

        def worker():
            seen["expired"] = ol._budget_expired()

        ol.set_deadline(time.time() - 1)
        try:
            t = threading.Thread(target=worker)
            t.start()
            t.join()
        finally:
            ol.clear_deadline()
        assert seen["expired"] is False


class TestOneRequestBeforeManySmallOnes:
    """The tiling premise was measured against the sick host and generalised.

    Same county, same layer, cache bypassed, 2026-08-11:

        whole county (1 request)  19.3 s  43,783 elements  0/1 failed
        0.25 deg     (4 requests) 88.0 s  29,517 elements  3/4 failed
        0.20 deg     (9 requests) 168.5 s 41,372 elements  5/9 failed

    A query returning ZERO elements still costs 12-19 s, so the per-request cost
    is a fixed floor and splitting can only add to it. Pre-splitting a county is
    what made step 3 unable to finish.
    """

    def _gdf(self, ids):
        import geopandas as gpd
        from shapely.geometry import Point

        return gpd.GeoDataFrame(
            {"geometry": [Point(0, i) for i, _ in enumerate(ids)]},
            index=list(ids), crs="EPSG:4326")

    def test_a_whole_county_is_one_request(self):
        """Adams County, Ohio (39001) — the reproduction — is 0.438 x 0.459
        degrees. It used to be pre-split into 9."""
        from backend.assess import osm_layers as ol

        assert ol._start_tiles((-83.7053, 38.5966, -83.2676, 39.0552)) == [
            (-83.7053, 38.5966, -83.2676, 39.0552)]

    def test_a_genuinely_enormous_area_is_still_pre_split(self):
        """The cap is a safety valve for areas far beyond a normal county, not
        a promise that everything goes in one request."""
        from backend.assess import osm_layers as ol

        assert len(ol._start_tiles((-120.0, 34.0, -116.0, 38.0))) > 1

    def test_a_transient_failure_is_retried_before_anything_is_split(self, monkeypatch):
        """Splitting is the answer to 'too big'. Every failure actually measured
        was a connection refusal, a 429 or a gateway 504 — for those, quartering
        turns one failed request into four, which is how the rate limiting got
        self-inflicted."""
        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_probe_overpass_once", lambda: None)
        monkeypatch.setattr(ol, "_retry_pause", lambda attempt: 0.0)
        seen = []

        def flaky(bbox):
            seen.append(bbox)
            if len(seen) == 1:
                raise OSError("connect timeout")
            return self._gdf([1])

        out = ol._tiled((0.0, 0.0, 0.4, 0.4), flaky)
        assert seen == [(0.0, 0.0, 0.4, 0.4)] * 2, "retried a different bbox"
        assert len(ol._combine(out)) == 1

    def test_splitting_still_happens_once_retries_are_exhausted(self, monkeypatch):
        """The adaptive fallback must survive — a genuinely oversized area still
        has to come down to something the server will answer."""
        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_probe_overpass_once", lambda: None)
        monkeypatch.setattr(ol, "_retry_pause", lambda attempt: 0.0)
        monkeypatch.setattr(ol, "OSM_TILE_MAX_DEPTH", 1)
        monkeypatch.setattr(ol, "OSM_TILE_RETRIES", 1)
        seen = []

        def too_big(bbox):
            seen.append(bbox)
            if bbox == (0.0, 0.0, 0.4, 0.4):
                raise OSError("gateway timeout")
            return self._gdf([f"{bbox[0]}:{bbox[1]}"])

        merged = ol._combine(ol._tiled((0.0, 0.0, 0.4, 0.4), too_big))
        assert seen[:2] == [(0.0, 0.0, 0.4, 0.4)] * 2, "split without retrying"
        assert len(merged) == 4, "did not fall back to quadrants"

    def test_retrying_never_sleeps_past_the_budget(self, monkeypatch):
        """A backoff that outlives the step is just a slower zombie.

        conftest zeroes the pause for the rest of the suite, so this test puts a
        deliberately huge one back — otherwise it would assert nothing.
        """
        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_probe_overpass_once", lambda: None)
        monkeypatch.setattr(ol, "_retry_pause", lambda attempt: 30.0)
        slept = []
        monkeypatch.setattr(ol.time, "sleep", lambda s: slept.append(s))

        def always_fail(bbox):
            raise OSError("connect timeout")

        ol.set_deadline(time.time() + 0.05)
        try:
            with pytest.raises(Exception):
                ol._tiled((0.0, 0.0, 0.4, 0.4), always_fail)
        finally:
            ol.clear_deadline()
        assert slept, "the backoff was never reached, so nothing was clamped"
        assert all(s <= 0.05 for s in slept), f"slept past the deadline: {slept}"


class TestAKnownOutageFailsFast:
    """When the pre-flight probe finds NOTHING answering, the retry/split
    machinery can only multiply the wait before reporting the same failure.

    Measured 2026-08-11 during a real outage: the probe said "no endpoint
    answered" at ~40 s, and the step then spent the entire 900 s budget to
    return zero layers.
    """

    def _gdf(self, ids):
        import geopandas as gpd
        from shapely.geometry import Point

        return gpd.GeoDataFrame(
            {"geometry": [Point(0, i) for i, _ in enumerate(ids)]},
            index=list(ids), crs="EPSG:4326")

    def test_an_outage_costs_one_attempt_not_a_cascade(self, monkeypatch):
        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_probe_overpass_once", lambda: None)
        monkeypatch.setattr(ol, "_outage_suspected_flag", True)
        monkeypatch.setattr(ol, "OSM_TILE_MAX_DEPTH", 3)
        calls = []

        def always_fail(bbox):
            calls.append(bbox)
            raise OSError("connect timeout")

        with pytest.raises(OSError):
            ol._tiled((0.0, 0.0, 0.4, 0.4), always_fail)
        assert calls == [(0.0, 0.0, 0.4, 0.4)], f"{len(calls)} requests during an outage"

    def test_failover_does_not_rotate_during_a_known_outage(self, monkeypatch):
        """Rotating means trying another host we just watched fail, for the
        price of a second full timeout."""
        import osmnx as ox

        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_outage_suspected_flag", True)
        before = ox.settings.overpass_url
        calls = []

        def call():
            calls.append(1)
            raise OSError("connect timeout")

        try:
            with pytest.raises(OSError):
                ol._with_overpass_failover(call)
        finally:
            ox.settings.overpass_url = before
        assert len(calls) == 1

    def test_a_working_endpoint_still_gets_the_full_treatment(self, monkeypatch):
        """The fast path must arm only on a probe that found nothing at all."""
        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_probe_overpass_once", lambda: None)
        monkeypatch.setattr(ol, "OSM_TILE_MAX_DEPTH", 1)
        assert ol._outage_suspected() is False
        calls = []

        def fail_then_split(bbox):
            calls.append(bbox)
            if bbox == (0.0, 0.0, 0.4, 0.4):
                raise OSError("gateway timeout")
            return self._gdf([f"{bbox[0]}:{bbox[1]}"])

        merged = ol._combine(ol._tiled((0.0, 0.0, 0.4, 0.4), fail_then_split))
        assert len(merged) == 4, "retry-then-split was disarmed outside an outage"

    def test_a_successful_probe_clears_a_previous_outage(self, monkeypatch):
        """The flag is process-wide; a later run on a healthy network must not
        inherit the last run's outage."""
        import osmnx as ox

        from backend.assess import osm_layers as ol

        monkeypatch.setattr(ol, "_outage_suspected_flag", True)
        monkeypatch.setattr(ol, "_probed", False)
        monkeypatch.setattr(ol, "_learn_dead_addresses", lambda host: None)
        monkeypatch.setattr("requests.post", lambda url, **k: _Resp())
        before = ox.settings.overpass_url
        try:
            ol._probe_overpass_once()
        finally:
            ox.settings.overpass_url = before
            ol._probed = False
        assert ol._outage_suspected() is False


class _Resp:
    status_code = 200

    def json(self):
        return {"elements": []}


class TestTheStepStopsItsOwnWorkers:
    """The step-level half of the same bug: `_fetch_pass` must hand its deadline
    to the worker threads, or abandoning a future changes nothing."""

    def test_each_layer_fetch_runs_under_the_step_deadline(self, monkeypatch):
        from backend import hcas
        from backend.assess import osm_layers as ol

        seen = {}

        def fake_fetch(bbox):
            seen["deadline"] = ol.current_deadline()
            seen["expired"] = ol._budget_expired()
            import geopandas as gpd
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

        fake_fetch.__name__ = "fetch_buildings"
        monkeypatch.setattr(hcas, "_layer_fetchers", lambda: {"buildings": fake_fetch})

        hcas._fetch_layers("p", 3, ["buildings"], (0, 0, 0.1, 0.1), budget_s=30)

        assert seen["deadline"] is not None, "worker ran with no deadline at all"
        assert seen["expired"] is False
        assert seen["deadline"] <= time.time() + 31

    def test_the_deadline_does_not_outlive_the_fetch(self):
        """Worker threads are pooled and reused. A deadline left set would make
        the NEXT step's fetch on that thread think its budget was already spent,
        so it has to be cleared even when the fetch raises."""
        from backend import hcas
        from backend.assess import osm_layers as ol

        ol.clear_deadline()
        hcas._bounded(lambda bbox: "ok", time.time() + 30)((0, 0, 1, 1))
        assert ol.current_deadline() is None

        def boom(bbox):
            raise OSError("timed out")

        with pytest.raises(OSError):
            hcas._bounded(boom, time.time() + 30)((0, 0, 1, 1))
        assert ol.current_deadline() is None, "a raising fetch left its deadline set"
