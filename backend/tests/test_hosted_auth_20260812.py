"""The auth surface a hosted deploy actually presents, measured 2026-08-12.

Found while putting HCAS behind a tunnel on the Oracle box: with `DEMO_PASSWORD`
set, every JSON route correctly answered 401, but `GET /hcas` — the wizard page
itself — answered 200. That combination is worse than either extreme. A browser
prompts for Basic credentials only on a 401 it receives while *navigating*, never
on one it receives from `fetch()`, so the visitor got a fully rendered wizard in
which every button failed 401 with nowhere to type the password. DEPLOY.md had
been claiming `/hcas` should 401 since the hosted path was written.

These tests pin the rule the hosted path depends on: with a password set, the
routes a person reaches by typing a URL must 401 so the browser can ask; without
one, nothing is gated and the local/personal path is unchanged.

No network.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app import app
from backend.config import settings

# The wizard page and the two read-only pages opened as plain browser links. All
# reached by navigation, so all must be able to trigger the credential prompt.
NAVIGABLE = ["/hcas", "/hcas/audit.csv"]

# fetch()-reached JSON routes: 401 is enough, the page holds the credentials.
FETCHED = ["/hcas/projects", "/hcas/areal-units"]


@pytest.fixture()
def password(monkeypatch):
    monkeypatch.setattr(settings, "demo_password", "s3kr1t")
    return "s3kr1t"


class TestPasswordSet:
    def test_navigable_routes_challenge_the_browser(self, password):
        client = TestClient(app)
        for path in NAVIGABLE:
            r = client.get(path)
            assert r.status_code == 401, f"{path} is open — a hosted deploy leaks it"
            # Without this header the browser shows an error page instead of a
            # login box, which is the whole failure this module exists for.
            assert r.headers.get("www-authenticate", "").lower().startswith("basic"), (
                f"{path} 401s but does not ask, so there is nowhere to type the password"
            )

    def test_fetched_routes_are_gated(self, password):
        client = TestClient(app)
        for path in FETCHED:
            r = client.get(path, headers={"X-Requested-With": "geoai"})
            assert r.status_code == 401, f"{path} is open"

    def test_the_password_actually_opens_the_page(self, password):
        client = TestClient(app)
        r = client.get("/hcas", auth=("", password))
        assert r.status_code == 200
        assert "<html" in r.text.lower()

    def test_healthz_stays_open_for_the_platform_probe(self, password):
        # A health check cannot send credentials; gating it makes the platform
        # declare a healthy app dead.
        r = TestClient(app).get("/healthz")
        assert r.status_code == 200


class TestNoPassword:
    def test_nothing_is_gated_by_default(self):
        # The personal/local path sets no password and must stay frictionless.
        assert settings.demo_password is None
        client = TestClient(app)
        assert client.get("/hcas").status_code == 200
        assert client.get(
            "/hcas/areal-units", headers={"X-Requested-With": "geoai"}
        ).status_code == 200
