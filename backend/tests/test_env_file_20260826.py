"""The .env file must reach the code that reads it — 2026-08-26.

Two defects, both in the path a field tester actually walks:

1. `Settings` forbade extra inputs, so ANY name in .env that Settings does not
   model raised a ValidationError at import time — the app refused to start.
   The app's own step-1 message tells the user to add `CENSUS_API_KEY=...` to
   .env, so following the instruction bricked the install. Same failure class
   as the 2026-07-25 Python-3.9 import crash: dead before the server binds.

2. Nothing exported .env into os.environ. `Settings` reads it, but every other
   documented knob (CENSUS_API_KEY, HCAS_DB, HCAS_LAYER_BUDGET_S, the OSM_*
   timeouts) is read with os.environ.get, so a value set in .env was silently
   ignored and ACS stayed "unavailable" forever with the same message telling
   the user to do the thing they had already done.
"""
from __future__ import annotations

import os
from pathlib import Path

from backend import config


def _write_env(tmp_path: Path, body: str) -> Path:
    p = tmp_path / ".env"
    p.write_text(body, encoding="utf-8")
    return p


class TestSettingsToleratesUnmodelledKeys:
    def test_unmodelled_key_does_not_crash_settings(self, tmp_path):
        env = _write_env(tmp_path, "CENSUS_API_KEY=abc123\nHCAS_DB=/data/hcas.db\n")
        s = config.Settings(_env_file=str(env))
        assert s.demo_password is None  # constructed at all == the fix

    def test_modelled_keys_still_load(self, tmp_path):
        env = _write_env(tmp_path, "DEMO_PASSWORD=hunter2\nCENSUS_API_KEY=abc123\n")
        s = config.Settings(_env_file=str(env))
        assert s.demo_password == "hunter2"


class TestLoadEnvFile:
    def test_exports_unmodelled_key_to_os_environ(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CENSUS_API_KEY", raising=False)
        env = _write_env(tmp_path, "CENSUS_API_KEY=abc123\n")
        config.load_env_file(env)
        assert os.environ.get("CENSUS_API_KEY") == "abc123"

    def test_real_environment_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CENSUS_API_KEY", "from-the-shell")
        env = _write_env(tmp_path, "CENSUS_API_KEY=from-the-file\n")
        config.load_env_file(env)
        assert os.environ["CENSUS_API_KEY"] == "from-the-shell"

    def test_missing_file_is_not_an_error(self, tmp_path):
        config.load_env_file(tmp_path / "nope.env")

    def test_blank_value_does_not_shadow_a_real_one(self, tmp_path, monkeypatch):
        """START-HCAS.bat's old bug wrote an empty key line; an empty value in
        .env must not overwrite a good value already in the environment."""
        monkeypatch.setenv("CENSUS_API_KEY", "good")
        env = _write_env(tmp_path, "CENSUS_API_KEY=\n")
        config.load_env_file(env)
        assert os.environ["CENSUS_API_KEY"] == "good"


class TestEnvExampleDocumentsWhatTheAppAsksFor:
    def test_census_api_key_is_documented(self):
        example = (Path(__file__).resolve().parents[2] / ".env.example").read_text(
            encoding="utf-8"
        )
        assert "CENSUS_API_KEY" in example
