from nico_agent.config import Settings


def test_settings_use_safe_local_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment == "development"
    assert settings.database_url.startswith("postgresql+asyncpg://")
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
