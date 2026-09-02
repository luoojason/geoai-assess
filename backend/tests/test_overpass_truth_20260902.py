"""Two ways the app told the user something false — measured 2026-09-01.

A full gated 1->8 run on Adams County OH (shipped `cfbb151` build) confirmed 8/8
steps and fetched ZERO features. Two of the reasons were the app's own:

  * `overpass-api.de` publishes two A records and one of them was black-holing
    connections. `osm_layers` already knows how to drop such an address, but it
    only learns during ENDPOINT SELECTION. `_with_overpass_failover` rotates to
    another endpoint and retries immediately, so for the following 900 s of
    retries every rotation played the coin-flip again and never re-learned. The
    resulting connect timeout surfaced to the user as "DNS lookup failed - check
    the system DNS settings (a leftover VPN/proxy DNS is a common cause)", which
    is what sent Prof. Shi chasing his own DNS on 2026-07-20.

  * A layer whose worker thread started at or after the step's deadline did no
    work at all and was still reported as "gave up after 0s - the OpenStreetMap
    server did not answer". Zero seconds elapsed is proof the server was never
    asked. Step 7 describes this same situation correctly ("not attempted");
    step 3's inventory did not, so one run produced two contradictory artifacts
    and the one the user reads first was the false one.

Neither test touches the network.
"""
import time

import pytest


class TestRotationLearnsDeadAddresses:
    """Rotating to a host is exactly when its dead addresses must be dropped —
    the retry is about to spend a full connect timeout on whatever DNS hands
    back."""

    def test_rotation_learns_dead_addresses_for_the_host_it_rotates_to(self, monkeypatch):
        from backend.assess import osm_layers as ol

        learned = []
        monkeypatch.setattr(ol, "_learn_dead_addresses", lambda host: learned.append(host))
        monkeypatch.setattr(ol, "_budget_expired", lambda: False)
        monkeypatch.setattr(ol, "_outage_suspected", lambda: False)
        monkeypatch.setattr(ol.ox.settings, "overpass_url", ol._OVERPASS_PRIMARY)

        calls = {"n": 0}

        def call():
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("connection timed out")
            return "layer"

        assert ol._with_overpass_failover(call) == "layer"

        rotated_to = ol._next_endpoint(ol._OVERPASS_PRIMARY)
        from urllib.parse import urlsplit

        assert urlsplit(rotated_to).hostname in learned, (
            "rotating to an endpoint must drop its unusable addresses BEFORE the "
            f"retry spends a connect timeout on one; learned={learned}"
        )

    def test_the_endpoint_that_just_failed_is_re_probed(self, monkeypatch):
        """The host we rotate AWAY from is the one with evidence against it.

        Measured 2026-09-02: a live run rotated off overpass-api.de on a
        ConnectTimeout roughly every other request for twenty minutes and never
        re-probed it, because the rotation only ever learned about the endpoint
        it was moving TO. The dead address belonged to the host that had just
        failed, so the verdict that would have fixed the run was never taken.
        """
        from backend.assess import osm_layers as ol

        learned = []
        monkeypatch.setattr(ol, "_learn_dead_addresses", lambda host: learned.append(host))
        monkeypatch.setattr(ol, "_budget_expired", lambda: False)
        monkeypatch.setattr(ol, "_outage_suspected", lambda: False)
        monkeypatch.setattr(ol.ox.settings, "overpass_url", ol._OVERPASS_PRIMARY)

        calls = {"n": 0}

        def call():
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("connection timed out")
            return "layer"

        ol._with_overpass_failover(call)

        from urllib.parse import urlsplit

        failed_host = urlsplit(ol._OVERPASS_PRIMARY).hostname
        assert failed_host in learned, (
            "the endpoint that just timed out must be re-probed — its dead "
            f"address is why it failed; learned={learned}"
        )

    def test_the_endpoint_left_for_the_next_call_is_learned_too(self, monkeypatch):
        """Both retries are exhausted and the endpoint moves on for the NEXT
        call. That handover has the same problem as the first rotation."""
        from backend.assess import osm_layers as ol

        learned = []
        monkeypatch.setattr(ol, "_learn_dead_addresses", lambda host: learned.append(host))
        monkeypatch.setattr(ol, "_budget_expired", lambda: False)
        monkeypatch.setattr(ol, "_outage_suspected", lambda: False)
        monkeypatch.setattr(ol.ox.settings, "overpass_url", ol._OVERPASS_PRIMARY)

        def always_fails():
            raise OSError("connection timed out")

        with pytest.raises(OSError):
            ol._with_overpass_failover(always_fails)

        from urllib.parse import urlsplit

        nxt = ol._next_endpoint(ol._OVERPASS_PRIMARY)
        after = ol._next_endpoint(nxt)
        assert urlsplit(after).hostname in learned, (
            "the endpoint handed to the next call must have its dead addresses "
            f"dropped as well; learned={learned}"
        )


class TestZeroElapsedIsNotAServerFailure:
    """`began` is set when a worker thread picks the layer up, not when a
    request goes out. A worker that starts at the deadline does nothing."""

    def test_a_layer_that_never_started_is_not_attempted(self):
        from backend import hcas

        note = hcas._layer_failure_note(began=None, deadline=100.0, now=100.0)
        assert "not attempted" in note
        assert "did not answer" not in note

    def test_a_layer_that_started_after_the_deadline_is_not_blamed_on_the_server(self):
        from backend import hcas

        # Worker picked it up as the budget ran out: zero seconds of fetching.
        note = hcas._layer_failure_note(began=100.0, deadline=100.0, now=100.0)
        assert "not attempted" in note, (
            "a layer that fetched for 0s was never asked of Overpass; blaming the "
            f"server there is the Delaware County falsehood. got: {note!r}"
        )
        assert "did not answer" not in note

    def test_a_layer_that_really_waited_still_reports_the_server_timeout(self):
        from backend import hcas

        note = hcas._layer_failure_note(began=100.0, deadline=1000.0, now=1000.0)
        assert "gave up after 900s" in note
        assert "did not answer" in note
        assert "not attempted" not in note
