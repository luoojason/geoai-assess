# Running HCAS locally

The tool runs entirely on your machine (macOS or Windows) and talks to the LLM through the Anthropic API — no GPU, no server to rent.

## Easiest: one-click setup

- **macOS**: double-click `START-HCAS.command`. The first time, **right-click it → Open** (macOS blocks double-clicking files downloaded from the internet).
- **Windows**: install Python 3.9+ from python.org (check "Add python.exe to PATH"), then double-click `START-HCAS.bat`.

Either script installs everything, asks for the API key once (Anthropic `sk-ant-...` or OpenRouter `sk-or-...` — both work), starts the server, and opens the tool in your browser. Run it again any time to relaunch. First install takes a few minutes; the first data fetch for a new study area takes a minute or two (live OpenStreetMap queries).

Note for testing from mainland China: the Anthropic API and OpenStreetMap servers may require a VPN.

## Manual setup (developers)

### Prerequisites

- Python 3.9+
- An API key, used only by assessment steps 2 and 4 — either Anthropic (console.anthropic.com) or OpenRouter (openrouter.ai, key starts with `sk-or-`). Paste either into the same `ANTHROPIC_API_KEY=` slot; the app detects which it is.

## Setup

```bash
git clone <this repo>
cd geoai-assess
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and set: ANTHROPIC_API_KEY=sk-ant-...
```

## Run

```bash
python3 -m uvicorn backend.app:app --port 8000
```

Then open:

- **http://127.0.0.1:8000/hcas** — the 8-step gated assessment wizard: create a project (any US county), then Run → review/edit → Confirm through the steps. Steps 2 and 4 call Claude; everything else is local computation over OpenStreetMap data.
- **http://127.0.0.1:8000/** — the direct sub-tract assessment map (no LLM).

First data fetches take ~10–60s per layer (live OpenStreetMap queries).

## Tests

```bash
python3 -m pytest backend/tests -q   # no network, no API key needed
```

Costs: a full 8-step run makes 2 LLM calls (steps 2 and 4) — typically a few cents. Token usage per run is recorded in each step's audit record (`backend/hcas.db`).
