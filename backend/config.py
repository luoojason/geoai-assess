import os
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values
from pydantic_settings import BaseSettings, SettingsConfigDict

# The installers (START-HCAS.bat / .command) write .env next to themselves,
# i.e. the repo root, one level above this package.
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load_env_file(path: Optional[Path] = None) -> None:
    """Export .env into os.environ without clobbering the real environment.

    Settings models a handful of names; the app documents many more and reads
    them straight from os.environ — CENSUS_API_KEY, HCAS_DB, HCAS_LAYER_BUDGET_S,
    the OSM_* timeouts. Without this, step 1's own instruction ("add
    CENSUS_API_KEY=... to the .env file next to START-HCAS") set a value that
    nothing ever read, and ACS stayed unavailable with the same message telling
    the user to do what they had already done.

    A real environment variable always wins, and a blank line in .env never
    shadows one — START-HCAS.bat's 2026-07-19 bug wrote exactly such a blank.
    """
    path = ENV_FILE if path is None else Path(path)
    if not path.is_file():
        return
    for key, value in dotenv_values(path, encoding="utf-8").items():
        if value and not os.environ.get(key):
            os.environ[key] = value


class Settings(BaseSettings):
    # App settings
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # LLM (HCAS reasoning steps) — None falls through to the SDK's env/profile chain
    anthropic_api_key: Optional[str] = None

    # Default projected CRS for CONUS counties (EPSG:5070 = CONUS Albers Equal Area)
    default_crs: str = "EPSG:5070"

    # Knowledgebase (Prof. Wang's GraphRAG runtime). Off unless an operator
    # points HCAS at a running instance — it binds to loopback on its own host,
    # so a field tester's laptop normally has none.
    kg_enabled: bool = False
    kg_base_url: str = "http://127.0.0.1:8765"
    kg_version: str = "v2"  # v2 = bilingual entity resolution; v1 kept queryable
    kg_status_timeout_s: float = 10.0
    # The vendor's own example client uses 920s; the server kills a query at 15 min.
    kg_query_timeout_s: float = 920.0
    kg_busy_retries: int = 2
    kg_busy_backoff_s: float = 5.0

    # Security settings
    cors_allowed_origins: list[str] = []
    demo_password: Optional[str] = None
    rate_limit_per_minute: int = 600
    trust_proxy_headers: bool = True
    rate_limit_lru_max: int = 1024

    # extra="ignore": a name in .env that Settings does not model must never
    # crash the app at import. It used to — the default is extra="forbid", so
    # CENSUS_API_KEY in .env raised a ValidationError before uvicorn ever bound,
    # which is the 2026-07-25 "could not open the site" failure class again.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


load_env_file()
settings = Settings()
