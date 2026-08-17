"""Process configuration.

All four process types (api, ingestor, worker, scheduler) share one image and one
settings object; they differ only in entrypoint. Everything comes from environment
variables so the same image can run locally, in staging and in production with no
code change — in AWS these are injected from SSM Parameter Store (ARCHITECTURE §7).

`extra="forbid"` is deliberate: a typo'd variable in a task definition should fail
the container at boot, loudly, rather than silently fall back to a default.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AUTOTRAIN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
    )

    environment: Literal["local", "staging", "production"] = "local"
    log_level: str = "INFO"

    database_url: str
    db_pool_min_size: int = Field(default=1, ge=0)
    db_pool_max_size: int = Field(default=10, ge=1)
    # Fail a request rather than queue behind an exhausted pool forever.
    db_pool_timeout_seconds: float = Field(default=10.0, gt=0)

    migrations_dir: Path = _BACKEND_ROOT / "migrations"

    # Bind address for the api process. Loopback by default so a dev machine
    # never exposes the stub-auth API by accident; containers set 0.0.0.0.
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)

    # Ingestor (delay sweep) — process type 2. 'none' makes the ingestor
    # refuse to start; 'hsp' arrives with the HSP client PR.
    arrivals_source: Literal["none", "hsp"] = "none"
    ingestor_batch_size: int = Field(default=200, ge=1)
    ingestor_interval_seconds: float = Field(default=900.0, gt=0)
    # Wait this long after scheduled arrival before asking the source — HSP
    # publishes next-day, so asking immediately is a guaranteed miss.
    ingestor_arrival_lag_minutes: int = Field(default=120, ge=0)
    # A journey still without data this many days after travel is marked
    # 'unmatched' and leaves the sweep.
    ingestor_give_up_days: int = Field(default=7, ge=1)

    # Only read by the integration test suite, which drops and recreates it.
    test_database_url: str | None = None

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached so config is parsed once per process, not once per import."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
