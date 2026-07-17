import pytest
from pydantic import ValidationError

from nico_agent.config import Settings


def test_settings_use_safe_local_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment == "development"
    assert settings.resolved_database_url.startswith("postgresql+asyncpg://")
    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.minio_url == "http://localhost:9000"
    assert settings.dependency_timeout_seconds == 2.0


def test_settings_read_prefixed_environment(monkeypatch) -> None:
    monkeypatch.setenv("NICO_ENVIRONMENT", "test")
    monkeypatch.setenv("NICO_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("NICO_DEPENDENCY_TIMEOUT_SECONDS", "0.25")

    settings = Settings(_env_file=None)

    assert settings.environment == "test"
    assert settings.log_level == "DEBUG"
    assert settings.dependency_timeout_seconds == 0.25


def test_database_components_are_safely_encoded_in_the_url() -> None:
    settings = Settings(
        database_user="agent@example.com",
        database_password="pass@word:/#",
        _env_file=None,
    )

    assert settings.resolved_database_url == (
        "postgresql+asyncpg://agent%40example.com:pass%40word%3A%2F%23@localhost:5432/nico_agent"
    )


def test_production_rejects_default_sandbox_runner_token() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production", _env_file=None)

    settings = Settings(
        environment="production",
        sandbox_runner_token="production-runner-token-replaced",
        _env_file=None,
    )
    assert settings.sandbox_runner_token == "production-runner-token-replaced"
