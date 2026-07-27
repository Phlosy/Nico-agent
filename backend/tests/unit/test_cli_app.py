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
        return {
            "status": "alive",
            "service": "Nico Agent",
            "version": cli_module.__version__,
        }

    def readiness(self) -> dict[str, Any]:
        return {"status": "ready", "components": {}}

    def get_tenant(self) -> dict[str, Any]:
        return {"id": str(self.profile.tenant_id), "name": "Test tenant"}

    def setup_readiness(self) -> dict[str, Any]:
        return {
            "overall": "full",
            "areas": [
                {"key": "model", "state": "ready"},
                {"key": "web", "state": "ready"},
                {"key": "capabilities", "state": "ready"},
                {"key": "verification", "state": "ready"},
            ],
        }

    def web_status(self) -> dict[str, Any]:
        return {
            "writes_enabled": False,
            "tenant_revision": 1,
            "configured": False,
            "authorized": False,
            "enabled": False,
            "provider": None,
            "credential_ref": None,
            "secret_required": False,
            "diagnosis": "unconfigured",
            "latest_probe": None,
            "agents": [],
        }

    def list_projects(self, *, include_system: bool = False) -> list[dict[str, Any]]:
        del include_system
        return [
            {
                "id": "22222222-2222-4222-8222-222222222222",
                "name": "Research",
                "kind": "shared",
                "status": "active",
                "revision": 1,
            }
        ]

    def get_project(self, project_id: str) -> dict[str, Any]:
        return {
            "id": project_id,
            "name": "Research",
            "kind": "shared",
            "status": "active",
            "revision": 1,
        }

    def list_project_members(self, _project_id: str) -> list[dict[str, Any]]:
        return [
            {
                "id": "77777777-7777-4777-8777-777777777777",
                "agent_id": "33333333-3333-4333-8333-333333333333",
                "role": "lead",
                "status": "active",
                "revision": 1,
            }
        ]

    def list_project_sessions(self, _project_id: str) -> list[dict[str, Any]]:
        return [
            {
                "id": "88888888-8888-4888-8888-888888888888",
                "project_member_id": "77777777-7777-4777-8777-777777777777",
                "current_conversation_id": "33333333-3333-4333-8333-333333333333",
                "status": "active",
            }
        ]

    def open_project_session(self, project_id: str, session_id: str) -> dict[str, Any]:
        return {
            **self.create_conversation(title="Research — Researcher"),
            "project_id": project_id,
            "project_session_id": session_id,
        }

    def project_timeline(
        self,
        _project_id: str,
        _session_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        del limit
        return {
            "entries": [
                {
                    "sequence": after_sequence + 1,
                    "kind": "run",
                    "event_type": "RunCompleted",
                    "run_id": "55555555-5555-4555-8555-555555555555",
                    "facts": {"status": "completed"},
                    "links": {},
                }
            ],
            "next_cursor": None,
            "has_more": False,
        }

    def request_project_sync(self, project_id: str, **_kwargs) -> dict[str, Any]:
        return {"id": "cycle-1", "project_id": project_id, "status": "pending"}

    def update_project_cadence(self, project_id: str, **kwargs) -> dict[str, Any]:
        return {
            **self.get_project(project_id),
            "revision": kwargs["expected_project_revision"] + 1,
            "supervision_cadence_seconds": kwargs["cadence_seconds"],
        }

    def list_project_supervision_cycles(self, project_id: str) -> list[dict[str, Any]]:
        return [{"id": "cycle-1", "project_id": project_id, "status": "completed"}]

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

    def register_external_tool_provider(self, **kwargs) -> dict[str, Any]:
        return {"id": "99999999-9999-4999-8999-999999999999", **kwargs, "status": "registered"}

    def get_external_tool_provider(self, provider_id: str) -> dict[str, Any]:
        return {"id": provider_id, "name": "local.stub", "status": "active"}

    def transition_external_tool_provider(self, provider_id: str, *, action: str) -> dict[str, Any]:
        return {"id": provider_id, "name": "local.stub", "status": action}

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
    model_activated = False
    capabilities_activated = False
    web_skipped = False

    def setup_readiness(self):
        model_ready = type(self).model_activated
        capability_ready = type(self).capabilities_activated
        web_state = "skipped" if type(self).web_skipped else "incomplete"
        target = (
            {
                "agent_id": "33333333-3333-4333-8333-333333333333",
                "agent_name": "setup-assistant",
                "agent_revision": 1 if not capability_ready else 2,
                "agent_version_id": "44444444-4444-4444-8444-444444444444",
                "agent_version": 1 if not capability_ready else 2,
                "model_name": "model-a",
            }
            if model_ready
            else None
        )
        return {
            "schema_version": 1,
            "tenant_revision": 1 + int(type(self).web_skipped) + int(capability_ready),
            "overall": (
                "partial"
                if model_ready and capability_ready and type(self).web_skipped
                else "incomplete"
            ),
            "areas": [
                {
                    "key": "model",
                    "state": "ready" if model_ready else "incomplete",
                    "summary": "model",
                },
                {"key": "web", "state": web_state, "summary": "web"},
                {
                    "key": "capabilities",
                    "state": "ready" if capability_ready else "incomplete",
                    "summary": "capabilities",
                },
                {"key": "verification", "state": "incomplete", "summary": "verification"},
            ],
            "target": target,
            "selected_profile": "minimal" if capability_ready else None,
            "web_provider": None,
            "verified_at": None,
        }

    def update_setup_intent(self, _command):
        type(self).web_skipped = True
        return {"tenant_revision": 2, "web_intent": "skipped"}

    def capability_catalog(self, *, agent_id=None):
        return {
            "schema_version": 1,
            "tenant_revision": 2,
            "agent_id": agent_id,
            "profiles": [],
            "tools": [],
            "skills": [],
        }

    def preview_capabilities(self, command):
        return {
            "preview_hash": "c" * 64,
            "target_agent_name": "setup-assistant",
            "proposed_agent_version": 2,
            "risks": [],
            "diff": {},
            "command": command,
        }

    def activate_capabilities(self, command):
        type(self).capabilities_activated = True
        return {
            "agent_id": command["target"]["agent_id"],
            "agent_version_id": "44444444-4444-4444-8444-444444444444",
            "agent_version": 2,
        }

    def provider_setup_readiness(self):
        return {
            "needs_setup": True,
            "reason": "verified_native_route_required",
            "project_count": 1,
        }

    def provider_catalog(self):
        return {
            "schema_version": 1,
            "catalog_revision": "2026-07-24",
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
        type(self).model_activated = True
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
    assert eager.stdout.strip() == f"nico {cli_module.__version__}"
    assert json.loads(server.stdout) == {
        "client": cli_module.__version__,
        "server": cli_module.__version__,
        "service": "Nico Agent",
    }


def test_version_option_uses_local_build_identifier_instead_of_release_version(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NICO_BUILD_VERSION", "dev-abc123-dirty")

    result = runner.invoke(cli_module.app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "nico dev-abc123-dirty"


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
        "tenant_access",
    }
    assert json.loads(agents.stdout)[0]["name"] == "researcher"


def test_external_tool_provider_cli_is_remote_and_machine_readable(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli_module, "NicoApiClient", FakeClient)
    prefix = [
        "--config-file",
        str(tmp_path / "config.toml"),
        "--tenant-id",
        str(TENANT_ID),
        "--json",
        "tool-provider",
    ]
    provider_id = "99999999-9999-4999-8999-999999999999"

    registered = runner.invoke(
        cli_module.app,
        [
            *prefix,
            "register",
            "local.stub",
            "--endpoint",
            "https://provider.example.test",
            "--credential-ref",
            "env:NICO_TOOL_SECRET_STUB",
        ],
    )
    verified = runner.invoke(cli_module.app, [*prefix, "verify", provider_id])

    assert registered.exit_code == verified.exit_code == 0
    assert json.loads(registered.stdout)["status"] == "registered"
    assert json.loads(verified.stdout)["status"] == "verify"


def test_doctor_fails_when_profile_tenant_is_not_available(monkeypatch, tmp_path: Path) -> None:
    class MissingTenantClient(FakeClient):
        def get_tenant(self) -> dict[str, Any]:
            raise cli_module.CliError(
                "RESOURCE_NOT_FOUND",
                "tenant was not found",
                status_code=404,
            )

    monkeypatch.setattr(cli_module, "NicoApiClient", MissingTenantClient)
    result = runner.invoke(
        cli_module.app,
        [
            "--config-file",
            str(tmp_path / "config.toml"),
            "--tenant-id",
            str(TENANT_ID),
            "--json",
            "doctor",
        ],
    )

    assert result.exit_code == 1
    tenant_check = next(
        item for item in json.loads(result.stdout) if item["check"] == "tenant_access"
    )
    assert tenant_check == {
        "check": "tenant_access",
        "status": "fail",
        "detail": "RESOURCE_NOT_FOUND",
    }


def test_doctor_reports_partial_as_warning_without_failure(monkeypatch, tmp_path: Path) -> None:
    class PartialClient(FakeClient):
        def setup_readiness(self) -> dict[str, Any]:
            return {
                "overall": "partial",
                "areas": [
                    {"key": "model", "state": "ready"},
                    {"key": "web", "state": "skipped"},
                    {"key": "capabilities", "state": "ready"},
                    {"key": "verification", "state": "incomplete"},
                ],
            }

    monkeypatch.setattr(cli_module, "NicoApiClient", PartialClient)
    result = runner.invoke(
        cli_module.app,
        [
            "--config-file",
            str(tmp_path / "config.toml"),
            "--tenant-id",
            str(TENANT_ID),
            "--json",
            "doctor",
        ],
    )
    assert result.exit_code == 0
    checks = {item["check"]: item for item in json.loads(result.stdout)}
    assert checks["setup_overall"]["status"] == "warning"
    assert checks["setup_web"]["detail"] == "skipped"


def test_doctor_fails_for_blocked_setup_area(monkeypatch, tmp_path: Path) -> None:
    class BlockedClient(FakeClient):
        def setup_readiness(self) -> dict[str, Any]:
            return {
                "overall": "incomplete",
                "areas": [
                    {"key": "model", "state": "ready"},
                    {"key": "web", "state": "blocked"},
                    {"key": "capabilities", "state": "incomplete"},
                    {"key": "verification", "state": "incomplete"},
                ],
            }

    monkeypatch.setattr(cli_module, "NicoApiClient", BlockedClient)
    result = runner.invoke(
        cli_module.app,
        [
            "--config-file",
            str(tmp_path / "config.toml"),
            "--tenant-id",
            str(TENANT_ID),
            "--json",
            "doctor",
        ],
    )
    assert result.exit_code == 1
    checks = {item["check"]: item for item in json.loads(result.stdout)}
    assert checks["setup_web"]["status"] == "fail"


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
    assert cli_module.ConfigStore(config_path).load().profiles[
        "default"
    ].recent_personal_agent_id == UUID("33333333-3333-4333-8333-333333333333")


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
    FakeProviderClient.model_activated = False
    FakeProviderClient.capabilities_activated = False
    FakeProviderClient.web_skipped = False
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
            "--skip-web",
            "--capability-profile",
            "minimal",
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "partial"
    assert payload["model"] == "model-a"
    assert "secret:providers/openai" not in result.stdout


def test_setup_status_is_read_only_and_non_interactive(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli_module, "NicoApiClient", FakeClient)
    result = runner.invoke(
        cli_module.app,
        [
            "--config-file",
            str(tmp_path / "config.toml"),
            "--tenant-id",
            str(TENANT_ID),
            "--json",
            "setup",
            "--status",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["overall"] == "full"
    assert [area["key"] for area in payload["areas"]] == [
        "model",
        "web",
        "capabilities",
        "verification",
    ]


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


def test_project_status_and_timeline_resolve_exact_name(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli_module, "NicoApiClient", FakeClient)
    prefix = [
        "--config-file",
        str(tmp_path / "config.toml"),
        "--tenant-id",
        str(TENANT_ID),
        "--json",
        "project",
    ]

    status = runner.invoke(cli_module.app, [*prefix, "status", "Research"])
    timeline = runner.invoke(
        cli_module.app,
        [*prefix, "timeline", "Research", "--agent", "researcher"],
    )

    assert status.exit_code == 0, status.output
    assert json.loads(status.stdout)["project"]["name"] == "Research"
    assert timeline.exit_code == 0, timeline.output
    assert json.loads(timeline.stdout)["timeline"]["entries"][0]["kind"] == "run"


def test_project_open_one_shot_remembers_workspace(monkeypatch, tmp_path: Path) -> None:
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
            "project",
            "open",
            "Research",
            "--message",
            "summarize progress",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["workspace"]["project"]["name"] == "Research"
    assert payload["turn"]["assistant_output"] == {"answer": "hello"}
    assert cli_module.ConfigStore(config_path).load().profiles["default"].recent_project_id == UUID(
        "22222222-2222-4222-8222-222222222222"
    )


def test_project_supervision_commands_keep_json_stdout_clean(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli_module, "NicoApiClient", FakeClient)
    prefix = [
        "--config-file",
        str(tmp_path / "config.toml"),
        "--tenant-id",
        str(TENANT_ID),
        "--json",
        "project",
    ]

    sync = runner.invoke(cli_module.app, [*prefix, "sync", "Research"])
    cadence = runner.invoke(cli_module.app, [*prefix, "cadence", "Research", "2h"])
    disabled = runner.invoke(cli_module.app, [*prefix, "cadence", "Research", "off"])

    assert sync.exit_code == cadence.exit_code == disabled.exit_code == 0
    assert json.loads(sync.stdout)["status"] == "pending"
    assert json.loads(cadence.stdout)["supervision_cadence_seconds"] == 7200
    assert json.loads(disabled.stdout)["supervision_cadence_seconds"] is None
