from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest
from pydantic import ValidationError

from nico_agent.tools import (
    ToolDefinitionSpec,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolGateway,
    ToolIsolation,
    ToolRegistry,
    ToolRetryPolicy,
    ToolRisk,
    executor_required_secret_names,
)
from nico_agent.tools.contracts import canonical_json
from nico_agent.tools.errors import (
    ToolExecutorFailure,
    ToolNotFound,
    ToolRegistryConflict,
    ToolSchemaViolation,
)


def _spec(**overrides) -> ToolDefinitionSpec:
    values = {
        "name": "file.read",
        "version": "1.0.0",
        "description": "Read one file from the scoped Run workspace",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "minLength": 1}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
            "additionalProperties": False,
        },
        "permission": "filesystem.read",
        "timeout_seconds": 5,
        "retry_policy": ToolRetryPolicy(max_attempts=1),
        "isolation": ToolIsolation.WORKSPACE,
        "risk": ToolRisk.LOW,
        "max_output_bytes": 1024,
    }
    values.update(overrides)
    return ToolDefinitionSpec(**values)


@dataclass
class _Executor:
    spec: ToolDefinitionSpec
    implementation_hash: str = "a" * 64

    async def execute(self, context, arguments, secrets):
        return ToolExecutionResult(output={"content": arguments["path"]})


def test_definition_is_canonical_and_validates_input_and_output_without_values_in_errors() -> None:
    first = _spec()
    second = _spec(
        input_schema={
            "required": ["path"],
            "properties": {"path": {"minLength": 1, "type": "string"}},
            "additionalProperties": False,
            "type": "object",
        }
    )

    assert first.reference == "file.read@1.0.0"
    assert first.content_hash == second.content_hash
    first.validate_input({"path": "report.md"})
    first.validate_output({"content": "ok"})

    with pytest.raises(ToolSchemaViolation) as captured:
        first.validate_input({"path": 12345})
    assert captured.value.code == "TOOL_INPUT_INVALID"
    assert captured.value.details == {"path": ["path"], "validator": "type"}
    assert "12345" not in captured.value.message


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "File Read"},
        {"version": "latest"},
        {"permission": "Filesystem Read"},
        {"input_schema": {"type": "array"}},
        {"input_schema": {"type": "object", "$ref": "https://example.test/schema"}},
        {"output_schema": {"type": "object", "properties": {"x": {"type": "wat"}}}},
        {"timeout_seconds": 0},
    ],
)
def test_definition_rejects_ambiguous_or_unsafe_contracts(overrides) -> None:
    with pytest.raises(ValidationError):
        _spec(**overrides)


def test_output_limit_is_enforced_after_schema_validation() -> None:
    spec = _spec(max_output_bytes=30)

    with pytest.raises(ToolSchemaViolation) as captured:
        spec.validate_output({"content": "x" * 100})

    assert captured.value.code == "TOOL_OUTPUT_TOO_LARGE"
    assert captured.value.details["validator"] == "max_output_bytes"


def test_registry_uses_exact_versions_and_fails_closed_on_duplicates() -> None:
    one = _Executor(_spec(version="1.0.0"))
    two = _Executor(_spec(version="2.0.0"))
    registry = ToolRegistry([two, one])

    assert registry.get("file.read", "1.0.0") is one
    assert [item.reference for item in registry.definitions()] == [
        "file.read@1.0.0",
        "file.read@2.0.0",
    ]

    with pytest.raises(ToolNotFound):
        registry.get("file.read", "3.0.0")
    with pytest.raises(ToolRegistryConflict):
        registry.register(_Executor(_spec(version="1.0.0")))


def test_executor_context_contains_only_scoped_identity() -> None:
    _context = ToolExecutionContext(
        tenant_id=uuid4(),
        run_id=uuid4(),
        run_step_id=uuid4(),
        actor_id="worker:test",
        correlation_id=uuid4(),
    )

    assert "database" not in ToolExecutionContext.model_fields
    assert "secret" not in ToolExecutionContext.model_fields


def test_legacy_executor_defaults_to_all_declared_secrets() -> None:
    executor = _Executor(_spec(secret_names=frozenset({"authorization"})))

    assert executor_required_secret_names(executor, {"provider": "legacy"}) == frozenset(
        {"authorization"}
    )


def test_canonical_json_rejects_non_finite_or_non_json_values() -> None:
    with pytest.raises(ValueError):
        canonical_json({"value": float("nan")})
    with pytest.raises(TypeError):
        canonical_json({"value": {1, 2}})


def test_executor_retry_override_can_fail_closed_without_widening_policy() -> None:
    spec = _spec(
        retry_policy=ToolRetryPolicy(
            max_attempts=2,
            backoff_seconds=0.2,
            retryable_codes=frozenset({"TEMPORARY"}),
        )
    )
    local_limit = ToolExecutorFailure("TEMPORARY", "limited", retryable=False)
    provider_limit = ToolExecutorFailure(
        "TEMPORARY",
        "limited",
        retryable=True,
        retry_after_seconds=7,
    )

    assert (
        ToolGateway._should_retry(spec, local_limit.code, 1, retryable=local_limit.retryable)
        is False
    )
    assert (
        ToolGateway._should_retry(spec, provider_limit.code, 1, retryable=provider_limit.retryable)
        is True
    )
    assert provider_limit.retry_after_seconds == 7
    assert ToolGateway._should_retry(spec, "NOT_DECLARED", 1, retryable=True) is False
