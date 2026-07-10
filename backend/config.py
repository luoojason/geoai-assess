from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # App settings
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # LLM (HCAS reasoning steps) — None falls through to the SDK's env/profile chain
    anthropic_api_key: str | None = None

    # Default projected CRS for CONUS counties (EPSG:5070 = CONUS Albers Equal Area)
    default_crs: str = "EPSG:5070"

    # Security settings
    cors_allowed_origins: list[str] = []
    demo_password: str | None = None
    rate_limit_per_minute: int = 30
    trust_proxy_headers: bool = True
    rate_limit_lru_max: int = 1024

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
