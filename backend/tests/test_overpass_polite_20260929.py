"""The polite Overpass client (2026-09-29).

On Railway one step-3 run made every overpass-api.de backend refuse TCP from the
app's IP for 10+ minutes: no status check, instant retries, no shared in-flight
cap. These tests pin the fix. All HTTP is mocked; nothing here touches a server.
"""
import threading
import time

import osmnx as ox
import pytest
import requests

from backend.assess import osm_layers as ol
from backend.assess.netdiag import friendly_net_reason

_REAL_RETRY_PAUSE = ol._retry_pause     # conftest zeroes it per test

OK_STATUS = """Connected as: 123456789
Current time: 2026-09-29T18:00:00Z
Announced endpoint: none
Rate limit: 4
4 slots available now.
Currently running queries (pid, space limit, time limit, start time):
"""

BUSY_STATUS = """Connected as: 123456789
Current time: 2026-09-29T18:00:00Z
Announced endpoint: none
Rate limit: 2
Slot available after: 2026-09-29T18:00:03Z, in 3 seconds.
Slot available after: 2026-09-29T18:00:20Z, in 20 seconds.
Currently running queries (pid, space limit, time limit, start time):
"""

MIXED_STATUS = """Rate limit: 4
1 slots available now.
Slot available after: 2026-09-29T18:00:09Z, in 9 seconds.
"""

KUMI_STATUS = """Connected as: 3261744638
Current time: 2026-09-29T18:00:00Z
Rate limit: 0
Currently running queries (pid, space limit, time limit, start time):
812\t536870912\t180\t2026-09-29T17:59:58Z
"""

Z = ol._OVERPASS_Z
LZ4 = ol._OVERPASS_LZ4
KUMI = ol._OVERPASS_MIRROR


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.reason = "OK" if status == 200 else "Too Many Requests"
        self.ok = status < 400
        self._body = body if body is not None else {"elements": []}
        self.url = "https://z.overpass-api.de/api/interpreter"
        self.content = b"{}"
        self.text = "{}"

    def json(self):
        return self._body


@pytest.fixture
def polite(monkeypatch):
    """No osmnx disk cache, no real sleeping, a recorder for posts and sleeps."""
    monkeypatch.setattr(ox.settings, "use_cache", False)
    monkeypatch.setattr(ox.settings, "overpass_url", Z)
    rec = {"posts": [], "sleeps": [], "status": []}
    monkeypatch.setattr(ol.time, "sleep", lambda s: rec["sleeps"].append(s))

    def post(url, **kw):
        rec["posts"].append(url)
        return _Resp()

    monkeypatch.setattr(requests, "post", post)
    return rec


def _status_seq(monkeypatch, rec, *bodies):
    seq = list(bodies)

    def fetch(url):
        rec["status"].append(url)
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(ol, "_fetch_status_text", fetch)


# --- parsing -----------------------------------------------------------------

def test_status_parse_free_slots():
    assert ol.parse_overpass_status(OK_STATUS) == {
        "rate_limit": 4, "available": 4, "wait_s": None}


def test_status_parse_busy_takes_soonest_slot():
    assert ol.parse_overpass_status(BUSY_STATUS) == {
        "rate_limit": 2, "available": 0, "wait_s": 3.0}


def test_status_parse_mixed_counts_free_slots():
    st = ol.parse_overpass_status(MIXED_STATUS)
    assert st["available"] == 1 and st["wait_s"] == 9.0


@pytest.mark.parametrize("body", [None, "", "<html>502 Bad Gateway</html>",
                                  "garbage\n\n\nnot a status", KUMI_STATUS])
def test_status_parse_rejects_other_formats(body):
    assert ol.parse_overpass_status(body) is None


# --- slot check --------------------------------------------------------------

def test_waits_when_server_says_so_then_sends(monkeypatch, polite):
    _status_seq(monkeypatch, polite, BUSY_STATUS, OK_STATUS)
    ol._polite_overpass_request({"data": "q"})
    assert polite["sleeps"] == [4.0]          # 3 s asked for, plus a margin
    assert len(polite["posts"]) == 1
    assert ol.polite_stats()["waits"] == 1


