from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
