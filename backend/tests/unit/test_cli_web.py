from __future__ import annotations

from typing import Any

import pytest

from nico_agent.cli.errors import CliError
from nico_agent.cli.output import Output
from nico_agent.cli.service_bridge import SecretAttempt
from nico_agent.cli.web import WebConfigureInput, WebCoordinator


class FakeClient:
    def __init__(self, *, writes_enabled: bool = True) -> None:
        self.writes_enabled = writes_enabled
        self.calls: list[str] = []
        self.candidate: dict[str, Any] | None = None
        self.activated = 0
        self.disabled = 0
        self.status_value: dict[str, Any] = {
            "writes_enabled": writes_enabled,
            "tenant_revision": 4,
            "configured": True,
            "enabled": True,
            "authorized": True,
            "provider": "brave",
            "credential_ref": "env:NICO_TOOL_SECRET_WEB_SEARCH_BRAVE_EXISTING",
            "secret_required": True,
            "diagnosis": "ready",
            "latest_probe": {"status": "succeeded"},
            "agents": [{"id": "agent-1", "name": "agent", "revision": 7}],
        }

    def web_setup_readiness(self):
        self.calls.append("readiness")
        return {
            "writes_enabled": self.writes_enabled,
            "reason": "ready" if self.writes_enabled else "deployment_policy_disabled",
            "tenant_revision": 4,
        }

    def web_catalog(self):
        self.calls.append("catalog")
        return {
            "schema_version": 1,
            "catalog_revision": "catalog-1",
            "dns_resolvers": [
                {
                    "key": "system",
                    "label": "System DNS",
                    "description": "Use the operating system resolver.",
                    "recommended": False,
                },
                {
                    "key": "cloudflare",
                    "label": "Cloudflare DNS",
                    "description": "Use DNS-over-HTTPS; compatible with Fake-IP.",
                    "recommended": True,
                },
                {
                    "key": "google",
                    "label": "Google Public DNS",
                    "description": "Use Google DNS-over-HTTPS.",
                    "recommended": False,
                },
            ],
            "providers": [
                {
                    "key": "brave",
                    "display_name": "Brave Search",
                    "requires_secret": True,
                    "endpoints": [{"key": "managed", "default": True}],
                },
                {
                    "key": "searxng",
                    "display_name": "SearXNG",
                    "requires_secret": False,
                    "endpoints": [{"key": "configured", "default": True}],
                },
            ],
        }

    def create_web_probe(self, *, candidate, idempotency_key):
        del idempotency_key
        self.calls.append("probe")
        self.candidate = candidate
        return {
            "id": "probe-1",
            "status": "succeeded",
            "candidate_hash": "a" * 64,
            "verified_at": "2026-07-22T00:00:00Z",
        }

    def get_web_probe(self, probe_id):
        raise AssertionError(f"completed probe {probe_id} must not be polled")

    def preview_web_activation(self, *, probe_id, candidate_hash, target):
        self.calls.append("preview")
        return {
            "probe_id": probe_id,
            "candidate_hash": candidate_hash,
            "preview_hash": "b" * 64,
            "changed_fields": ["agent_version"],
            "projection": {
                "agent": {"id": "agent-1", "name": "agent"},
                "agent_version": {"version": 2},
                "target": target,
            },
        }

    def activate_web(self, **kwargs):
        self.calls.append("activate")
        self.activated += 1
        return {"agent_id": "agent-1", "agent_version": 2, "request": kwargs}

    def web_status(self):
        self.calls.append("status")
        return {**self.status_value}

    def test_web_configuration(self, *, idempotency_key):
        del idempotency_key
        self.calls.append("test")
        return {
            "id": "probe-test",
            "status": "succeeded",
            "candidate_hash": "c" * 64,
            "verified_at": "2026-07-22T00:00:00Z",
        }

    def preview_web_disable(self, *, target):
        self.calls.append("disable-preview")
        return {
            "preview_hash": "d" * 64,
            "changed_fields": ["agent_version"],
            "projection": {
                "agent": {"id": target["agent_id"], "name": "agent"},
                "agent_version": {"version": 3},
            },
        }

    def disable_web(self, *, target, preview_hash):
        self.calls.append("disable")
        self.disabled += 1
        return {"agent_id": target["agent_id"], "preview_hash": preview_hash}


class FakeBridge:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.recovered = 0
        self.begun: list[tuple[str, str]] = []
        self.committed = 0
        self.rolled_back = 0

    def recover_credentials(self):
        self.recovered += 1

    def begin_credential(self, env_name, secret):
        self.begun.append((env_name, secret))
        return SecretAttempt(
            "attempt-1",
            "token-" + "x" * 32,
            f"env:{env_name}",
            command="credential-secret",
        )

    def renew(self, _attempt):
        return None

    def commit(self, _attempt):
        self.committed += 1

    def rollback(self, _attempt):
        self.rolled_back += 1

    def credential_available(self, _credential_ref):
        return self.available