def test_wait_longer_than_budget_rests_host_and_rotates(monkeypatch, polite):
    _status_seq(monkeypatch, polite, BUSY_STATUS)
    ol.set_deadline(time.time() + 6)          # 4 s wait + 5 s margin does not fit
    with pytest.raises(ol.OverpassBusy) as ei:
        ol._polite_overpass_request({"data": "q"})
    assert polite["posts"] == [] and polite["sleeps"] == []
    assert ol._host_blocked("z.overpass-api.de")
    assert ol._is_refusal(ei.value)
    assert ol._next_endpoint(ol._OVERPASS_PRIMARY) == LZ4    # z. is skipped


def test_status_failure_falls_back_to_sending(monkeypatch, polite):
    _status_seq(monkeypatch, polite, None)
    ol._polite_overpass_request({"data": "q"})
    assert len(polite["posts"]) == 1 and polite["sleeps"] == []


def test_kumi_is_never_asked_for_status(monkeypatch, polite):
    _status_seq(monkeypatch, polite, BUSY_STATUS)
    monkeypatch.setattr(ox.settings, "overpass_url", KUMI)
    ol._polite_overpass_request({"data": "q"})
    assert polite["status"] == [] and len(polite["posts"]) == 1


def test_status_is_cached_between_workers(monkeypatch, polite):
    _status_seq(monkeypatch, polite, OK_STATUS)
    for _ in range(3):
        ol._polite_overpass_request({"data": "q"})
    assert len(polite["status"]) == 1 and len(polite["posts"]) == 3


def test_cached_free_slots_are_consumed_not_reused(monkeypatch, polite):
    _status_seq(monkeypatch, polite, MIXED_STATUS.replace("1 slots", "2 slots"))
    for _ in range(3):
        ol._polite_overpass_request({"data": "q"})
    assert len(polite["status"]) == 2         # third caller had to ask again


def test_429_is_raised_as_refusal_not_slept_on(monkeypatch, polite):
    monkeypatch.setattr(requests, "post", lambda url, **kw: _Resp(429))
    with pytest.raises(ol.ResponseStatusCodeError) as ei:
        ol._polite_overpass_request({"data": "q"})
    assert ol._is_refusal(ei.value)
    assert polite["sleeps"] == []             # osmnx would have slept 55 s
    assert "slow down" in friendly_net_reason(ei.value)


# --- backoff ----------------------------------------------------------------

def test_backoff_schedule_grows_and_caps(monkeypatch):
    monkeypatch.setattr("random.uniform", lambda a, b: 1.0)
    assert [_REAL_RETRY_PAUSE(a) for a in (1, 2, 3, 4, 9)] == [5.0, 15.0, 40.0, 60.0, 60.0]


def test_backoff_has_jitter_within_bounds():
    for a, base in ((1, 5.0), (2, 15.0), (3, 40.0)):
        p = _REAL_RETRY_PAUSE(a)
        assert 0.75 * base <= p <= 1.25 * base


def test_tile_loop_backs_off_between_refusals(monkeypatch, polite):
    monkeypatch.setattr(ol, "_retry_pause", _REAL_RETRY_PAUSE)
    monkeypatch.setattr("random.uniform", lambda a, b: 1.0)
    calls = []

    def refused(b):
        calls.append(b)
        raise requests.exceptions.ConnectTimeout("refused")

    with pytest.raises(requests.exceptions.ConnectTimeout):
        ol._tiled((0, 0, 0.1, 0.1), refused)
    assert len(calls) == 1 + ol.OSM_TILE_RETRIES + ol.OSM_REFUSAL_RETRIES
    assert polite["sleeps"] == [5.0, 15.0, 40.0, 60.0]


def test_backoff_never_sleeps_past_budget(monkeypatch, polite):
    monkeypatch.setattr(ol, "_retry_pause", _REAL_RETRY_PAUSE)
    ol.set_deadline(time.time() + 2)
    ol._pause_for_retry(3)
    assert polite["sleeps"] and polite["sleeps"][0] <= 2.0


# --- refused-host cooldown ---------------------------------------------------

def test_two_fast_refusals_rest_the_host(monkeypatch, polite):
    def refuse(url, **kw):
        polite["posts"].append(url)
        raise requests.exceptions.ConnectionError("[WinError 10061] refused")

    monkeypatch.setattr(requests, "post", refuse)
    for _ in range(2):
        with pytest.raises(requests.exceptions.ConnectionError):
            ol._polite_overpass_request({"data": "q"})
    assert ol._host_blocked("z.overpass-api.de")
    with pytest.raises(ol.OverpassBusy) as ei:
        ol._polite_overpass_request({"data": "q"})
    assert len(polite["posts"]) == 2          # the third never went on the wire
    assert "refusing connections" in friendly_net_reason(ei.value)
    assert ol._next_endpoint(ol._OVERPASS_PRIMARY) == LZ4


