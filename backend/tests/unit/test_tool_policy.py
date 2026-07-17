from __future__ import annotations

import pytest

from nico_agent.tools import ToolDefinitionSpec, ToolIsolation, ToolRetryPolicy
from nico_agent.tools.errors import ToolAccessDenied, ToolSecretUnavailable
from nico_agent.tools.policy import authorize_tool, build_tool_policy_snapshot
from nico_agent.tools.secrets import EnvironmentSecretResolver, redact_value, resolve_secrets


def _spec(*, secrets: frozenset[str] = frozenset()) -> ToolDefinitionSpec:
    return ToolDefinitionSpec(
        name="http.read",
        version="1.0.0",
        description="Read one allowlisted HTTPS resource",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        permission="network.read",
        timeout_seconds=5,
        retry_policy=ToolRetryPolicy(
            max_attempts=2,
            retryable_codes=frozenset({"TEMPORARY"}),
        ),
        isolation=ToolIsolation.NETWORK,
        secret_names=secrets,
    )


def test_policy_is_exact_intersection_and_agent_config_can_only_restrict() -> None:
    snapshot = build_tool_policy_snapshot(
        {
            "tool_policy": {
                "allow": ["http.read@1.0.0", "file.read@1.0.0"],
                "permissions": ["network.read", "filesystem.read"],
                "tools": {
                    "http.read@1.0.0": {
                        "allowed_domains": ["docs.example", "api.example"],
                        "max_bytes": 10000,
                        "follow_redirects": True,
                    }
                },
            }
        },
        {
            "allow": ["http.read@1.0.0"],
            "permissions": ["network.read"],
            "tools": {
                "http.read@1.0.0": {
                    "allowed_domains": ["docs.example"],
                    "max_bytes": 1000,
                    "follow_redirects": False,
                }
            },
        },
    )

    assert snapshot["allow"] == ["http.read@1.0.0"]
    assert snapshot["permissions"] == ["network.read"]
    authorization = authorize_tool(snapshot, _spec())
    assert authorization.tool_config == {
        "allowed_domains": ["docs.example"],
        "max_bytes": 1000,
        "follow_redirects": False,
    }


def test_missing_wildcard_and_unavailable_plugin_layers_fail_closed() -> None:
    spec = _spec()
    for snapshot in (
        build_tool_policy_snapshot({}, {}),
        build_tool_policy_snapshot(
            {"tool_policy": {"allow": ["*"], "permissions": ["*"]}},
            {"allow": ["http.read@1.0.0"], "permissions": ["network.read"]},
        ),
        build_tool_policy_snapshot(
            {
                "tool_policy": {
                    "allow": ["http.read@1.0.0"],
                    "permissions": ["network.read"],
                }
            },
            {"allow": ["http.read@1.0.0"], "permissions": ["network.read"]},
            plugin_refs=[{"name": "not-yet-available"}],
        ),
    ):
        with pytest.raises(ToolAccessDenied):
            authorize_tool(snapshot, spec)


def test_secret_refs_are_intersected_and_only_tool_prefixed_environment_is_resolved() -> None:
    spec = _spec(secrets=frozenset({"authorization"}))
    snapshot = build_tool_policy_snapshot(
        {
            "tool_policy": {
                "allow": [spec.reference],
                "permissions": [spec.permission],
                "secret_refs": {
                    "authorization": "env:NICO_TOOL_SECRET_HTTP_AUTH",
                    "tenant_only": "env:NICO_TOOL_SECRET_TENANT_ONLY",
                },
            }
        },
        {
            "allow": [spec.reference],
            "permissions": [spec.permission],
            "secrets": ["authorization"],
        },
    )
    authorization = authorize_tool(snapshot, spec)
    assert authorization.secret_refs == {"authorization": "env:NICO_TOOL_SECRET_HTTP_AUTH"}
    secrets = resolve_secrets(
        EnvironmentSecretResolver({"NICO_TOOL_SECRET_HTTP_AUTH": "very-secret-value"}),
        authorization.secret_refs,
    )
    assert secrets == {"authorization": "very-secret-value"}

    with pytest.raises(ToolSecretUnavailable):
        EnvironmentSecretResolver({"PATH": "should-never-be-readable"}).resolve(
            "authorization", "env:PATH"
        )


def test_redaction_covers_sensitive_keys_discovered_values_and_inline_patterns() -> None:
    value = {
        "token": "literal-token",
        "nested": [
            "the secret is very-secret-value",
            "api_key=another-secret",
        ],
    }

    redacted = redact_value(value, {"authorization": "very-secret-value"})

    assert redacted["token"] == "[REDACTED]"
    assert "very-secret-value" not in redacted["nested"][0]
    assert redacted["nested"][1] == "[REDACTED]"
