# HCAS as a hosted web app — see DEPLOY.md.
#
# Why a container and not a serverless function: one assessment step is a
# single blocking HTTP request that can legitimately run for minutes (step 3's
# layer budget alone is 900s by default), and geopandas/osmnx/folium need a
# warm process with a writable tile cache. Function platforms cap request time
# well below that.
FROM python:3.12-slim

# geopandas/shapely/pyproj ship manylinux wheels, so no build toolchain is
# needed — but GEOS/PROJ data and curl (for the healthcheck) are.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so an app-code change does not re-resolve the wheel set.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/

# Everything that must survive a redeploy lives under /data; mount the volume
# there. HCAS_DB puts the SQLite file on it — the default location is inside
# the image, which is discarded on every deploy.
ENV HCAS_DB=/data/hcas.db \
    PYTHONUNBUFFERED=1 \
    PORT=8000
# osmnx caches every Overpass response in ./cache relative to the working
# directory, and that cache is what makes a re-run of a half-failed step cheap.
# Symlink rather than an env var because osmnx takes the path from
# ox.settings.cache_folder, not the environment.
#
# Only the symlink is made here. The directory it points at is created at
# startup instead, because /data is where the platform mounts the volume at
# RUN time — anything this build writes under /data disappears behind that
# mount, so a build-time mkdir would leave the symlink dangling on first boot.
RUN rm -rf /app/cache && ln -s /data/osm-cache /app/cache

# One worker on purpose. A run holds its worker for the whole step, and the
# progress registry that drives the wizard's live bar is in-process — a second
# worker would answer progress polls for runs it knows nothing about. Scale by
# raising the instance size, not the worker count, until that state moves out
# of the process.
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
  CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1
CMD ["sh", "-c", "mkdir -p /data/osm-cache && uvicorn backend.app:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 950"]
