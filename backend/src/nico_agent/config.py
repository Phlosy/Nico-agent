"""Environment-backed application configuration."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    """Runtime settings shared by API, worker, migrations and health tooling."""

    model_config = SettingsConfigDict(
        env_prefix="NICO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Nico Agent Platform"
    app_version: str = "0.2.0"
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:8080"]

    database_url: str | None = None
    database_host: str = "localhost"
    database_port: int = Field(default=5432, ge=1, le=65535)
    database_name: str = "nico_agent"
    database_user: str = "nico"
    database_password: str = "nico"
    redis_url: str = "redis://localhost:6379/0"
    minio_url: str = "http://localhost:9000"
    dependency_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    worker_health_interval_seconds: float = Field(default=15.0, gt=0, le=300)
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    worker_lease_seconds: int = Field(default=30, ge=5, le=3600)
    worker_heartbeat_seconds: float = Field(default=10.0, gt=0, le=1800)
    worker_concurrency: int = Field(default=1, ge=1, le=64)
    worker_id: str = Field(default="nico-worker", min_length=1, max_length=180)
    hermes_command: str = Field(default="hermes", min_length=1, max_length=1000)
    hermes_cwd: str | None = None

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("minio_url")
    @classmethod
    def remove_minio_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_worker_lease(self) -> Settings:
        if self.worker_heartbeat_seconds >= self.worker_lease_seconds:
            raise ValueError("worker heartbeat must be shorter than the Run lease")
        return self

    @property
    def resolved_database_url(self) -> str:
        if self.database_url is not None:
            return self.database_url
        return URL.create(
            "postgresql+asyncpg",
            username=self.database_user,
            password=self.database_password,
            host=self.database_host,
            port=self.database_port,
            database=self.database_name,
        ).render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide immutable settings instance."""

    return Settings()
