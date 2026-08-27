"""Client for Prof. Wang's GraphRAG knowledgebase runtime.

The knowledgebase ships as a standalone package (`graphrag-english-runtime-*`)
that serves a JSON HTTP API on loopback. This module is the only thing in HCAS
that talks to it, so the rest of the pipeline never imports GraphRAG or shares
its virtual environment.

Three facts from the vendor's guide shape everything here:

* **One query at a time.** The server refuses a concurrent query with HTTP 429.
  We hold a process-wide lock so HCAS never collides with itself, and retry a
  429 (someone querying the browser UI) with backoff.
* **Queries are slow.** Local search takes minutes; global search longer; the
  server kills anything past 15 minutes. The vendor's own example uses a 920 s
  client timeout, so that is the default here.
* **It is optional.** It binds to 127.0.0.1 on the machine that hosts it, so a
  field tester running HCAS on their laptop usually has no knowledgebase at all.
  Every entry point degrades to `None` rather than raising, and callers fall
  back to the built-in theory list.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.config import settings

log = logging.getLogger(__name__)

QUERY_METHODS = ("local", "global")
ANSWER_LANGUAGES = ("en", "zh", "auto")
VERSIONS = ("v1", "v2")

# The vendor validates these server-side; checking here turns a 400 round-trip
# into an immediate, more legible error.
_MIN_QUESTION_CHARS = 3
_MAX_QUESTION_CHARS = 2000

# Only one GraphRAG query runs at a time server-side. Serialising locally means
# a busy 429 signals *another client* (the browser UI), not HCAS racing itself.
_QUERY_LOCK = threading.Lock()


class KGUnavailable(RuntimeError):
    """The knowledgebase is not configured, not running, or not ready."""


@dataclass
class KGAnswer:
    """One GraphRAG answer plus the provenance needed to ground a citation."""

    question: str
    answer: str
    method: str
    version: str
    answer_language: str
    duration_seconds: int
    source_documents: List[Dict[str, Any]] = field(default_factory=list)
    resolved_source_count: int = 0

    @property
    def source_titles(self) -> List[str]:
        """Citable titles, de-duplicated, order preserved.

        Step 4 enum-locks its `references` to a fixed list. Feeding it these
        titles keeps the enum lock intact while sourcing it from the real
        corpus instead of a hardcoded table.
        """
        titles: List[str] = []
        for doc in self.source_documents:
            if not isinstance(doc, dict):
                continue
            title = doc.get("title") or doc.get("document") or doc.get("id")
            if title and title not in titles:
                titles.append(str(title))
        return titles


def _endpoint(path: str) -> str:
    return settings.kg_base_url.rstrip("/") + path


def _get_json(path: str, timeout: float) -> Any:
    with urlopen(_endpoint(path), timeout=timeout) as response:  # noqa: S310 - loopback only
        return json.load(response)


def _post_json(path: str, payload: Dict[str, Any], timeout: float) -> Any:
    request = Request(  # noqa: S310 - loopback only
        _endpoint(path),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def enabled() -> bool:
    """Whether the operator has pointed HCAS at a knowledgebase at all."""
    return bool(settings.kg_enabled and settings.kg_base_url)


def status(version: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Readiness probe. Returns ``None`` when the knowledgebase is unreachable.

    Never raises: an absent knowledgebase is the normal case for a field tester
    running HCAS on a laptop, not an error condition.
    """
    if not enabled():
        return None
    version = version or settings.kg_version
    try:
        return _get_json(f"/api/graphrag/status?version={version}", settings.kg_status_timeout_s)
    except (HTTPError, URLError, OSError, ValueError) as exc:
        log.info("knowledgebase status probe failed: %s", exc)
        return None


def available(version: Optional[str] = None) -> bool:
    """True only when the knowledgebase answered and reported itself ready."""
    probe = status(version)
    return bool(probe and probe.get("ready"))