def test_one_refusal_then_success_resets_the_count(monkeypatch, polite):
    answers = [requests.exceptions.ConnectionError("refused"), _Resp(),
               requests.exceptions.ConnectionError("refused")]

    def post(url, **kw):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(requests, "post", post)
    for _ in range(3):
        try:
            ol._polite_overpass_request({"data": "q"})
        except requests.exceptions.ConnectionError:
            pass
    assert not ol._host_blocked("z.overpass-api.de")


def test_read_timeouts_are_not_counted_as_refusals(monkeypatch, polite):
    def slow(url, **kw):
        raise requests.exceptions.ReadTimeout("read timed out")

    monkeypatch.setattr(requests, "post", slow)
    for _ in range(3):
        with pytest.raises(requests.exceptions.ReadTimeout):
            ol._polite_overpass_request({"data": "q"})
    assert not ol._host_blocked("z.overpass-api.de")


def test_two_handshake_timeouts_rest_the_host(monkeypatch, polite):
    # What the 2026-09-29 live run actually saw: the IP was refused by a TCP
    # handshake that never completed, not by a reset.
    def dropped(url, **kw):
        raise requests.exceptions.ConnectTimeout("connect timeout=10")

    monkeypatch.setattr(requests, "post", dropped)
    for _ in range(2):
        with pytest.raises(requests.exceptions.ConnectTimeout):
            ol._polite_overpass_request({"data": "q"})
    assert ol._host_blocked("z.overpass-api.de")


def test_rotation_does_not_probe_a_rested_host(monkeypatch, polite):
    learned = []
    monkeypatch.setattr(ol, "_learn_dead_addresses", lambda h: learned.append(h))

    def call():
        raise ol.OverpassBusy(ol._RESTING_HOSTS)

    with pytest.raises(ol.OverpassBusy):
        ol._with_overpass_failover(call)
    assert learned == []


# --- concurrency cap ---------------------------------------------------------

def _run_concurrently(monkeypatch, urls):
    """Max requests on the wire at once when len(urls) workers send together."""
    gate = threading.Event()
    lock = threading.Lock()
    live = {"now": 0, "max": 0}

    def post(url, **kw):
        with lock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        gate.wait(0.3)
        with lock:
            live["now"] -= 1
        return _Resp()

    monkeypatch.setattr(requests, "post", post)

    def worker(u):
        ol._polite_overpass_request({"data": u})

    monkeypatch.setattr(ox.settings, "overpass_url", urls[0])
    threads = [threading.Thread(target=worker, args=(u,)) for u in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    return live["max"]


def test_family_in_flight_never_exceeds_cap(monkeypatch, polite):
    # Four workers — say two layers for the project area and two for the base
    # area — all aimed at the overpass-api.de family.
    monkeypatch.setattr(ol, "OSM_OVERPASS_MAX_INFLIGHT", 2)
    assert _run_concurrently(monkeypatch, [Z] * 6) <= 2


def test_cap_shrinks_to_the_servers_reported_rate_limit(monkeypatch, polite):
    monkeypatch.setattr(ol, "OSM_OVERPASS_MAX_INFLIGHT", 4)
    ol._rate_limits[Z] = 1
    assert _run_concurrently(monkeypatch, [Z] * 4) == 1


def test_the_whole_family_shares_one_cap():
    assert {ol._gate_key(h) for h in ol._STATUS_HOSTS} == {ol._FAMILY_KEY}
    assert ol._gate_key("overpass.kumi.systems") != ol._FAMILY_KEY


def test_own_cap_full_for_the_whole_budget_is_reported_plainly(monkeypatch, polite):
    monkeypatch.setattr(ol, "OSM_OVERPASS_MAX_INFLIGHT", 1)
    ol._inflight[ol._FAMILY_KEY] = 1           # someone else holds the only place
    ol.set_deadline(time.time() + 0.2)
    with pytest.raises(ol.OverpassBusy) as ei:
        ol._polite_overpass_request({"data": "q"})
    assert polite["posts"] == []
    assert "fair-use share" in friendly_net_reason(ei.value)


def test_osmnx_sends_through_the_polite_client():
    from osmnx import _overpass
    assert _overpass._overpass_request is ol._polite_overpass_request