def _coordinator(
    client: FakeClient,
    bridge: FakeBridge | None = None,
    *,
    interactive: bool = False,
    answers: tuple[str, ...] = (),
    confirmed: bool = True,
) -> WebCoordinator:
    iterator = iter(answers)
    return WebCoordinator(
        client,  # type: ignore[arg-type]
        Output(json_mode=True, no_color=True),
        service_bridge=bridge,  # type: ignore[arg-type]
        interactive=interactive,
        prompt=lambda *_args, **_kwargs: next(iterator),
        confirm=lambda *_args, **_kwargs: confirmed,
        poll_interval=0,
    )


def _starter(provider: str, *, credential_ref: str | None = None) -> WebConfigureInput:
    return WebConfigureInput(
        provider=provider,
        credential_ref=credential_ref,
        project_id="project-1",
        starter_agent_name="web-agent",
        starter_agent_display_name="Web Agent",
        confirmed=True,
    )


def test_deployment_preflight_happens_before_catalog_prompt_or_secret() -> None:
    client = FakeClient(writes_enabled=False)
    bridge = FakeBridge()

    with pytest.raises(CliError) as captured:
        _coordinator(client, bridge, interactive=True, answers=("brave",)).configure(
            WebConfigureInput()
        )

    assert captured.value.code == "WEB_PROVIDER_WRITES_DISABLED"
    assert client.calls == ["readiness"]
    assert bridge.recovered == 0
    assert bridge.begun == []


def test_hidden_brave_key_uses_local_transaction_and_never_enters_api_candidate() -> None:
    client = FakeClient()
    bridge = FakeBridge()
    prompts: list[tuple[str, dict[str, Any]]] = []

    def prompt(message: str, **kwargs: Any) -> str:
        prompts.append((message, kwargs))
        if message.startswith("DNS resolver"):
            return "cloudflare"
        return "brave-canary"

    coordinator = _coordinator(client, bridge, interactive=True)
    coordinator.prompt = prompt
    result = coordinator.configure(_starter("brave"))

    assert result["status"] == "ready"
    assert next(options for message, options in prompts if "API key" in message) == {
        "hide_input": True
    }
    assert bridge.recovered == 1
    assert bridge.committed == 1
    assert bridge.rolled_back == 0
    assert client.candidate is not None
    assert client.candidate["credential_ref"].startswith("env:NICO_TOOL_SECRET_")
    assert "brave-canary" not in str(client.candidate)


def test_noninteractive_searxng_configuration_needs_no_fake_secret() -> None:
    client = FakeClient()

    result = _coordinator(client).configure(_starter("searxng"))

    assert result["provider"] == "searxng"
    assert client.candidate is not None
    assert client.candidate["credential_ref"] is None
    assert client.candidate["policy"]["dns_resolver"] == "system"


def test_interactive_web_setup_selects_recommended_doh_resolver() -> None:
    client = FakeClient()

    result = _coordinator(client, interactive=True, answers=("2",)).configure(_starter("searxng"))

    assert result["provider"] == "searxng"
    assert client.candidate is not None
    assert client.candidate["policy"]["dns_resolver"] == "cloudflare"


def test_web_test_probes_without_publishing() -> None:
    client = FakeClient()

    result = _coordinator(client).test()

    assert result["status"] == "succeeded"
    assert client.activated == 0
    assert client.calls == ["readiness", "test"]


@pytest.mark.parametrize(
    ("server_diagnosis", "secret_available", "expected"),
    [
        ("unconfigured", True, "unconfigured"),
        ("unauthorized", True, "unauthorized"),
        ("provider_unreachable", True, "provider_unreachable"),
        ("recent_probe_failed", True, "recent_probe_failed"),
        ("ready", False, "secret_unavailable"),
    ],
)
def test_status_preserves_actionable_diagnostic_categories(
    server_diagnosis: str,
    secret_available: bool,
    expected: str,
) -> None:
    client = FakeClient()
    client.status_value["diagnosis"] = server_diagnosis

    status = _coordinator(client, FakeBridge(available=secret_available)).status()

    assert status["diagnosis"] == expected
    assert "NICO_TOOL_SECRET" in status["credential_ref"]


def test_disable_previews_and_publishes_new_agent_version() -> None:
    client = FakeClient()

    result = _coordinator(client).disable(agent_id="agent-1", confirmed=True)

    assert result["status"] == "disabled"
    assert client.disabled == 1
    assert client.calls == ["readiness", "status", "disable-preview", "disable"]
