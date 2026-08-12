# Hosting HCAS on the web

Right now every tester downloads `HCAS-setup.zip`, installs Python, and runs a
server on their own laptop. Hosting replaces all of that with a URL and a
password. This file is what it takes, what it costs, and what it does not fix.

Everything here is prepared but **not deployed** — no account has been created
and nothing is live.

---

## What hosting actually buys

| Today | Hosted |
|---|---|
| A new zip after every fix, installed by hand | One URL, always the current build |
| Their Python version is part of the product (the 3.9 crash, the `.bat` key prompt) | Our Python, our container, identical for everyone |
| Their network does the OpenStreetMap / Census / CDC fetching | A datacenter does it, on a fast link that stays the same |
| Their machine must stay awake for a 7-minute step | The step runs server-side; closing the tab is already survivable |
| Each tester needs their own API key | One key we control, with one spend cap |

The middle two are the ones that have actually cost us weeks. Most of the field
reports so far have been about the *installation and the local network*, not
about the assessment.

## What it does not fix

- **Data-source outages.** If Overpass is down, it is down for the server too.
- **Concurrency.** A run holds a worker for minutes and the live progress bar is
  in-process state. One instance = one assessment at a time, comfortably. Two
  people starting a big county at once will queue. Fixing that properly means
  moving the run to a background worker and the progress registry out of the
  process — worth doing before "hundreds of concurrent uses", not before a
  three-person test.
- **Accounts.** There is one shared password (HTTP Basic), not per-user logins,
  so projects are visible to anyone who has it.

---

## Cost

### The short answer

**Compute: ~$0–7/month. Model calls: ~$0.10–0.35 per assessment run.** The model
calls are the real recurring cost, and hosting does not change them — it just
moves them onto one key instead of everyone's.

### Why the free serverless answer (Vercel + Supabase) does not work here

This is the natural guess and it is wrong for this specific app, for one
structural reason:

**A step is a single blocking HTTP request that can run for minutes.** Step 3's
layer budget alone is 420 seconds by default, and step 7 is comparable. Vercel
functions cap request duration well below that (60s on Hobby; 300s is the
ceiling on paid plans). The first county-sized fetch would be killed mid-request
every time — and the app would have no way to report why.

Three more, each independently disqualifying:

- **Bundle size.** Vercel's Python functions are limited to 250 MB unzipped.
  `geopandas` + `osmnx` + `pyproj` + `shapely` + `folium` + `pandas`/`numpy` do
  not fit, and there is no trimming that makes them fit.
- **No writable disk.** osmnx caches every Overpass response on disk, and that
  cache is exactly what makes re-running a half-failed step cheap. Serverless
  gives you an ephemeral `/tmp` per invocation, so every retry would pay full
  price again.
- **Cold start.** Importing this dependency set takes seconds, on every request.

Supabase is a good answer to a question we are not being asked yet. It would
replace the SQLite file with hosted Postgres — genuinely useful once there are
real users and real concurrency — but it provides no compute, so it does not
address any of the above. Keep it in mind for the "hundreds of concurrent uses"
conversation, not for this one.

**What the app needs instead is a small always-on container with a disk.** That
is a different (and cheaper to reason about) product than serverless.

### Options, honestly costed

| Option | Cost | Notes |
|---|---|---|
| **Fly.io**, shared-cpu-1x 1 GB + 1 GB volume | **~$5–6/mo** | Auto-stops when idle and wakes on request, so real cost is lower than the sticker. Recommended. |
| **Render**, Starter + 1 GB disk | **~$7/mo** | Simplest UI; deploys from the repo. |
| **Render free tier** | $0 | 512 MB RAM, **no persistent disk**, sleeps after 15 min. Projects would vanish on restart. Not usable as-is. |
| **Hugging Face Spaces**, free CPU | $0 | 2 vCPU / 16 GB RAM, Docker supported, no request timeout — the only genuinely free option with enough headroom. Storage is ephemeral unless you pay, so it would need Supabase (or similar) for persistence. A real path, but it is a second piece of work, not a free lunch. |
| **Dartmouth hardware** | $0 | Prof. Shi has offered this. No monthly bill and no data leaves the university, at the cost of an IT conversation and someone maintaining the box. |

Memory is the one number to watch, and it is now measured. A full 8-step run
over Adams County OH (1,518 km², all eight layers, 19,051 features) peaks at
**555 MB resident** — 115 MB of that is just importing the geospatial stack,
and the two spikes are the layer fetch (533 MB) and the step-7 Folium render
(553 MB). Steady state between steps is ~334 MB.

