from __future__ import annotations

import threading
from contextlib import contextmanager
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
    def __init__(self, *, renew_error: bool = False, commit_error: bool = False) -> None:
        self.renew_error = renew_error
        self.commit_error = commit_error
        self.recovered = 0
        self.begun: list[tuple[str, str]] = []
        self.renewed = 0
        self.committed = 0
        self.rolled_back = 0
        self.renew_event = threading.Event()

    def recover(self):
        self.recovered += 1

    def begin(self, env_name, secret):
        self.begun.append((env_name, secret))
        return SecretAttempt("attempt-1", "token-" + "x" * 32, f"env:{env_name}")

    def renew(self, _attempt):
        self.renewed += 1
        self.renew_event.set()
        if self.renew_error:
            raise CliError("RUNTIME_MAINTENANCE_LEASE_LOST", "lease lost", exit_code=4)

    def commit(self, _attempt):
        self.committed += 1
        if self.commit_error:
            raise CliError("RUNTIME_MAINTENANCE_LEASE_LOST", "release failed", exit_code=4)

    def rollback(self, _attempt):
        self.rolled_back += 1


def _coordinator(
    client,
    bridge,
    *,
    interactive=False,
    answers=(),
    confirm=None,
    renew_interval_seconds=30.0,
):
    iterator = iter(answers)
    return ProviderOnboardingCoordinator(
        client,
        Output(json_mode=True, no_color=True),
        service_bridge=bridge,
        interactive=interactive,
        prompt=lambda *_args, **_kwargs: next(iterator),
        confirm=confirm or (lambda *_args, **_kwargs: True),
        poll_interval=0,
        renew_interval_seconds=renew_interval_seconds,
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
        answers=("sk-unit-canary",),
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


def test_local_key_is_requested_in_the_first_hidden_prompt() -> None:
    client = FakeClient()
    bridge = FakeBridge()
    prompts: list[tuple[str, dict[str, Any]]] = []

    def prompt(message: str, **kwargs: Any) -> str:
        prompts.append((message, kwargs))
        return "sk-unit-canary"

    coordinator = ProviderOnboardingCoordinator(
        client,
        Output(json_mode=True, no_color=True),
        service_bridge=bridge,
        interactive=True,
        prompt=prompt,
        confirm=lambda *_args, **_kwargs: True,
        poll_interval=0,
    )
    progress_messages: list[str] = []

    class RecordingRenderer:
        @contextmanager
        def progress(self, message: str):
            progress_messages.append(message)
            yield

    coordinator.renderer = RecordingRenderer()

    result = coordinator.onboard(
        ProviderInput(
            provider_key="openai",
            model="model-a",
            project_id="project-1",
            starter_agent_name="assistant-hidden-key",
            starter_agent_display_name="Hidden Key Assistant",
            confirmed=True,
        )
    )

    assert result["status"] == "ready"
    assert prompts == [
        (
            "Provider API key (input hidden; leave blank to use an existing reference)",
            {"hide_input": True},
        )
    ]
    assert bridge.begun[0][1] == "sk-unit-canary"
    assert "sk-unit-canary" not in str(client.probes)
    assert progress_messages == [
        "Checking local credential state…",
        "Securing API key and refreshing the model worker…",
        "Verifying model model-a…",
        "Publishing the Provider route…",
        "Finalizing the Provider credential…",
    ]


def test_custom_local_provider_builds_a_unique_openai_compatible_candidate() -> None:
    client = FakeClient()

    result = _coordinator(client, FakeBridge()).onboard(
        ProviderInput(
            provider_key="other",
            credential_ref="secret:providers/local-ollama",
            model="qwen3:8b",
            project_id="project-1",
            starter_agent_name="assistant-local",
            starter_agent_display_name="Local Assistant",
            custom_provider_key="custom-local-ollama",
            custom_provider_name="Local Ollama",
            custom_protocol="openai_compatible",
            custom_base_url="http://localhost:11434/v1",
            confirmed=True,
        )
    )

    candidate = client.probes[0]["candidate"]
    assert result["provider"] == "custom-local-ollama"
    assert candidate["provider_key"] == "custom-local-ollama"
    assert candidate["protocol"] == "openai_compatible"
    assert candidate["base_url"] == "http://host.docker.internal:11434/v1"
    assert candidate["provider_options"] == {"nico_custom_display_name": "Local Ollama"}


def test_custom_provider_accepts_a_single_character_generated_slug() -> None:
    client = FakeClient()

    result = _coordinator(client, FakeBridge()).onboard(
        ProviderInput(
            provider_key="other",
            credential_ref="secret:providers/x",
            model="model-a",
            project_id="project-1",
            starter_agent_name="assistant-x",
            starter_agent_display_name="X Assistant",
            custom_provider_name="X",
            custom_protocol="openai_compatible",
            custom_base_url="https://models.example.com/v1",
            confirmed=True,
        )
    )

    assert result["provider"] == "custom-x"
    assert client.probes[0]["candidate"]["provider_key"] == "custom-x"


def test_interactive_existing_reference_is_requested_after_a_blank_hidden_key() -> None:
    client = FakeClient()
    bridge = FakeBridge()
    prompts: list[tuple[str, dict[str, Any]]] = []
    answers = iter(("", "env:NICO_MODEL_SECRET_OPENAI"))

    def prompt(message: str, **kwargs: Any) -> str:
        prompts.append((message, kwargs))
        return next(answers)

    coordinator = ProviderOnboardingCoordinator(
        client,
        Output(json_mode=True, no_color=True),
        service_bridge=bridge,
        interactive=True,
        prompt=prompt,
        confirm=lambda *_args, **_kwargs: True,
        poll_interval=0,
    )
    coordinator.onboard(
        ProviderInput(
            provider_key="openai",
            model="model-a",
            project_id="project-1",
            starter_agent_name="assistant-reference",
            starter_agent_display_name="Reference Assistant",
            confirmed=True,
        )
    )

    assert prompts[0][1] == {"hide_input": True}
    assert prompts[1] == ("Credential reference (env:NICO_MODEL_SECRET_* or secret:*)", {})
    assert bridge.begun == []
    assert client.probes[0]["candidate"]["credential_ref"] == "env:NICO_MODEL_SECRET_OPENAI"


@pytest.mark.parametrize(
    "base_url",
    ("http://localhost:not-a-port/v1", "http://user:pass@localhost:11434/v1"),
)
def test_custom_local_provider_rejects_malformed_urls_without_a_traceback(base_url: str) -> None:
    with pytest.raises(CliError) as captured:
        _coordinator(FakeClient(), FakeBridge()).onboard(
            ProviderInput(
                provider_key="other",
                credential_ref="secret:providers/local",
                model="model-a",
                project_id="project-1",
                starter_agent_name="assistant-invalid-url",
                starter_agent_display_name="Invalid URL Assistant",
                custom_provider_name="Local",
                custom_protocol="openai_compatible",
                custom_base_url=base_url,
                confirmed=True,
            )
        )

    assert captured.value.code == "PROVIDER_LOCATION_INVALID"


def test_non_interactive_flow_requires_reference_model_target_and_confirmation() -> None:
    with pytest.raises(CliError) as captured:
        _coordinator(FakeClient(), FakeBridge()).onboard(ProviderInput(provider_key="openai"))
    assert captured.value.code == "PROVIDER_INPUT_REQUIRED"


def test_local_key_renews_maintenance_while_confirmation_is_open() -> None:
    client = FakeClient()
    bridge = FakeBridge()
    coordinator = _coordinator(
        client,
        bridge,
        interactive=True,
        answers=("sk-unit-canary",),
        confirm=lambda *_args, **_kwargs: bridge.renew_event.wait(1),
        renew_interval_seconds=0.01,
    )

    result = coordinator.onboard(
        ProviderInput(
            provider_key="openai",
            model="model-a",
            project_id="project-1",
            starter_agent_name="assistant-three",
            starter_agent_display_name="Assistant Three",
        )
    )

    assert result["status"] == "ready"
    assert bridge.renewed >= 1
    assert bridge.committed == 1
    assert bridge.rolled_back == 0


def test_lease_renewal_loss_before_activation_rolls_back() -> None:
    client = FakeClient()
    bridge = FakeBridge(renew_error=True)
    coordinator = _coordinator(
        client,
        bridge,
        interactive=True,
        answers=("sk-unit-canary",),
        confirm=lambda *_args, **_kwargs: bridge.renew_event.wait(1),
        renew_interval_seconds=0.01,
    )

    with pytest.raises(CliError) as captured:
        coordinator.onboard(
            ProviderInput(
                provider_key="openai",
                model="model-a",
                project_id="project-1",
                starter_agent_name="assistant-four",
                starter_agent_display_name="Assistant Four",
            )
        )

    assert captured.value.code == "RUNTIME_MAINTENANCE_LEASE_LOST"
    assert bridge.committed == 0
    assert bridge.rolled_back == 1


def test_commit_release_failure_after_activation_preserves_published_secret() -> None:
    client = FakeClient()
    bridge = FakeBridge(commit_error=True)
    coordinator = _coordinator(
        client,
        bridge,
        interactive=True,
        answers=("key", "sk-unit-canary"),
    )

    with pytest.raises(CliError) as captured:
        coordinator.onboard(
            ProviderInput(
                provider_key="openai",
                model="model-a",
                project_id="project-1",
                starter_agent_name="assistant-five",
                starter_agent_display_name="Assistant Five",
                confirmed=True,
            )
        )

    assert captured.value.code == "RUNTIME_MAINTENANCE_LEASE_LOST"
    assert bridge.committed == 1
    assert bridge.rolled_back == 0
