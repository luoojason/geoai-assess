# Knowledgebase integration (Prof. Wang's GraphRAG runtime)

Prof. Wang's knowledgebase arrived **2026-08-25** via Prof. Shi, as
`graphrag-english-runtime-20260729-051918.zip` (553 MB) plus a 5-page
`USER-GUIDE_0823.pdf`. This document records what it is, how HCAS talks to it,
and the decisions taken so the next person does not have to re-derive them.

## What actually shipped

Not the five-theory artifact the old `KG_THEORIES` mock was written against.
It is a **query-only Microsoft GraphRAG 3.1.0 workspace** with two prebuilt
indexes over the same 96-paper corpus:

| | v1 "Original Index" | **v2 "Bilingual Entity Resolution"** (default) |
|---|---|---|
| Papers | 96 | 96 |
| Documents | 1,169 | 1,169 |
| Text units | 1,416 | 1,416 |
| Entities | 12,542 | **13,955** |
| Relationships | 16,714 | **22,827** |
| Communities / reports | 2,331 | **2,803** |
| Entity→document links | — | 26,064 |

Both report `status: ok`; vectors are 2,048-dimensional. v2 was approved
2026-07-19 with a clean acceptance run (0 CJK entity titles, 12/12 alias
groups, 0 entities without sources, 0 graph self-loops).

It contains **no ingestion, no index construction, no source articles, and no
credentials**. It is a query surface, nothing more.

## The API

A small Node server serves JSON on loopback:

```
GET  /api/graphrag/status?version=v2
GET  /api/graphrag/graph
GET  /api/graphrag/evidence
POST /api/graphrag/query
POST /api/graphrag/translate
```

`POST /api/graphrag/query` takes
`{version: v1|v2, method: local|global, answer_language: en|zh|auto, question: 3–2000 chars}`
and returns `answer` (prose carrying GraphRAG citations such as
`[Data: Sources (12); Relationships (4)]`), `source_documents`,
`resolved_source_count`, and `duration_seconds`.

`local` search answers focused questions about a named concept or relationship.
`global` search synthesises across community reports and is much slower.

## Four constraints that shaped the client

1. **Single-flight.** The server runs one query at a time and answers a
   concurrent one with **HTTP 429**. `backend/kg.py` holds a process-wide lock
   so HCAS never races itself, and retries a 429 (which then means *another*
   client, most likely the browser UI) with backoff.
2. **Slow.** Queries take minutes and the server kills one at **15 minutes**.
   The vendor's own example client uses a **920 s** timeout, which is our
   default. This cannot sit inline in a fast request path.
3. **Loopback only, unauthenticated.** It binds `127.0.0.1` and has no auth,
   TLS, rate limiting, or multi-user queue. The guide explicitly says not to
   expose port 8765. Sanctioned deployments: run the client on the same host,
   put an authenticated HTTPS gateway in front after a security review, or host
   the indexes alongside the service.
4. **Two API keys.** An OpenAI-compatible generation endpoint, plus an
   **Alibaba Cloud DashScope** key for `text-embedding-v4`. The vectors were
   built at 2,048 dims with that model, and query-time embedding **must match**.
   This is a new third-party dependency, and a Chinese one — worth confirming
   who pays for it and whether the grant covers it.

### It is not really Windows-only

The package is *packaged* for Windows (`install.cmd`, `configure.cmd`,
`start.bat`), and the guide says Windows 10/11. But the server is Node plus a
Python venv, and it was **verified running on macOS on 2026-08-26**:

```
node server/graphrag_server.mjs 8799
curl "http://127.0.0.1:8799/api/graphrag/status?version=v2"
```

Both indexes reported `ready: true`; only `keys.generation` / `keys.embedding`
were false, since `configure.cmd` had never run. Only the launcher scripts are
Windows-bound, so **co-hosting the knowledgebase on the same Linux box as HCAS
is open** — that is the vendor's own deployment pattern 3, and it avoids
exposing the port entirely.

## The design decision that matters

`_STEP4_SCHEMA` **enum-locks** two things: which theory step 4 may cite, and
which reference titles it may quote. That lock is not incidental — it is the
experimental control the enum-lock grounding study rests on. Swapping in a
knowledgebase that returns free prose would have quietly destroyed it.

So the integration is deliberately *additive*:

- **The theory enum never changes.** `KG_THEORIES` stays the fixed taxonomy.
- **The knowledgebase supplies evidence underneath it**, appended to the step-4
  system prompt via `kg.as_prompt_context()`.
- **The reference enum widens, and only with real provenance** — the source
  documents the knowledgebase actually returned for that question
  (`KGAnswer.source_titles`). The lock is never relaxed; the admissible set
  grows to documents the corpus vouched for.

This is strictly stronger than the five canned summaries it replaces, and the
research control survives intact.

## Behaviour when the knowledgebase is absent

That is the normal case: a field tester running HCAS on a laptop has no
knowledgebase, because it binds to loopback on its own host.

`kg_enabled` defaults to **False**, and every entry point returns `None` rather
than raising. `_step4_system("")` and `_step4_schema(None)` return the original
constants *by identity*, so with the knowledgebase off, step 4 builds exactly
the prompt and schema it built before this module existed. Tests assert that.

## Configuration

In `backend/config.py`, all off by default:

| Setting | Default | Meaning |
|---|---|---|
| `kg_enabled` | `False` | Master switch |
| `kg_base_url` | `http://127.0.0.1:8765` | Where the runtime listens |
| `kg_version` | `v2` | `v2` recommended; `v1` stays queryable |
| `kg_status_timeout_s` | `10.0` | Readiness probe |
| `kg_query_timeout_s` | `920.0` | Vendor's own client value |
| `kg_busy_retries` | `2` | Retries on HTTP 429 |
| `kg_busy_backoff_s` | `5.0` | Linear backoff between retries |

To turn it on, run the runtime and set `KG_ENABLED=true` in `.env`.

## Running the knowledgebase locally

```bash
unzip graphrag-english-runtime-20260729-051918.zip -d kb
cd kb/graphrag-english-runtime-20260729-051918
chmod -R u+rwX .          # the archive ships read-only data dirs
node server/graphrag_server.mjs 8765
```

Answering real queries additionally needs the two API keys and the GraphRAG
Python venv that `install.cmd` builds (GraphRAG 3.1.0 + json-repair 0.61.5).
Without them the server starts and serves `status`, `graph`, and `evidence`,
but `query` returns **503**, which the client treats as "unavailable" and
degrades from.

## Open questions for Prof. Shi and Prof. Wang

- Who supplies the DashScope key and the generation endpoint, and does the
  grant cover them?
- Where should the knowledgebase run — beside HCAS on the Oracle host, or on
  Prof. Wang's machine behind an authenticated gateway?
- A minutes-long, single-flight query cannot block an interactive step. Should
  step 4 ground **asynchronously** (draft immediately, enrich when the answer
  lands), or is a multi-minute wait on that step acceptable?
- **What are the 96 papers?** The corpus list is not in the bundle, and the
  paper set is load-bearing for the methodology-grounding evaluation.
