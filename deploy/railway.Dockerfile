# HCAS on Railway. Same image as ../Dockerfile, plus the Claude Code CLI so the
# model calls run on the Claude subscription (CLAUDE_CODE_OAUTH_TOKEN, made with
# `claude setup-token`) instead of a metered API key. hcas.py falls back to
# `claude -p` whenever no ANTHROPIC_API_KEY is set.
#
# Used by both Railway services: `hcas` (KG_ENABLED=false, the professors' site)
# and `hcas-kb` (KG_ENABLED=true, KG_BASE_URL -> the graphrag service).
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Native installer: a standalone binary in /root/.local/bin, no Node needed.
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/root/.local/bin:${PATH}" DISABLE_AUTOUPDATER=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ backend/
COPY frontend/ frontend/

ENV HCAS_DB=/data/hcas.db PYTHONUNBUFFERED=1 PORT=8000
RUN rm -rf /app/cache && ln -s /data/osm-cache /app/cache

EXPOSE 8000
CMD ["sh", "-c", "mkdir -p /data/osm-cache && uvicorn backend.app:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 950"]
