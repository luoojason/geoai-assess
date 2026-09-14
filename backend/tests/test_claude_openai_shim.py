"""Tests for tools/claude_openai_shim.py. No CLI is ever run."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "tools" / "claude_openai_shim.py"
_spec = importlib.util.spec_from_file_location("claude_openai_shim", _PATH)
shim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shim)


def _completed(rc, out=b"", err=b""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(shim.time, "sleep", lambda s: None)


def test_transient_exit_is_retried(monkeypatch):
    # 2026-09-13: three concurrent calls exited 1 mid-query, GraphRAG's own fast
    # retries all landed in the same window, and step 4 silently lost grounding.
    results = iter([_completed(1), _completed(0, b"ok")])
    monkeypatch.setattr(shim.subprocess, "run", lambda *a, **k: next(results))
    assert shim.call_claude("hi") == "ok"


def test_gives_up_after_retries_and_reports_stdout(monkeypatch):
    calls = {"n": 0}

    def _fail(*a, **k):
        calls["n"] += 1
        return _completed(1, b"API Error: 401 token expired")

    monkeypatch.setattr(shim.subprocess, "run", _fail)
    with pytest.raises(RuntimeError, match="token expired"):
        shim.call_claude("hi")
    assert calls["n"] == shim.CALL_RETRIES + 1