**That rules out every 512 MB tier**, which is the whole free column above, and
it confirms the 2 GB instance the blueprint asks for. Two cautions on the
number: it was taken outside a container (add uvicorn and FastAPI on top), and
Adams County is a *small rural* county. Feature count is what drives this, so a
metro county is a different measurement — an earlier Franklin County OH run
pulled ~90k features, roughly 4.7× this one, and nothing yet says 2 GB survives
that. **Measure a metro county before demoing one.**

### The cost that does not go away

Steps 2 and 4 call a model; measured earlier this cycle at **~$0.10–0.35 per
full 8-step run** depending on the route. Today those run free on the Claude Max
subscription through the `claude -p` CLI. **That path does not exist on a
server** — there is no logged-in CLI in a container. A hosted deploy must use a
real API key (an OpenRouter key with a hard credit cap is the safest form), and
that key pays for every run anyone does. At a test cadence of a few runs a day
this is a couple of dollars a month; it is the number that scales with usage.

---

## Deploying it

The app is packaged as a container: `Dockerfile` at the repo root, no build
step, no external services.

### Required environment

| Variable | Value | Why |
|---|---|---|
| `ANTHROPIC_API_KEY` | `sk-or-...` (OpenRouter) or `sk-ant-...` | Steps 2 and 4. Use a credit-capped OpenRouter key. |
| `DEMO_PASSWORD` | any passphrase | Turns on HTTP Basic auth for every route. **Without it the app is open to the internet.** |
| `HCAS_DB` | `/data/hcas.db` | Already set in the Dockerfile; must land on the mounted volume. |
| `CENSUS_API_KEY` | optional, free | Median household income / home value / age on step 1. |
| `HCAS_LAYER_BUDGET_S` | optional, default `420` | Per-step ceiling on the layer fetch. |

Mount a volume at **`/data`** (1 GB is plenty). It holds the SQLite database and
the osmnx tile cache, both of which must survive a redeploy.

### Fly.io

```bash
fly launch --no-deploy                     # answer no to a managed Postgres
fly volumes create hcas_data --size 1
fly secrets set ANTHROPIC_API_KEY=sk-or-... DEMO_PASSWORD=...
fly deploy
```

In `fly.toml`, mount the volume and keep one machine:

```toml
[[mounts]]
  source      = "hcas_data"
  destination = "/data"

[http_service]
  internal_port        = 8000
  auto_stop_machines   = "suspend"
  auto_start_machines  = true
  min_machines_running = 0
```

### Render — the path this repo is set up for

`render.yaml` at the root is a Blueprint: it declares the service, the region,
the 1 GB disk at `/data`, the health check, and which environment variables to
prompt for. Nothing has to be configured by hand in the dashboard, and no secret
is ever written into the repo (`sync: false` means Render asks you for the value
and stores it encrypted).

1. Sign in to render.com with GitHub and grant access to `geoai-assess`
   (it is a private repo, so Render's GitHub app has to be authorised for it).
2. **New → Blueprint**, pick the repo, pick the branch, **Apply**.
3. Render prompts for `ANTHROPIC_API_KEY`, `DEMO_PASSWORD` and
   `CENSUS_API_KEY`. Paste them there — they go straight from you to Render.
4. First build takes a while (the geospatial wheel set is large). Subsequent
   deploys reuse the layer cache.

The blueprint asks for the **Standard** instance (2 GB). Starter is 512 MB and
$18/mo cheaper, and the measurement above settles it: a small rural county
peaks at **555 MB**, so Starter would be killed on the first real run. Do not
downgrade it to save the difference.

### One risk to watch on the first real run

A step is a single HTTP request that can run for minutes, and a hosted platform
puts a proxy in front of it. If that proxy has an idle-connection limit shorter
than a big county's fetch, the browser sees the connection drop.

The app now survives that: the run continues server-side, and when the request
fails the wizard asks the server whether the step is still going and re-attaches
to its live progress instead of reporting an error. That is tested, but the
proxy limit itself is not — **watch the first county-sized step 3 and confirm the
progress bar keeps moving.** If it drops and recovers, that is this mechanism
working, not a bug.

### Checking it worked

```bash
curl -fsS https://<host>/healthz          # {"status":"ok"} — no auth needed
curl -u :$DEMO_PASSWORD https://<host>/hcas   # should return the wizard HTML
curl -fsS https://<host>/hcas                 # should 401 once DEMO_PASSWORD is set
```

Then walk one small county end to end (steps 1 → 8) before sending the URL to
anyone. The first run on a fresh volume has a cold osmnx cache and is the
slowest one you will see.

### Not yet verified

The image has **not been built or run** — there is no Docker on the development
machine. The two things it changes in the app were tested directly: `HCAS_DB`
redirects the database to the given path, and `/healthz` answers 200 without
credentials. The Dockerfile itself is unexercised; expect to fix something on
the first build.
