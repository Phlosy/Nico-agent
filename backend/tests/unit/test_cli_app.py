from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from typer.testing import CliRunner

cli_module = importlib.import_module("nico_agent.cli.app")
runner = CliRunner()
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")


class FakeClient:
    def __init__(self, profile) -> None:
        self.profile = profile

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def liveness(self) -> dict[str, Any]:
        return {"status": "alive", "service": "Nico Agent", "version": "0.2.0"}

    def readiness(self) -> dict[str, Any]:
        return {"status": "ready", "components": {}}

    def list_projects(self) -> list[dict[str, Any]]:
        return []

    def get_project(self, project_id: str) -> dict[str, Any]:
        return {"id": project_id, "name": "Research", "status": "active"}

    def list_agents(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "33333333-3333-4333-8333-333333333333",
                "name": "researcher",
                "display_name": "Researcher",
                "status": "ready",
                "current_version_id": "version-1",
            }
        ]

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        return {"id": agent_id, "name": "researcher", "status": "ready"}

    def list_agent_versions(self, _agent_id: str) -> list[dict[str, Any]]:
        return []

    def get_task(self, task_id: str) -> dict[str, Any]:
        return {"id": task_id, "status": "running"}

    def get_run(self, run_id: str) -> dict[str, Any]:
        return {"id": run_id, "status": "completed"}

    def get_runtime(self, run_id: str) -> dict[str, Any]:
        return {"run_id": run_id, "provider_name": "mock", "status": "completed"}

    def list_run_events(self, _run_id: str) -> list[dict[str, Any]]:
        return []

    def create_conversation(self, **kwargs) -> dict[str, Any]:
        return {
            "id": "33333333-3333-4333-8333-333333333333",
            "mode": kwargs.get("mode", "project"),
            "project_id": kwargs.get("project_id") or "22222222-2222-4222-8222-222222222222",
            "agent_id": kwargs.get("agent_id", "33333333-3333-4333-8333-333333333333"),
            "title": kwargs["title"],
            "agent_version_id": "version-1",
            "status": "active",
        }

    def list_conversations(self, **_kwargs) -> list[dict[str, Any]]:
        return [self.create_conversation(title="Continued")]

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        return {
            **self.create_conversation(title="Resumed"),
            "id": conversation_id,
        }

    def list_conversation_turns(self, _conversation_id: str, **_kwargs) -> list[dict[str, Any]]:
        return []

    def create_conversation_turn(
        self, conversation_id: str, message: str, **_kwargs
    ) -> dict[str, Any]:
        return {
            "id": "44444444-4444-4444-8444-444444444444",
            "conversation_id": conversation_id,
            "task_id": "66666666-6666-4666-8666-666666666666",
            "run_id": "55555555-5555-4555-8555-555555555555",
            "user_input": message,
        }

    def stream_run_events(self, _run_id: str, *, after_sequence: int = 0):
        yield {"sequence": after_sequence + 1, "type": "RunCompleted", "payload": {}}

    def get_conversation_turn(self, turn_id: str) -> dict[str, Any]:
        return {
            "id": turn_id,
            "run_id": "55555555-5555-4555-8555-555555555555",
            "run_status": "completed",
            "run_revision": 2,
            "status": "completed",
            "assistant_output": {"answer": "hello"},
            "error": None,
        }


class FakeProviderClient(FakeClient):
    def provider_setup_readiness(self):
        return {
            "needs_setup": True,
            "reason": "verified_native_route_required",
            "project_count": 1,
        }

    def provider_catalog(self):
        return {
            "schema_version": 1,
            "catalog_revision": "2026-07-20",
            "providers": [
                {
                    "key": "openai",
                    "display_name": "OpenAI",
                    "protocol": "openai_compatible",
                    "locations": [
                        {
                            "key": "global",
                            "base_url": "https://api.openai.com/v1",
                            "default": True,
                        }
                    ],
                    "discovery": "openai_models",
                    "recommended_models": ["model-a"],
                }
            ],
        }

    def list_projects(self):
        return [{"id": "22222222-2222-4222-8222-222222222222", "status": "active"}]

    def create_provider_probe(self, *, kind, candidate, idempotency_key):
        del candidate, idempotency_key
        return {
            "id": "probe-1",
            "kind": kind,
            "status": "succeeded",
            "candidate_hash": "a" * 64,
            "verified_at": "2026-07-20T00:00:00Z",
            "result": {},
        }

    def preview_provider_activation(self, *, probe_id, candidate_hash, target):
        return {
            "probe_id": probe_id,
            "candidate_hash": candidate_hash,
            "preview_hash": "b" * 64,
            "projection": {"target": target},
        }

    def activate_provider(self, **_kwargs):
        return {
            "agent_id": "33333333-3333-4333-8333-333333333333",
            "agent_version_id": "version-1",
            "endpoint_id": "endpoint-1",
        }


def test_version_options_and_server_version(monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "NicoApiClient", FakeClient)

    eager = runner.invoke(cli_module.app, ["--version"])
    server = runner.invoke(cli_module.app, ["--json", "version", "--server"])

    assert eager.exit_code == 0
    assert eager.stdout.strip() == "nico 0.2.0"
    assert json.loads(server.stdout) == {
        "client": "0.2.0",
        "server": "0.2.0",
        "service": "Nico Agent",
    }


