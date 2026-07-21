from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import pytest

from nico_agent.cli.config import CliConfig, ConfigStore, Profile
from nico_agent.cli.errors import CliError

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")


def test_config_store_round_trip_is_private_and_deterministic(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.toml"
    store = ConfigStore(path, environ={})
    config = CliConfig(
        current_profile="remote",
        profiles={
            "default": Profile(),
            "remote": Profile(
                base_url="https://nico.example.test/",
                tenant_id=TENANT_ID,
                actor_id="operator",
                api_token_env="NICO_REMOTE_TOKEN",
                timeout_seconds=12,
                service_command="/home/operator/.nico/bin/nico-service",
                install_root="/home/operator/.nico",
            ),
        },
    )

    store.save(config)

    assert store.load() == config
    assert os.stat(path).st_mode & 0o777 == 0o600
    content = path.read_text()
    assert "https://nico.example.test" in content
    assert "NICO_REMOTE_TOKEN" in content
    assert "/home/operator/.nico/bin/nico-service" in content
    assert "secret-value" not in content


def test_resolve_applies_environment_without_persisting_token(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    ConfigStore(path, environ={}).save(
        CliConfig(
            profiles={
                "default": Profile(
                    api_token_env="REMOTE_TOKEN",
                    tenant_id=TENANT_ID,
                )
            }
        )
    )
    store = ConfigStore(
        path,
        environ={
            "NICO_API_URL": "https://override.example.test",
            "NICO_ACTOR_ID": "env-actor",
            "REMOTE_TOKEN": "secret-value",
        },
    )

    resolved = store.resolve()

    assert resolved.base_url == "https://override.example.test"
    assert resolved.actor_id == "env-actor"
    assert resolved.api_token == "secret-value"
    assert resolved.public_dict()["api_token_available"] is True
    assert "secret-value" not in repr(resolved.public_dict())
    assert "secret-value" not in path.read_text()


def test_recent_personal_agent_is_profile_scoped_and_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    store = ConfigStore(path, environ={})
    store.save(
        CliConfig(
            current_profile="remote",
            profiles={"default": Profile(), "remote": Profile()},
        )
    )
    agent_id = UUID("33333333-3333-4333-8333-333333333333")

    store.remember_personal_agent("remote", agent_id)

    loaded = store.load()
    assert loaded.profiles["remote"].recent_personal_agent_id == agent_id
    assert loaded.profiles["default"].recent_personal_agent_id is None
    assert store.resolve(profile_name="remote").recent_personal_agent_id == agent_id


@pytest.mark.parametrize(
    "content",
    [
        "not = [valid",
        'current_profile = "missing"\n[profiles.default]\nbase_url = "http://localhost"',
    ],
)
def test_invalid_config_fails_with_stable_error(tmp_path: Path, content: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(content)

    with pytest.raises(CliError) as captured:
        ConfigStore(path, environ={}).load()

    assert captured.value.code in {"INVALID_CONFIG", "PROFILE_NOT_FOUND"}
    assert captured.value.exit_code == 2


def test_invalid_profile_url_and_token_env_are_rejected() -> None:
    with pytest.raises(ValueError):
        Profile(base_url="file:///tmp/nico")
    with pytest.raises(ValueError):
        Profile(api_token_env="not an env name")
