from __future__ import annotations

from io import StringIO
from typing import Any
from uuid import uuid4

import pytest

from nico_agent.cli.capabilities import CapabilityCoordinator, CapabilityInput
from nico_agent.cli.errors import CliError
from nico_agent.cli.output import Output


class FakeClient:
    def __init__(self) -> None:
        self.preview_command: dict[str, Any] | None = None
        self.activation_command: dict[str, Any] | None = None

    def capability_catalog(self, *, agent_id=None):
        return {
            "schema_version": 1,
            "tenant_revision": 4,
            "agent_id": agent_id,
            "profiles": [
                {
                    "key": "minimal",
                    "description": "No optional capabilities",
                    "recommended": False,
                },
                {
                    "key": "web_research",
                    "description": "Search and fetch",
                    "recommended": True,
                },
                {
                    "key": "developer",
                    "description": "Files and Python",
                    "recommended": False,
                },
                {
                    "key": "custom",
                    "description": "Choose capabilities",
                    "recommended": False,
                },
            ],
            "tools": [
                {
                    "reference": "file.read@1.0.0",
                    "usable": True,
                    "risk": "low",
                },
                {
                    "reference": "python.execute@1.0.0",
                    "usable": True,
                    "risk": "high",
                },
                {
                    "reference": "database.read@1.0.0",
                    "usable": False,
                    "risk": "medium",
                    "reason_code": "DATABASE_SOURCE_REQUIRED",
                },
            ],
            "skills": [],
        }

    def get_agent(self, agent_id):
        return {"id": agent_id, "revision": 7}

    def preview_capabilities(self, command):
        self.preview_command = command
        selected = command["selection"]["tool_refs"]
        risks = ["high"] if "python.execute@1.0.0" in selected else []
        return {
            "preview_hash": "a" * 64,
            "target_agent_name": "agent",
            "proposed_agent_version": 2,
            "risks": risks,
            "diff": {
                "tools_added": selected,
                "tools_removed": [],
                "skills_added": [],
                "skills_removed": [],
                "model_route_changed": False,
            },
        }

    def activate_capabilities(self, command):
        self.activation_command = command
        return {
            "agent_id": command["target"].get("agent_id", str(uuid4())),
            "agent_version_id": str(uuid4()),
            "agent_version": 2,
        }


def coordinator(
    client: FakeClient,
    *,
    interactive: bool = False,
    prompts: list[str] | None = None,
    confirms: list[bool] | None = None,
) -> CapabilityCoordinator:
    prompt_values = iter(prompts or [])
    confirm_values = iter(confirms or [])
    return CapabilityCoordinator(
        client,  # type: ignore[arg-type]
        Output(no_color=True, stdout=StringIO(), stderr=StringIO()),
        interactive=interactive,
        prompt=lambda *_args, **_kwargs: next(prompt_values),
        confirm=lambda *_args, **_kwargs: next(confirm_values),
    )


def test_recommended_profile_is_selected_interactively() -> None:
    client = FakeClient()
    result = coordinator(
        client,
        interactive=True,
        prompts=["web_research"],
        confirms=[True],
    ).publish(CapabilityInput(agent_id="agent-1"))
    assert result["status"] == "ready"
    assert client.preview_command["selection"] == {
        "profile": "web_research",
        "tool_refs": [],
        "skill_version_ids": [],
    }


def test_custom_select_all_excludes_unavailable_and_requires_high_risk() -> None:
    client = FakeClient()
    result = coordinator(client).publish(
        CapabilityInput(
            agent_id="agent-1",
            profile="custom",
            select_all=True,
            accepted_risks=["high"],
            confirmed=True,
        )
    )
    selected = client.preview_command["selection"]["tool_refs"]
    assert selected == ["file.read@1.0.0", "python.execute@1.0.0"]
    assert "database.read@1.0.0" not in selected
    assert client.activation_command["accepted_risks"] == ["high"]
    assert result["new_conversation_required"] is True


def test_custom_clear_all_is_a_valid_empty_selection() -> None:
    client = FakeClient()
    coordinator(client).publish(
        CapabilityInput(
            agent_id="agent-1",
            profile="custom",
            clear_all=True,
            confirmed=True,
        )
    )
    assert client.preview_command["selection"]["tool_refs"] == []


def test_noninteractive_risk_confirmation_is_fail_closed() -> None:
    client = FakeClient()
    with pytest.raises(CliError) as captured:
        coordinator(client).publish(
            CapabilityInput(
                agent_id="agent-1",
                profile="custom",
                tool_refs=["python.execute@1.0.0"],
                confirmed=True,
            )
        )
    assert captured.value.code == "CAPABILITY_RISK_CONFIRMATION_REQUIRED"
    assert client.activation_command is None


def test_starter_target_uses_exact_source_version() -> None:
    client = FakeClient()
    source_version = str(uuid4())
    coordinator(client).publish(
        CapabilityInput(
            starter_agent_name="starter-agent",
            starter_agent_display_name="Starter Agent",
            source_agent_version_id=source_version,
            profile="minimal",
            confirmed=True,
        )
    )
    assert client.preview_command["target"] == {
        "starter_agent_name": "starter-agent",
        "starter_agent_display_name": "Starter Agent",
        "source_agent_version_id": source_version,
    }
