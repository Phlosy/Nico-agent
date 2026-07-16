"""Environment-backed application configuration."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings shared by API, worker, migrations and health tooling."""

    model_config = SettingsConfigDict(
        env_prefix="NICO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Nico Agent Platform"
    app_version: str = "0.1.0"
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8080"]

    database_url: str = "postgresql+asyncpg://nico:nico@localhost:5432/nico_agent"
    redis_url: str = "redis://localhost:6379/0"
    minio_url: str = "http://localhost:9000"
    dependency_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    worker_health_interval_seconds: float = Field(default=15.0, gt=0, le=300)

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("minio_url")
    @classmethod
    def remove_minio_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide immutable settings instance."""

    return Settings()
