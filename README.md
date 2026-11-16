# HCAS: Healthy Community Assessment System

HCAS walks a user through a healthy-community assessment of a US study area in
8 steps. At each step the system drafts a result (with an LLM at steps 2 and 4,
deterministically elsewhere), the user reviews and edits it, and only a
confirmed step unlocks the next one. Every step keeps the AI draft, the human's
final version and the edit between them, so the record of human-in-the-loop
decisions is part of the output.

| Step | What it produces |
|---|---|
| 1. Study area intake | The project area, built from Census reporting units (county, town/township, ZIP code area, tract, block group), with AI help to turn a description into units |
| 2. Factor & metric selection | Health factors and metrics, drafted by the LLM, optionally grounded in the knowledgebase |
| 3. Data retrieval | Map layers from public sources (Census ACS and TIGERweb, OpenStreetMap, CDC PLACES and SVI, EPA air quality) |
| 4. Solution framework | The analysis approach, drafted by the LLM |
| 5. GIS database construction | The assembled database for the area |
| 6. Workflow design | The GIS workflow |
| 7. GIS procedure execution | The metrics, computed per reporting unit |
| 8. Report assembly | An editable report with references |

## Layout

| Path | What it is |
|---|---|
| `backend/app.py` | FastAPI app and HTTP routes (`/hcas/...`) |
| `backend/hcas.py` | The step coordinator: step runners, gating, persistence (SQLite), audit trail |
| `backend/gis/` | Study-area resolution and reporting units (`areas.py`, `profile.py`) |
| `backend/assess/` | Data fetching (OpenStreetMap/Overpass, Census) and metric computation |
| `backend/kg.py` | Client for the optional GraphRAG knowledgebase service |
| `backend/tests/` | Test suite (`python -m pytest backend/tests`) |
| `frontend/templates/hcas.html` | The wizard UI |
| `deploy/railway.Dockerfile` | The image the hosted sites run |
| `docs/` | Knowledgebase notes and measurements |

## Run it locally

Needs Python 3.9+.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env    # add ANTHROPIC_API_KEY (and optionally CENSUS_API_KEY)
python -m uvicorn backend.app:app --port 8000
```

Then open http://localhost:8000/hcas. One-click launchers (`START-HCAS.bat`,
`START-HCAS.command`) and every setting are described in
[README-local-run.md](README-local-run.md).

## Hosting

The hosted sites run on Railway from `deploy/railway.Dockerfile`, as two
services built from the same code: one without the knowledgebase and one with
it (`KG_ENABLED=true`, pointing at a separate GraphRAG service). Both sit behind
a shared password (`DEMO_PASSWORD`). Keys are set as service variables and are
never stored in this repository; `.env.example` lists the names.
[DEPLOY.md](DEPLOY.md) is the original hosting plan and its reasoning.
