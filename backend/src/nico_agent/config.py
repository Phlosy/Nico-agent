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
    minio_access_key: str = Field(default="nico-minio", min_length=3, max_length=200)
    minio_secret_key: str = Field(
        default="nico-minio-change-me", min_length=8, max_length=500, repr=False
    )
    minio_bucket: str = Field(default="nico-artifacts", pattern=r"^[a-z0-9][a-z0-9.-]{1,61}$")
    artifact_max_bytes: int = Field(default=10_485_760, ge=1, le=104_857_600)
    conversation_attachment_ttl_seconds: int = Field(default=86_400, ge=60, le=604_800)
    conversation_attachment_excerpt_chars: int = Field(default=16_000, ge=0, le=64_000)
    conversation_attachment_max_count: int = Field(default=16, ge=1, le=100)
    conversation_attachment_max_total_bytes: int = Field(default=26_214_400, ge=1, le=524_288_000)
    dependency_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    worker_health_interval_seconds: float = Field(default=15.0, gt=0, le=300)
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    worker_lease_seconds: int = Field(default=30, ge=5, le=3600)
    worker_heartbeat_seconds: float = Field(default=10.0, gt=0, le=1800)
    worker_concurrency: int = Field(default=1, ge=1, le=64)
    worker_id: str = Field(default="nico-worker", min_length=1, max_length=180)
    provider_probe_concurrency: int = Field(default=1, ge=1, le=8)
    provider_probe_poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    provider_probe_lease_seconds: int = Field(default=90, ge=30, le=3600)
    project_supervision_concurrency: int = Field(default=1, ge=1, le=8)
    project_supervision_poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    project_supervision_lease_seconds: int = Field(default=90, ge=30, le=3600)
    worker_health_marker: str = Field(
        default="/tmp/nico-worker-ready", min_length=1, max_length=2000
    )
    hermes_enabled: bool = False
    hermes_command: str = Field(default="hermes", min_length=1, max_length=1000)
    hermes_cwd: str | None = None
    hermes_state_root: str = Field(default="/tmp/nico-agent-hermes", min_length=1, max_length=2000)
    workspace_root: str = Field(default="/tmp/nico-agent-workspaces", min_length=1, max_length=2000)
    workspace_max_file_bytes: int = Field(default=1_048_576, ge=1, le=10_485_760)
    workspace_max_total_bytes: int = Field(default=10_485_760, ge=1, le=104_857_600)
    http_max_response_bytes: int = Field(default=1_048_576, ge=1, le=10_485_760)
    http_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    http_read_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    http_max_redirects: int = Field(default=3, ge=0, le=10)
    http_allow_loopback: bool = False
    model_connect_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    model_read_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    model_max_response_bytes: int = Field(default=10_485_760, ge=1024, le=104_857_600)
    model_allow_http_loopback: bool = False
    model_trusted_private_hosts: list[str] = []
    model_allow_http_trusted_hosts: bool = False
    model_max_attempts: int = Field(default=3, ge=1, le=10)
    model_retry_base_seconds: float = Field(default=0.2, ge=0, le=30)
    model_endpoint_writes_enabled: bool = False
    native_post_tool_delay_seconds: float = Field(default=0, ge=0, le=300)
    tool_approval_required_risks: list[Literal["medium", "high"]] = ["medium", "high"]
    tool_approval_ttl_seconds: int = Field(default=900, ge=30, le=86_400)
    database_tool_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    database_tool_statement_timeout_ms: int = Field(default=5_000, ge=100, le=60_000)
    database_tool_max_rows: int = Field(default=500, ge=1, le=5_000)
    database_tool_max_output_bytes: int = Field(default=1_048_576, ge=1, le=10_485_760)
    sandbox_runner_url: str = Field(
        default="http://sandbox-runner:8090", min_length=1, max_length=2000
    )
    sandbox_runner_token: str = Field(
        default="nico-sandbox-development-token", min_length=16, max_length=500, repr=False
    )
    sandbox_docker_socket: str = Field(
        default="/var/run/docker.sock", min_length=1, max_length=2000
    )
    sandbox_image: str = Field(
        default=(
            "python:3.12.10-alpine@"
            "sha256:4bbf5ef9ce4b273299d394de268ad6018e10a9375d7efc7c2ce9501a6eb6b86c"
        ),
        min_length=80,
        max_length=500,
    )
    sandbox_wall_time_seconds: int = Field(default=10, ge=1, le=30)
    sandbox_memory_bytes: int = Field(default=134_217_728, ge=33_554_432, le=536_870_912)
    sandbox_nano_cpus: int = Field(default=500_000_000, ge=100_000_000, le=2_000_000_000)
    sandbox_pids_limit: int = Field(default=32, ge=1, le=128)
    sandbox_output_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    sandbox_max_concurrency: int = Field(default=4, ge=1, le=32)

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("minio_url")
    @classmethod
    def remove_minio_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("sandbox_runner_url")
    @classmethod
    def remove_sandbox_runner_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("sandbox_image")
    @classmethod
    def require_pinned_sandbox_image(cls, value: str) -> str:
        if "@sha256:" not in value or len(value.rsplit("@sha256:", 1)[1]) != 64:
            raise ValueError("sandbox image must be pinned by a sha256 digest")
        return value

    @model_validator(mode="after")
    def validate_worker_lease(self) -> Settings:
        if self.worker_heartbeat_seconds >= self.worker_lease_seconds:
            raise ValueError("worker heartbeat must be shorter than the Run lease")
        if self.workspace_max_file_bytes > self.workspace_max_total_bytes:
            raise ValueError("workspace file limit cannot exceed its total limit")
        if (
            self.environment == "production"
            and self.sandbox_runner_token == "nico-sandbox-development-token"
        ):
            raise ValueError("production requires a non-default Sandbox Runner token")
        if self.environment == "production" and self.minio_secret_key == "nico-minio-change-me":
            raise ValueError("production requires non-default MinIO credentials")
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