def query(
    question: str,
    method: str = "local",
    version: Optional[str] = None,
    answer_language: str = "en",
    timeout: Optional[float] = None,
) -> Optional[KGAnswer]:
    """Ask the knowledgebase one question.

    Returns ``None`` — rather than raising — whenever the knowledgebase cannot
    answer, so a step that wants grounding can fall back without a try/except at
    every call site. Bad arguments still raise ``ValueError``, because those are
    programming errors rather than environment conditions.
    """
    question = (question or "").strip()
    if not _MIN_QUESTION_CHARS <= len(question) <= _MAX_QUESTION_CHARS:
        raise ValueError(
            f"question must be {_MIN_QUESTION_CHARS}-{_MAX_QUESTION_CHARS} characters, got {len(question)}"
        )
    if method not in QUERY_METHODS:
        raise ValueError(f"method must be one of {QUERY_METHODS}, got {method!r}")
    if answer_language not in ANSWER_LANGUAGES:
        raise ValueError(f"answer_language must be one of {ANSWER_LANGUAGES}, got {answer_language!r}")
    version = version or settings.kg_version
    if version not in VERSIONS:
        raise ValueError(f"version must be one of {VERSIONS}, got {version!r}")

    if not enabled():
        return None

    payload = {
        "version": version,
        "method": method,
        "answer_language": answer_language,
        "question": question,
    }
    timeout = timeout if timeout is not None else settings.kg_query_timeout_s

    # Serialise our own calls; the server would 429 them anyway.
    with _QUERY_LOCK:
        for attempt in range(settings.kg_busy_retries + 1):
            try:
                raw = _post_json("/api/graphrag/query", payload, timeout)
            except HTTPError as exc:
                if exc.code == 429 and attempt < settings.kg_busy_retries:
                    # Someone else (most likely the browser UI) holds the runtime.
                    time.sleep(settings.kg_busy_backoff_s * (attempt + 1))
                    continue
                log.warning("knowledgebase query failed (HTTP %s): %s", exc.code, exc)
                return None
            except (URLError, OSError, ValueError) as exc:
                log.warning("knowledgebase query failed: %s", exc)
                return None

            if not isinstance(raw, dict) or not raw.get("ok"):
                log.warning("knowledgebase returned no answer: %s", raw)
                return None

            return KGAnswer(
                question=raw.get("question", question),
                answer=raw.get("answer", ""),
                method=raw.get("method", method),
                version=raw.get("version", version),
                answer_language=raw.get("answer_language", answer_language),
                duration_seconds=int(raw.get("duration_seconds") or 0),
                source_documents=list(raw.get("source_documents") or []),
                resolved_source_count=int(raw.get("resolved_source_count") or 0),
            )

    return None


def ground_framework(
    factors: Optional[List[str]] = None,
    place: str = "",
    method: str = "global",
    version: Optional[str] = None,
) -> Optional[KGAnswer]:
    """Retrieve theory grounding for the step-4 solution framework.

    Step 4 enum-locks both the theory it may cite and the reference titles it may
    quote. That lock is the experimental control the grounding study depends on,
    so it must survive the swap from the hardcoded theory table to the live
    knowledgebase. It does: this returns real corpus answers, and
    :attr:`KGAnswer.source_titles` supplies the enum the schema locks against.

    Defaults to ``global`` search, against the vendor's own ``local`` default,
    for two measured reasons:

    * Step 4 asks a corpus-level synthesis question ("which theories link these
      factors to health outcomes"), which is what global search is built for.
      Local search is for focused lookups of a named entity or relationship.
    * Global search never embeds the query, so it needs no DashScope key.
      Verified 2026-08-27: a global query returned a full cited answer with a
      deliberately invalid embedding key, while the same key made local search
      fail with a 401 from DashScope. GraphRAG still validates the embedding
      block at config load, so the key must be present — but it can be a
      placeholder.

    Returns ``None`` when the knowledgebase is absent, and the caller keeps its
    built-in theory list.
    """
    factors = [f for f in (factors or []) if f]
    focus = ", ".join(factors[:8]) if factors else "the built environment"
    where = f" in {place}" if place else ""
    question = (
        f"Which theories and empirical findings link {focus}{where} to community health "
        "outcomes, and what assessment methodology do they imply?"
    )
    # The question is bounded by the factor list, but a caller can pass anything.
    if len(question) > _MAX_QUESTION_CHARS:
        question = question[: _MAX_QUESTION_CHARS - 1].rstrip() + "?"
    return query(question, method=method, version=version)


def as_prompt_context(answer: Optional[KGAnswer], max_chars: int = 6000) -> str:
    """Render an answer as a prompt block, or "" when there is nothing to add.

    Returning "" for the absent case lets a prompt be assembled by concatenation
    without the caller branching on knowledgebase availability.
    """
    if answer is None or not answer.answer.strip():
        return ""
    body = answer.answer.strip()
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n[truncated]"
    lines = [
        "KNOWLEDGEBASE (GraphRAG, "
        f"index {answer.version}, {answer.method} search — cite only these sources):",
        body,
    ]
    titles = answer.source_titles
    if titles:
        lines.append("")
        lines.append("SOURCE DOCUMENTS:")
        lines.extend(f"- {t}" for t in titles)
    return "\n".join(lines)
