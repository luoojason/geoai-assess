"""Tests for the GraphRAG knowledgebase client (backend/kg.py). No network."""
from __future__ import annotations

import io
import json
from urllib.error import HTTPError, URLError

import pytest

from backend import kg


class _FakeResponse(io.BytesIO):
    """Minimal stand-in for the object urlopen yields as a context manager."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _json_response(payload):
    return _FakeResponse(json.dumps(payload).encode("utf-8"))


_OK_QUERY = {
    "ok": True,
    "dataset": "full",
    "version": "v2",
    "question": "Which factors link greenspace to health?",
    "method": "local",
    "answer_language": "en",
    "duration_seconds": 42,
    "answer": "Greenspace access lowers cardiovascular risk [Data: Sources (12)].",
    "source_documents": [
        {"title": "Urban green spaces and health"},
        {"title": "Urban green spaces and health"},  # duplicate: must collapse
        {"title": "Walkability and active living"},
    ],
    "resolved_source_count": 2,
}


@pytest.fixture(autouse=True)
def _enable_kg(monkeypatch):
    """Every test runs as though an operator configured a knowledgebase."""
    monkeypatch.setattr(kg.settings, "kg_enabled", True)
    monkeypatch.setattr(kg.settings, "kg_base_url", "http://127.0.0.1:8765")
    monkeypatch.setattr(kg.settings, "kg_version", "v2")
    monkeypatch.setattr(kg.settings, "kg_busy_backoff_s", 0.0)


# --- availability ---------------------------------------------------------


def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr(kg.settings, "kg_enabled", False)
    assert kg.enabled() is False
    assert kg.status() is None
    assert kg.query("a real question") is None


def test_available_true_when_ready(monkeypatch):
    monkeypatch.setattr(kg, "urlopen", lambda *a, **k: _json_response({"ready": True}))
    assert kg.available() is True


def test_available_false_when_not_ready(monkeypatch):
    monkeypatch.setattr(kg, "urlopen", lambda *a, **k: _json_response({"ready": False}))
    assert kg.available() is False


def test_status_survives_connection_refused(monkeypatch):
    def _refuse(*a, **k):
        raise URLError("connection refused")

    monkeypatch.setattr(kg, "urlopen", _refuse)
    assert kg.status() is None
    assert kg.available() is False


# --- argument validation --------------------------------------------------


@pytest.mark.parametrize("bad", ["", "ab"])
def test_question_too_short(bad):
    with pytest.raises(ValueError, match="characters"):
        kg.query(bad)


def test_question_too_long():
    with pytest.raises(ValueError, match="characters"):
        kg.query("x" * 2001)


def test_rejects_unknown_method():
    with pytest.raises(ValueError, match="method"):
        kg.query("a real question", method="hybrid")


def test_rejects_unknown_version():
    with pytest.raises(ValueError, match="version"):
        kg.query("a real question", version="v3")


def test_rejects_unknown_language():
    with pytest.raises(ValueError, match="answer_language"):
        kg.query("a real question", answer_language="fr")


# --- query ----------------------------------------------------------------


def test_query_parses_answer(monkeypatch):
    monkeypatch.setattr(kg, "urlopen", lambda *a, **k: _json_response(_OK_QUERY))
    answer = kg.query("Which factors link greenspace to health?")
    assert answer is not None
    assert answer.duration_seconds == 42
    assert answer.version == "v2"
    assert "cardiovascular" in answer.answer


def test_source_titles_dedupe_and_keep_order(monkeypatch):
    monkeypatch.setattr(kg, "urlopen", lambda *a, **k: _json_response(_OK_QUERY))
    answer = kg.query("Which factors link greenspace to health?")
    assert answer.source_titles == [
        "Urban green spaces and health",
        "Walkability and active living",
    ]


def test_query_sends_expected_payload(monkeypatch):
    seen = {}

    def _capture(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode("utf-8"))
        seen["timeout"] = timeout
        return _json_response(_OK_QUERY)

    monkeypatch.setattr(kg, "urlopen", _capture)
    kg.query("Which factors link greenspace to health?", method="global")

    assert seen["url"] == "http://127.0.0.1:8765/api/graphrag/query"
    assert seen["body"] == {
        "version": "v2",
        "method": "global",
        "answer_language": "en",
        "question": "Which factors link greenspace to health?",
    }
    assert seen["timeout"] == kg.settings.kg_query_timeout_s


def test_busy_429_is_retried_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def _busy_once(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise HTTPError("u", 429, "busy", {}, None)
        return _json_response(_OK_QUERY)

    monkeypatch.setattr(kg, "urlopen", _busy_once)
    assert kg.query("Which factors link greenspace to health?") is not None
    assert calls["n"] == 2


def test_busy_429_gives_up_after_retries(monkeypatch):
    calls = {"n": 0}

    def _always_busy(*a, **k):
        calls["n"] += 1
        raise HTTPError("u", 429, "busy", {}, None)

    monkeypatch.setattr(kg, "urlopen", _always_busy)
    assert kg.query("Which factors link greenspace to health?") is None
    assert calls["n"] == kg.settings.kg_busy_retries + 1


def test_503_not_ready_returns_none_without_retry(monkeypatch):
    calls = {"n": 0}

    def _not_ready(*a, **k):
        calls["n"] += 1
        raise HTTPError("u", 503, "not ready", {}, None)

    monkeypatch.setattr(kg, "urlopen", _not_ready)
    assert kg.query("Which factors link greenspace to health?") is None
    assert calls["n"] == 1


def test_answer_without_ok_flag_is_rejected(monkeypatch):
    monkeypatch.setattr(kg, "urlopen", lambda *a, **k: _json_response({"error": "boom"}))
    assert kg.query("Which factors link greenspace to health?") is None


# --- grounding helpers ----------------------------------------------------


def test_ground_framework_builds_bounded_question(monkeypatch):
    seen = {}

    def _capture(request, timeout=None):
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _json_response(_OK_QUERY)

    monkeypatch.setattr(kg, "urlopen", _capture)
    kg.ground_framework(factors=["greenspace access", "walkability"], place="Franklin County, OH")

    question = seen["body"]["question"]
    assert "greenspace access, walkability" in question
    assert "Franklin County, OH" in question
    assert len(question) <= 2000


def test_ground_framework_truncates_an_overlong_question(monkeypatch):
    seen = {}

    def _capture(request, timeout=None):
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _json_response(_OK_QUERY)

    monkeypatch.setattr(kg, "urlopen", _capture)
    kg.ground_framework(factors=["x" * 400] * 8)
    assert len(seen["body"]["question"]) <= 2000


def test_prompt_context_is_empty_when_absent():
    assert kg.as_prompt_context(None) == ""


def test_prompt_context_lists_sources(monkeypatch):
    monkeypatch.setattr(kg, "urlopen", lambda *a, **k: _json_response(_OK_QUERY))
    answer = kg.query("Which factors link greenspace to health?")
    context = kg.as_prompt_context(answer)
    assert "KNOWLEDGEBASE" in context
    assert "cardiovascular" in context
    assert "- Urban green spaces and health" in context
    assert context.count("Urban green spaces and health") == 1


def test_prompt_context_truncates(monkeypatch):
    long_answer = dict(_OK_QUERY, answer="y" * 9000)
    monkeypatch.setattr(kg, "urlopen", lambda *a, **k: _json_response(long_answer))
    answer = kg.query("Which factors link greenspace to health?")
    context = kg.as_prompt_context(answer, max_chars=100)
    assert "[truncated]" in context
    assert len(context) < 500
