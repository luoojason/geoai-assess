"""Suite-wide isolation for the process-wide state in `osm_layers`.

That module deliberately keeps global state — which endpoint the probe settled
on, which endpoints serve stale data, which addresses will not accept a
connection. That is right for a running app and poison for a test suite: on
2026-08-11 two rotation tests passed or failed depending on whether
overpass.private.coffee happened to answer a live probe in an *earlier test
file*, and a clean install failed twice then passed on rerun with no code
change. A green rerun would have been mistaken for evidence.

`test_hcas.py` already resets the freshness gate for its own tests. The
address-health state added the same day has exactly the same shape, so it is
reset here for every test in the suite rather than waiting to be caught by the
same class of bug a second time.

The retry backoff is zeroed for the same reason a unit test never sleeps: the
real pause is production behaviour, and leaving it in place added twelve
minutes to a ninety-second suite. The clamping logic itself is covered by
`test_retrying_never_sleeps_past_the_budget`, which restores a real pause.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_osm_layer_globals(monkeypatch):
    from backend.assess import osm_layers as ol

    monkeypatch.setattr(ol, "_dead_addresses", set())
    monkeypatch.setattr(ol, "_addr_checked", {})
    monkeypatch.setattr(ol, "_outage_suspected_flag", False)
    # No test may probe the real Overpass servers by accident. Since 2026-09-16
    # the probe's verdict expires, so leaving it unset made every test that
    # reached a fetcher re-probe five live endpoints (up to 12 s each) and more
    # than tripled the suite's run time. A test that wants a probe sets
    # `_probed` False itself.
    monkeypatch.setattr(ol, "_probed", True)
    monkeypatch.setattr(ol, "_probe_expires_at", float("inf"))
    monkeypatch.setattr(ol, "_retry_pause", lambda attempt: 0.0)
    ol.clear_deadline()
    try:
        yield
    finally:
        ol.clear_deadline()


# hcas.LAYER_CACHE_DIR defaults to a directory beside hcas.py — right for a
# laptop install, wrong for a test suite: every step-3/7 run that fetches a
# layer writes it there, so a full suite run left 80+ pid-named directories in
# the repo (found 2026-08-14 while adding the cache feature — DB_PATH already
# gets this same tmp_path treatment per-test, LAYER_CACHE_DIR needs the same).
@pytest.fixture(autouse=True)
def _isolate_layer_cache(tmp_path_factory, monkeypatch):
    from backend import hcas

    monkeypatch.setattr(hcas, "LAYER_CACHE_DIR", tmp_path_factory.mktemp("hcas_layer_cache"))
    # Confirming step 1 starts a background download of every layer. A test
    # suite must not have threads fetching behind its back; the prefetch tests
    # switch it on for themselves.
    monkeypatch.setattr(hcas, "PREFETCH_ENABLED", False)
