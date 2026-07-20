from __future__ import annotations

from typing import Any

import pytest

from nico_agent.cli.errors import CliError
from nico_agent.cli.output import Output
from nico_agent.cli.provider import ProviderInput, ProviderOnboardingCoordinator
from nico_agent.cli.service_bridge import SecretAttempt


class FakeClient:
    def __init__(self, *, activation_error: bool = False) -> None:
        self.activation_error = activation_error
        self.probes: list[dict[str, Any]] = []

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

    def create_provider_probe(self, *, kind, candidate, idempotency_key):
        del idempotency_key
        probe = {
            "id": f"probe-{len(self.probes) + 1}",
            "kind": kind,
            "status": "succeeded",
            "candidate_hash": "a" * 64,
            "verified_at": "2026-07-20T00:00:00Z" if kind == "verify_completion" else None,
            "result": {"models": [{"id": "model-a"}]},
            "candidate": candidate,
        }
        self.probes.append(probe)
        return probe

    def get_provider_probe(self, probe_id):
        return next(item for item in self.probes if item["id"] == probe_id)

    def cancel_provider_probe(self, probe_id):
        return {"id": probe_id, "status": "cancelled"}

    def list_projects(self):
        return [{"id": "project-1", "name": "Project", "status": "active"}]

    def list_agents(self):
        return []

    def get_agent(self, agent_id):
        return {"id": agent_id, "revision": 3}

    def preview_provider_activation(self, *, probe_id, candidate_hash, target):
        return {
            "probe_id": probe_id,
            "candidate_hash": candidate_hash,
            "preview_hash": "b" * 64,
            "projection": {"target": target},
        }

    def activate_provider(self, **kwargs):
        if self.activation_error:
            raise CliError("PROVIDER_PREVIEW_STALE", "preview changed")
        return {
            "agent_id": "agent-ready",
            "agent_version_id": "version-ready",
            "endpoint_id": "endpoint-ready",
            "request": kwargs,
        }


class FakeBridge:
    def __init__(self) -> None:
        self.recovered = 0
        self.begun: list[tuple[str, str]] = []
        self.renewed = 0
        self.committed = 0
        self.rolled_back = 0

    def recover(self):
        self.recovered += 1

    def begin(self, env_name, secret):
        self.begun.append((env_name, secret))
        return SecretAttempt("attempt-1", "token-" + "x" * 32, f"env:{env_name}")

    def renew(self, _attempt):
        self.renewed += 1

    def commit(self, _attempt):
        self.committed += 1

    def rollback(self, _attempt):
        self.rolled_back += 1


def _coordinator(client, bridge, *, interactive=False, answers=()):
    iterator = iter(answers)
    return ProviderOnboardingCoordinator(
        client,
        Output(json_mode=True, no_color=True),
        service_bridge=bridge,
        interactive=interactive,
        prompt=lambda *_args, **_kwargs: next(iterator),
        confirm=lambda *_args, **_kwargs: True,
        poll_interval=0,
    )


def test_reference_only_non_interactive_activation_never_opens_local_secret_bridge() -> None:
    client = FakeClient()
    bridge = FakeBridge()

    result = _coordinator(client, bridge).onboard(
        ProviderInput(
            provider_key="openai",
            credential_ref="secret:providers/openai",
            model="model-a",
            project_id="project-1",
            starter_agent_name="assistant-one",
            starter_agent_display_name="Assistant One",
            confirmed=True,
        )
    )

    assert result["status"] == "ready"
    assert result["chat_command"] == "nico chat --project project-1 --agent agent-ready"
    assert bridge.recovered == bridge.committed == bridge.rolled_back == 0
    assert client.probes[0]["candidate"]["credential_ref"] == "secret:providers/openai"


def test_local_key_failure_rolls_back_once_and_never_places_key_in_candidate() -> None:
    client = FakeClient(activation_error=True)
    bridge = FakeBridge()
    coordinator = _coordinator(
        client,
        bridge,
        interactive=True,
        answers=("key", "sk-unit-canary"),
    )

    with pytest.raises(CliError, match="preview changed"):
        coordinator.onboard(
            ProviderInput(
                provider_key="openai",
                model="model-a",
                project_id="project-1",
                starter_agent_name="assistant-two",
                starter_agent_display_name="Assistant Two",
                confirmed=True,
            )
        )

    assert bridge.recovered == 1
    assert bridge.begun[0][1] == "sk-unit-canary"
    assert bridge.committed == 0
    assert bridge.rolled_back == 1
    assert "sk-unit-canary" not in str(client.probes)


def test_non_interactive_flow_requires_reference_model_target_and_confirmation() -> None:
    with pytest.raises(CliError) as captured:
        _coordinator(FakeClient(), FakeBridge()).onboard(ProviderInput(provider_key="openai"))
    assert captured.value.code == "PROVIDER_INPUT_REQUIRED"
