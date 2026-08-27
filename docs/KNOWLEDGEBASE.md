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

## The 96 papers — the list was in the bundle all along

The user guide does not list the corpus, which is why "what are the 96 papers?"
sat open. It did not need to: `data/v2/output/documents.parquet` carries every
source document with its metadata, and the 1,169 rows are **sections**, not
papers — each `title` is `<paper title> — <section heading>`, em dash U+2014.
Splitting on it yields exactly **96 distinct papers**, 12.2 sections each on
average (1 to 27).

`docs/kb-corpus.json` is that manifest, extracted 2026-08-26 and committed so
the grounding evaluation has a corpus definition that does not depend on
re-downloading 553 MB. Per paper: title, authors, DOI, year, journal, section
count, and the corpus's own category tags. Every paper has authors and a year;
**74 of 96 carry a DOI**. Years run 2016-2026, and `raw_data` also exposes
`keywords`, `content_hash`, `section_index` and `source_path`.

Three properties of the corpus that change how it should be used:

- **It is not an English corpus.** Despite the package name, the 1,169
  documents are **501 `en`, 444 `zh`, 224 `mixed`** — 43% is Chinese or
  bilingual. That is what the v2 "bilingual entity resolution" index and the
  `answer_language` parameter exist for, and any evaluation that assumes an
  English-only corpus is measuring the wrong thing.
- **Every document is tagged `rights_scope: internal_research`**, all 1,169 of
  them. Bibliographic metadata is one thing; the document text is another, and
  nothing here should be redistributed or published on that basis without
  asking Prof. Wang first.
- **It carries its own four-way taxonomy**, which is a ready-made axis for the
  grounding study: `1_理论_工作框架` (theory / working framework, 331 docs),
  `2_健康风险` (health risk, 450), `3_健康资源` (health resources, 364),
  `4_健康行为` (health behaviour, 140). Papers carry one primary `category`
  and may carry several in `categories`.

The journals skew to Chinese planning titles — 规划师 (88 sections), 城市发展研究
(80), 南方建筑 (60), 国际城市规划 (53), 风景园林 (52) — alongside IJERPH,
Applied Geography and Cities.

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

## Verified live, 2026-08-27

Everything below was measured on this machine against the real runtime, not
inferred from the guide.

### It is not Windows-only, but it does need a two-line patch

`server/graphrag_server.mjs` hardcoded the Windows venv layout:

```js
const GRAPH_RAG_EXE = join(ROOT, ".venv-graphrag", "Scripts", "graphrag.exe");
```

`ready` is gated on `existsSync(GRAPH_RAG_EXE)`, so on macOS or Linux the
server **starts and serves `status`, `graph`, and `evidence`** — those read the
parquet files directly — while `ready` can never become true and no query can
ever be spawned. It looks healthy and is inert. Patched to pick `bin/` and drop
`.exe` off Windows; with that, `ready: true` and queries run.

This is what makes deployment pattern 3 — the knowledgebase beside HCAS on the
Linux host — actually available.

### Global search needs no DashScope key

The query config tells the story:

```yaml
local_search:   embedding_model_id: default_embedding_model   # embeds the query
global_search:  (none)                                        # maps over reports
```

Confirmed by running both with a deliberately invalid embedding key:

| | Result |
|---|---|
| `--method global` | **Full cited answer in 63 s** (community level 0) |
| `--method local` | **401 from DashScope** — `invalid_api_key` |

So global search never contacts Alibaba. **One catch:** GraphRAG validates the
whole config at load, so `DASHSCOPE_API_KEY` must be *present* or the run dies
with `api_key must be set when auth_method=api_key`. A placeholder satisfies
it. No Alibaba account and no cost are required for global-only operation.

`ground_framework()` therefore defaults to `global`, against the vendor's
`local` default, and a test pins that — switching it back silently reintroduces
a hard dependency on a key we may never get.

### End-to-end through backend/kg.py

Against the live server, a real step-4-shaped question:

- **184 seconds** at the default community level (level 0 took 63 s)
- **88 source documents** resolved from 592 citation references
- Answer carried `[Data: Reports (N)]` citations throughout

Two things follow. First, 184 s confirms this cannot sit inside an interactive
step — the async question is real, not theoretical. Second, and more important:

### ⚠ The returned sources are mostly Chinese

For a **Franklin County, Ohio** question, the top resolved sources were:

```
健康城市规划的研究热点与展望：基于英文文献计量分析
景感生态学视角下的健康社区构建
城市建成环境对母婴健康的影响——研究综述与展望
Impact of High-Density Urban Built Environment on COPD: Jing'an District, Shanghai
Improvement of Walk Score from the Perspective of Public Health: Jing'an District, Shanghai
```

The corpus is 501 English / 444 Chinese / 224 mixed document sections, and the
retrieval reflects that. Because the reference enum widens to exactly these
returned titles, a US county assessment would cite Chinese-language studies of
Shanghai districts.

That may well be defensible — the underlying theory is plausibly transferable,
and Prof. Wang's team curated the corpus deliberately. But it is a
**methodology decision for the paper**, not an implementation detail, and it
should be settled with Prof. Shi and Prof. Wang rather than absorbed silently.

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
- The 96-paper list is **answered** — see the section above and
  `docs/kb-corpus.json`. What is still worth confirming with Prof. Wang: that
  the four-way category taxonomy means what its labels say, and whether the 22
  papers without a DOI are intentional (book chapters, reports) or a gap.
