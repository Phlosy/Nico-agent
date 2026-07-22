from __future__ import annotations

from io import StringIO
from typing import Any

import pytest

from nico_agent.cli.capabilities import CapabilityInput
from nico_agent.cli.errors import CliError
from nico_agent.cli.output import Output
from nico_agent.cli.setup import GuidedSetupCoordinator, GuidedSetupInput


class FakeSetupClient:
    def __init__(self) -> None:
        self.model = False
        self.web = False
        self.skipped = False
        self.capabilities = False
        self.verified = False
        self.proof_blocked = False
        self.turn_options: dict[str, Any] | None = None
        self.model_setup_calls = 0
        self.web_setup_calls = 0

    def setup_readiness(self):
        tenant_revision = 1 + int(self.skipped or self.web) + int(self.capabilities)
        if self.verified:
            tenant_revision += 1
        return {
            "schema_version": 1,
            "tenant_revision": tenant_revision,
            "overall": (
                "full"
                if self.verified
                else (
                    "partial" if self.model and self.capabilities and self.skipped else "incomplete"
                )
            ),
            "areas": [
                {
                    "key": "model",
                    "state": "ready" if self.model else "incomplete",
                    "summary": "model",
                },
                {
                    "key": "web",
                    "state": "ready" if self.web else ("skipped" if self.skipped else "incomplete"),
                    "summary": "web",
                },
                {
                    "key": "capabilities",
                    "state": "ready" if self.capabilities else "incomplete",
                    "summary": "capabilities",
                },
                {
                    "key": "verification",
                    "state": (
                        "ready"
                        if self.verified
                        else ("blocked" if self.proof_blocked else "incomplete")
                    ),
                    "summary": "verification",
                },
            ],
            "target": (
                {
                    "agent_id": "agent-1",
                    "agent_name": "assistant",
                    "agent_revision": 2 if self.capabilities else 1,
                    "agent_version_id": "version-2" if self.capabilities else "version-1",
                    "agent_version": 2 if self.capabilities else 1,
                    "model_name": "deepseek-test",
                }
                if self.model
                else None
            ),
            "selected_profile": "web_research" if self.capabilities else None,
            "web_provider": "searxng" if self.web else None,
            "verified_at": "2026-07-22T00:00:00Z" if self.verified else None,
        }

    def update_setup_intent(self, _command):
        self.skipped = True
        return {"tenant_revision": 2, "web_intent": "skipped"}

    def create_conversation(self, **_kwargs):
        return {"id": "conversation-1"}

    def create_conversation_turn(self, _conversation_id, _message, **kwargs):
        self.turn_options = kwargs
        return {"id": "turn-1", "run_id": "run-1"}

    def stream_run_events(self, _run_id, **_kwargs):
        return iter(())

    def get_run(self, run_id):
        return {"id": run_id, "status": "completed"}

    def validate_setup_proof(self, **_kwargs):
        self.verified = True
        return {"state": "succeeded"}


class FakeCapabilities:
    def __init__(self, client: FakeSetupClient) -> None:
        self.client = client
        self.values: CapabilityInput | None = None

    def publish(self, values: CapabilityInput):
        self.values = values
        self.client.capabilities = True
        return {"status": "ready"}


def coordinator(client: FakeSetupClient):
    output = Output(no_color=True, stdout=StringIO(), stderr=StringIO())
    capabilities = FakeCapabilities(client)

    def model_setup():
        client.model_setup_calls += 1
        client.model = True
        return {"status": "ready"}

    def web_setup():
        client.web_setup_calls += 1
        client.web = True
        client.skipped = False
        return {"status": "ready"}

    value = GuidedSetupCoordinator(
        client,  # type: ignore[arg-type]
        output,
        interactive=False,
        prompt=lambda *_args, **_kwargs: "",
        confirm=lambda *_args, **_kwargs: False,
        model_setup=model_setup,
        web_setup=web_setup,
        capabilities=capabilities,  # type: ignore[arg-type]
    )
    return value, capabilities


def test_full_setup_runs_only_missing_areas_and_bounded_platform_proof() -> None:
    client = FakeSetupClient()
    setup, capabilities = coordinator(client)
    result = setup.run(
        GuidedSetupInput(enable_web=True, automatically_approve_tools=True),
        CapabilityInput(profile="web_research", confirmed=True),
    )
    assert result["status"] == "full"
    assert result["chat_command"] == "nico chat --agent agent-1"
    assert capabilities.values.agent_id == "agent-1"
    assert client.turn_options["max_steps"] == 3
    assert client.turn_options["token_budget"] == 1
    assert client.turn_options["timeout_seconds"] == 120
    assert client.turn_options["budgets"] == {
        "tool_allow": ["web.search@1.0.0", "web.fetch@1.1.0"],
        "setup_proof": True,
    }


def test_web_skip_finishes_partial_without_online_proof() -> None:
    client = FakeSetupClient()
    setup, _capabilities = coordinator(client)
    result = setup.run(
        GuidedSetupInput(skip_web=True),
        CapabilityInput(profile="minimal", confirmed=True),
    )
    assert result["status"] == "partial"
    assert client.turn_options is None


def test_noninteractive_setup_requires_explicit_web_choice() -> None:
    client = FakeSetupClient()
    client.model = True
    setup, _capabilities = coordinator(client)
    with pytest.raises(CliError) as captured:
        setup.run(
            GuidedSetupInput(),
            CapabilityInput(profile="minimal", confirmed=True),
        )
    assert captured.value.code == "SETUP_WEB_CHOICE_REQUIRED"


def test_explicit_enable_resumes_an_intentionally_skipped_web_step() -> None:
    client = FakeSetupClient()
    client.model = True
    client.skipped = True
    client.capabilities = True
    setup, _capabilities = coordinator(client)

    result = setup.run(
        GuidedSetupInput(enable_web=True, automatically_approve_tools=True),
        CapabilityInput(profile="web_research", confirmed=True),
    )

    assert result["status"] == "full"
    assert client.web_setup_calls == 1


def test_ready_setup_can_reconfigure_one_area_without_replaying_others() -> None:
    client = FakeSetupClient()
    client.model = True
    client.web = True
    client.capabilities = True
    client.verified = True
    setup, capabilities = coordinator(client)

    result = setup.run(
        GuidedSetupInput(reconfigure="capabilities"),
        CapabilityInput(profile="web_research", confirmed=True),
    )

    assert result["status"] == "full"
    assert capabilities.values is not None
    assert client.model_setup_calls == 0
    assert client.web_setup_calls == 0


def test_blocked_proof_is_reported_without_starting_an_unusable_run() -> None:
    client = FakeSetupClient()
    client.model = True
    client.web = True
    client.capabilities = True
    client.proof_blocked = True
    setup, _capabilities = coordinator(client)

    result = setup.run(
        GuidedSetupInput(),
        CapabilityInput(profile="minimal", confirmed=True),
    )

    assert result["status"] == "incomplete"
    assert client.turn_options is None
