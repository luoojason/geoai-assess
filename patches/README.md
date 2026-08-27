# Patches for Prof. Wang's GraphRAG runtime

The knowledgebase bundle (`graphrag-english-runtime-*`) is not vendored here —
it is 725 MB of index data and is re-downloaded from Prof. Shi's 2026-08-25
Slack message. These patches apply on top of an extracted copy.

## graphrag-runtime-posix-paths.patch

The bundle hardcodes the Windows venv layout:

```js
const GRAPH_RAG_EXE = join(ROOT, ".venv-graphrag", "Scripts", "graphrag.exe");
```

`ready` is gated on that path existing, so on macOS and Linux the server starts
and serves `status`, `graph`, and `evidence` — which read the parquet files
directly — while `ready` never becomes true and no query can ever be spawned.
It looks healthy and does nothing. This picks `bin/` and drops `.exe` off
Windows.

Without it, running the knowledgebase beside HCAS on the Linux host is
impossible, which is otherwise the deployment we want (it keeps the port off
the network entirely).

Apply with:

```bash
cd <extracted-bundle>
patch -p0 < /path/to/geoai-assess/patches/graphrag-runtime-posix-paths.patch
```
