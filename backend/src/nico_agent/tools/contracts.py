"""Frozen Tool Gateway DTOs independent from persistence and executors."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nico_agent.tools.errors import ToolDefinitionInvalid, ToolSchemaViolation

_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_PERMISSION = re.compile(r"^[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")


class ToolRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolIsolation(StrEnum):
    IN_PROCESS = "in_process"
    WORKSPACE = "workspace"
    NETWORK = "network"
    READ_ONLY_DATABASE = "read_only_database"
    CONTAINER = "container"


class ToolRetryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_attempts: int = Field(default=1, ge=1, le=5)
    backoff_seconds: float = Field(default=0, ge=0, le=30)
    retryable_codes: frozenset[str] = Field(default_factory=frozenset)

    @field_validator("retryable_codes")
    @classmethod
    def validate_codes(cls, value: frozenset[str]) -> frozenset[str]:
        if len(value) > 20 or any(not _ERROR_CODE.fullmatch(item) for item in value):
            raise ValueError("retryable_codes must contain at most 20 stable codes")
        return value


class ToolDefinitionSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=5, max_length=50)
    description: str = Field(min_length=1, max_length=2000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    permission: str = Field(min_length=1, max_length=150)
    timeout_seconds: int = Field(gt=0, le=300)
    retry_policy: ToolRetryPolicy = Field(default_factory=ToolRetryPolicy)
    isolation: ToolIsolation
    risk: ToolRisk = ToolRisk.LOW
    max_output_bytes: int = Field(default=1_048_576, ge=1, le=10_485_760)
    secret_names: frozenset[str] = Field(default_factory=frozenset)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("tool name must be a lowercase dotted identifier")
        return value

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not _SEMVER.fullmatch(value):
            raise ValueError("tool version must be semantic version syntax")
        return value

    @field_validator("permission")
    @classmethod
    def validate_permission(cls, value: str) -> str:
        if not _PERMISSION.fullmatch(value):
            raise ValueError("tool permission must be a stable lowercase code")
        return value

    @field_validator("secret_names")
    @classmethod
    def validate_secret_names(cls, value: frozenset[str]) -> frozenset[str]:
        if len(value) > 20 or any(not _PERMISSION.fullmatch(item) for item in value):
            raise ValueError("secret_names must contain at most 20 stable names")
        return value

    @model_validator(mode="after")
    def validate_schemas(self) -> ToolDefinitionSpec:
        _check_schema(self.input_schema, kind="input")
        _check_schema(self.output_schema, kind="output")
        if self.input_schema.get("type") != "object":
            raise ValueError("tool input_schema must describe an object")
        if self.output_schema.get("type") != "object":
            raise ValueError("tool output_schema must describe an object")
        return self

    @property
    def reference(self) -> str:
        return f"{self.name}@{self.version}"

    @property
    def content_hash(self) -> str:
        payload = self.model_dump(mode="json")
        payload["secret_names"] = sorted(self.secret_names)
        payload["retry_policy"]["retryable_codes"] = sorted(self.retry_policy.retryable_codes)
        return canonical_hash(payload)

    def validate_input(self, value: dict[str, Any]) -> None:
        _validate_instance(self.input_schema, value, code="TOOL_INPUT_INVALID")

    def validate_output(self, value: dict[str, Any]) -> None:
        _validate_instance(self.output_schema, value, code="TOOL_OUTPUT_INVALID")
        encoded = canonical_json(value).encode()
        if len(encoded) > self.max_output_bytes:
            raise ToolSchemaViolation(
                code="TOOL_OUTPUT_TOO_LARGE",
                message="tool output exceeds the configured byte limit",
                path=[],
                validator="max_output_bytes",
            )


class ToolExecutionContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    run_id: UUID
    run_step_id: UUID
    actor_id: str = Field(min_length=1, max_length=200)
    correlation_id: UUID
    tool_config: dict[str, Any] = Field(default_factory=dict)


class ToolExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    output: dict[str, Any]
    usage: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class ToolExecutor(Protocol):
    @property
    def spec(self) -> ToolDefinitionSpec: ...

    @property
    def implementation_hash(self) -> str: ...

    async def execute(
        self,
        context: ToolExecutionContext,
        arguments: dict[str, Any],
        secrets: dict[str, str],
    ) -> ToolExecutionResult: ...


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _check_schema(schema: dict[str, Any], *, kind: str) -> None:
    if _contains_reference(schema):
        raise ValueError(f"tool {kind}_schema cannot contain JSON Schema references")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"tool {kind}_schema is invalid") from exc


def _contains_reference(value: Any) -> bool:
    if isinstance(value, dict):
        if "$ref" in value or "$dynamicRef" in value:
            return True
        return any(_contains_reference(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_reference(item) for item in value)
    return False


def _validate_instance(schema: dict[str, Any], value: dict[str, Any], *, code: str) -> None:
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as exc:
        raise ToolSchemaViolation(
            code=code,
            message="tool value does not satisfy its JSON Schema",
            path=list(exc.absolute_path),
            validator=exc.validator,
        ) from exc


def ensure_definition(spec: ToolDefinitionSpec) -> ToolDefinitionSpec:
    """Translate unexpected schema failures to the stable tool error family."""

    try:
        _check_schema(spec.input_schema, kind="input")
        _check_schema(spec.output_schema, kind="output")
    except ValueError as exc:
        raise ToolDefinitionInvalid(str(exc)) from exc
    return spec