def test_config_profile_flow_never_writes_token(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    common = ["--config-file", str(config_path), "--json", "config"]

    saved = runner.invoke(
        cli_module.app,
        [
            *common,
            "set",
            "remote",
            "--api-url",
            "https://nico.example.test",
            "--tenant-id",
            str(TENANT_ID),
            "--api-token-env",
            "NICO_TEST_TOKEN",
        ],
        env={"NICO_TEST_TOKEN": "secret-value"},
    )
    selected = runner.invoke(cli_module.app, [*common, "use", "remote"])
    shown = runner.invoke(
        cli_module.app,
        [*common, "show"],
        env={"NICO_TEST_TOKEN": "secret-value"},
    )

    assert saved.exit_code == selected.exit_code == shown.exit_code == 0
    assert json.loads(shown.stdout)["api_token_available"] is True
    assert "secret-value" not in config_path.read_text()
    assert os.stat(config_path).st_mode & 0o777 == 0o600


def test_resource_command_requires_tenant(tmp_path: Path) -> None:
    result = runner.invoke(
        cli_module.app,
        ["--config-file", str(tmp_path / "missing.toml"), "--json", "agent", "list"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "TENANT_CONTEXT_REQUIRED"


def test_health_doctor_and_resource_json(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli_module, "NicoApiClient", FakeClient)
    prefix = [
        "--config-file",
        str(tmp_path / "config.toml"),
        "--tenant-id",
        str(TENANT_ID),
        "--json",
    ]

    health = runner.invoke(cli_module.app, [*prefix, "health"])
    doctor = runner.invoke(cli_module.app, [*prefix, "doctor"])
    agents = runner.invoke(cli_module.app, [*prefix, "agent", "list"])

    assert health.exit_code == doctor.exit_code == agents.exit_code == 0
    assert json.loads(health.stdout)["status"] == "ready"
    assert {item["check"] for item in json.loads(doctor.stdout)} >= {
        "config",
        "api_live",
        "dependencies",
    }
    assert json.loads(agents.stdout)[0]["name"] == "researcher"


def test_chat_one_shot_json_and_conversation_history(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli_module, "NicoApiClient", FakeClient)
    prefix = [
        "--config-file",
        str(tmp_path / "config.toml"),
        "--tenant-id",
        str(TENANT_ID),
        "--json",
    ]
    project = "22222222-2222-4222-8222-222222222222"
    agent = "33333333-3333-4333-8333-333333333333"

    chat = runner.invoke(
        cli_module.app,
        [*prefix, "chat", "hello", "--project", project, "--agent", agent],
    )
    history = runner.invoke(
        cli_module.app,
        [
            *prefix,
            "conversation",
            "history",
            "33333333-3333-4333-8333-333333333333",
        ],
    )

    assert chat.exit_code == 0, chat.output
    assert json.loads(chat.stdout)["turn"]["assistant_output"] == {"answer": "hello"}
    assert history.exit_code == 0
    assert json.loads(history.stdout) == []


def test_bare_chat_uses_ready_agent_and_remembers_it_per_profile(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(cli_module, "NicoApiClient", FakeClient)
    config_path = tmp_path / "config.toml"
    result = runner.invoke(
        cli_module.app,
        [
            "--config-file",
            str(config_path),
            "--tenant-id",
            str(TENANT_ID),
            "--json",
            "chat",
            "hello",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["conversation"]["project_id"] == "22222222-2222-4222-8222-222222222222"
    assert payload["conversation"]["mode"] == "personal"
    assert "_cli_mode" not in payload["conversation"]
    assert (
        cli_module.ConfigStore(config_path).load().profiles["default"].recent_personal_agent_id
        == UUID("33333333-3333-4333-8333-333333333333")
    )


def test_json_interactive_chat_is_rejected(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli_module, "NicoApiClient", FakeClient)
    result = runner.invoke(
        cli_module.app,
        [
            "--config-file",
            str(tmp_path / "config.toml"),
            "--tenant-id",
            str(TENANT_ID),
            "--json",
            "chat",
            "--continue",
        ],
    )
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "JSON_INTERACTIVE_UNSUPPORTED"


def test_setup_supports_safe_non_interactive_reference_flow(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli_module, "NicoApiClient", FakeProviderClient)
    project = "22222222-2222-4222-8222-222222222222"
    result = runner.invoke(
        cli_module.app,
        [
            "--config-file",
            str(tmp_path / "config.toml"),
            "--tenant-id",
            str(TENANT_ID),
            "--json",
            "setup",
            "--provider",
            "openai",
            "--credential-ref",
            "secret:providers/openai",
            "--model",
            "model-a",
            "--project",
            project,
            "--starter-name",
            "setup-assistant",
            "--starter-display-name",
            "Setup Assistant",
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "ready"
    assert payload["provider"] == "openai"
    assert "secret:providers/openai" not in result.stdout


def test_exec_supports_local_json_detach_and_private_output(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli_module, "NicoApiClient", FakeClient)
    output_file = tmp_path / "result.json"
    project = "22222222-2222-4222-8222-222222222222"
    agent = "33333333-3333-4333-8333-333333333333"

    result = runner.invoke(
        cli_module.app,
        [
            "--config-file",
            str(tmp_path / "config.toml"),
            "--tenant-id",
            str(TENANT_ID),
            "exec",
            "research this",
            "--project",
            project,
            "--agent",
            agent,
            "--detach",
            "--output",
            str(output_file),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["detached"] is True
    assert payload["task_id"] == "66666666-6666-4666-8666-666666666666"
    assert json.loads(output_file.read_text())["run_id"] == payload["run_id"]
    assert os.stat(output_file).st_mode & 0o777 == 0o600


def test_run_watch_supports_resume_cursor_and_local_json(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli_module, "NicoApiClient", FakeClient)
    run_id = "55555555-5555-4555-8555-555555555555"

    result = runner.invoke(
        cli_module.app,
        [
            "--config-file",
            str(tmp_path / "config.toml"),
            "--tenant-id",
            str(TENANT_ID),
            "run",
            "watch",
            run_id,
            "--after",
            "7",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["events"][0]["sequence"] == 8
    assert payload["run"]["status"] == "completed"
