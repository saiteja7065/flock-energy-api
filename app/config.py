"""Runtime settings, read from environment variables (or a local .env file)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="URJA_", env_file=".env", extra="ignore")

    # Portal connection
    base_url: str = "https://urja-ops.flockenergy.tech"
    email: str = Field(..., description="Portal login email")
    password: str = Field(..., description="Portal login password")
    timeout_s: float = 15.0

    # Politeness / robustness. The portal allows ~120 requests per rolling minute
    # (measured, see PROTOCOL.md); we stay comfortably below that.
    max_requests_per_minute: int = 100
    max_retries: int = 3

    # Freshness
    snapshot_ttl_s: int = 15 * 60  # meter catalogue (bulk export)
    consumption_ttl_s: int = 5 * 60  # per-meter energy responses


@lru_cache
def get_settings() -> Settings:
    return Settings()
