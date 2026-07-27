"""Public, persistence-neutral contracts for Tool Provider Protocol v1."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from nico_agent.domain.states import (
    ExternalToolProviderStatus,
    RunToolBindingStatus,
)
from nico_agent.tool_providers.security import (
    PROTOCOL_IDENTIFIER,
    PROTOCOL_VERSION,
    validate_provider_endpoint,
)

_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,299}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
_ERROR_CATEGORY = re.compile(r"^[a-z][a-z0-9_-]{0,49}$")
_CREDENTIAL_REF = re.compile(r"^env:NICO_TOOL_SECRET_[A-Z0-9_]{1,100}$")
_SENSITIVE_METADATA_KEY = re.compile(
    r"(?i)(?:api[_-]?key|authorization|cookie|password|private[_-]?key|secret|token)"
)
_SENSITIVE_TEXT = re.compile(
    r"(?i)(?:(?:api[_-]?key|authorization|cookie|password|secret|token)"
    r"\s*[=:]\s*)[^\s,;]+"
)


class FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ToolBindingApprovalMode(StrEnum):
    ALWAYS = "always"
    NEVER = "never"
    INHERIT = "inherit"


class ProviderHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class ProviderToolCallStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ProviderCancelStatus(StrEnum):
    CANCELLED = "cancelled"
    ALREADY_FINISHED = "already_finished"


class ExternalToolProviderCreate(FrozenContract):
    name: str = Field(min_length=1, max_length=120)
    project_id: UUID | None = None
    protocol: Literal["nico-tool-provider-v1"] = PROTOCOL_IDENTIFIER
    endpoint_ref: str = Field(
        min_length=1,
        max_length=2000,
        validation_alias=AliasChoices("endpoint_ref", "endpoint_url"),
    )
    credential_ref: str = Field(min_length=1, max_length=300, repr=False)
    expires_at: datetime | None = None
    metadata: dict[str, str | int | bool] = Field(default_factory=dict, max_length=50)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("Provider name must be a lowercase stable identifier")
        return value

    @field_validator("endpoint_ref")
    @classmethod
    def validate_endpoint_ref(cls, value: str) -> str:
        # Syntax/canonicalization belongs in the public DTO. Deployment policy
        # still decides whether this endpoint may use HTTP.
        return validate_provider_endpoint(value, allow_http=True)

    @property
    def endpoint_url(self) -> str:
        """Compatibility name for transports which operate on resolved URLs."""

        return self.endpoint_ref

    @field_validator("credential_ref")
    @classmethod
    def validate_credential_ref(cls, value: str) -> str:
        if not _CREDENTIAL_REF.fullmatch(value):
            raise ValueError("credential_ref must be an env:NICO_TOOL_SECRET_* reference")
        return value

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime | None) -> datetime | None:
        return _aware_utc(value, field_name="Provider expiry")

    @field_validator("metadata")
    @classmethod
    def validate_metadata(
        cls,
        value: dict[str, str | int | bool],
    ) -> dict[str, str | int | bool]:
        for key, item in value.items():
            if not key or len(key) > 100 or _SENSITIVE_METADATA_KEY.search(key):
                raise ValueError("Provider metadata keys must be safe and non-sensitive")
            if isinstance(item, str):
                _safe_text(item, maximum=500, field_name="Provider metadata value")
        if len(canonical_json_bytes(value)) > 16_384:
            raise ValueError("Provider metadata exceeds the size limit")
        return value


class ExternalToolProviderRead(FrozenContract):
    id: UUID
    tenant_id: UUID
    project_id: UUID | None = None
    name: str
    protocol: str
    endpoint_ref: str = Field(
        validation_alias=AliasChoices("endpoint_ref", "endpoint_url"),
    )
    endpoint_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    credential_ref: str = Field(repr=False)
    status: ExternalToolProviderStatus
    capability_snapshot: dict[str, Any] = Field(default_factory=dict)
    capability_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    verified_at: datetime | None = None
    expires_at: datetime | None = None
    revision: int = Field(ge=1)
    metadata: dict[str, str | int | bool] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    @field_validator("verified_at", "expires_at", "created_at", "updated_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None) -> datetime | None:
        return _aware_utc(value, field_name="Provider timestamp")

    @property
    def endpoint_url(self) -> str:
        return self.endpoint_ref


class ProviderHealthResponse(FrozenContract):
    protocol_version: Literal["1"] = PROTOCOL_VERSION
    provider_id: str = Field(min_length=1, max_length=300)
    status: ProviderHealthStatus
    time: datetime

    @field_validator("provider_id")
    @classmethod
    def validate_provider_id(cls, value: str) -> str:
        return _validate_opaque_id(value, field_name="provider_id")

    @field_validator("time")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return _aware_utc(value, field_name="Provider health time")


class ProviderToolIdentity(FrozenContract):
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=5, max_length=50)

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
            raise ValueError("tool version must use exact semantic version syntax")
        return value

    @property
    def reference(self) -> str:
        return f"{self.name}@{self.version}"


class ProviderToolContract(ProviderToolIdentity):
    input_schema_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    output_schema_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ProviderFeatureSet(FrozenContract):
    cancellation: bool = False
    idempotency: Literal[True] = True
    streaming: Literal[False] = False
    healthcheck: Literal[True] = True


class ProviderCapabilitiesResponse(FrozenContract):
    protocol_version: Literal["1"] = PROTOCOL_VERSION
    provider_id: str = Field(min_length=1, max_length=300)
    supported_tools: tuple[ProviderToolContract, ...] = Field(max_length=500)
    features: ProviderFeatureSet

    @field_validator("provider_id")
    @classmethod
    def validate_provider_id(cls, value: str) -> str:
        return _validate_opaque_id(value, field_name="provider_id")

    @model_validator(mode="after")
    def validate_unique_tools(self) -> ProviderCapabilitiesResponse:
        references = [tool.reference for tool in self.supported_tools]
        if len(references) != len(set(references)):
            raise ValueError("Provider capabilities cannot repeat an exact Tool")
        return self

    @property
    def capability_digest(self) -> str:
        return canonical_digest(self)


class ProviderTrace(FrozenContract):
    trace_id: str = Field(min_length=1, max_length=300)
    parent_event_id: str | None = Field(default=None, min_length=1, max_length=300)

    @field_validator("trace_id", "parent_event_id")
    @classmethod
    def validate_ids(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_opaque_id(value, field_name="trace identity")


class ProviderToolCallRequest(FrozenContract):
    protocol_version: Literal["1"] = PROTOCOL_VERSION
    provider_id: str = Field(min_length=1, max_length=300)
    binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    request_id: str = Field(min_length=1, max_length=300)
    tool_call_id: str = Field(min_length=1, max_length=300)
    idempotency_key: str = Field(min_length=1, max_length=300)
    tenant_id: UUID
    project_id: UUID | None = None
    run_id: UUID
    task_id: UUID | None = None
    agent_id: UUID
    agent_version_id: UUID
    tool: ProviderToolContract
    arguments: dict[str, Any]
    deadline: datetime
    attempt: int = Field(ge=1)
    trace: ProviderTrace

    @field_validator("provider_id", "request_id", "tool_call_id", "idempotency_key")
    @classmethod
    def validate_opaque_ids(cls, value: str) -> str:
        return _validate_opaque_id(value, field_name="Provider request identity")

    @field_validator("deadline")
    @classmethod
    def validate_deadline(cls, value: datetime) -> datetime:
        return _aware_utc(value, field_name="Provider request deadline")

    @field_validator("arguments")
    @classmethod
    def validate_argument_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(canonical_json_bytes(value)) > 1_048_576:
            raise ValueError("Provider request arguments exceed the protocol size limit")
        return value

    @property
    def request_digest(self) -> str:
        """Digest stable across retries; attempt intentionally does not affect idempotency."""

        return canonical_digest(self.model_dump(mode="json", exclude={"attempt"}))


class ProviderToolError(FrozenContract):
    code: str = Field(min_length=1, max_length=100)
    category: str = Field(min_length=1, max_length=50)
    retryable: bool
    message: str = Field(min_length=1, max_length=1000)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if not _ERROR_CODE.fullmatch(value):
            raise ValueError("Provider error code must be a stable uppercase code")
        return value

    @field_validator("category")
    @classmethod
    def validate_category(cls, value: str) -> str:
        if not _ERROR_CATEGORY.fullmatch(value):
            raise ValueError("Provider error category must be a stable lowercase code")
        return value

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        safe = _safe_text(value, maximum=1000, field_name="Provider error message")
        return _SENSITIVE_TEXT.sub("[REDACTED]", safe)


class ProviderToolCallResponse(FrozenContract):
    protocol_version: Literal["1"] = PROTOCOL_VERSION
    provider_id: str = Field(min_length=1, max_length=300)
    binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    request_id: str = Field(min_length=1, max_length=300)
    tool_call_id: str = Field(min_length=1, max_length=300)
    tool: ProviderToolIdentity
    status: ProviderToolCallStatus
    result: dict[str, Any] | None = None
    error: ProviderToolError | None = None
    started_at: datetime
    finished_at: datetime
    provider_execution_id: str = Field(min_length=1, max_length=300)
    idempotency_replayed: bool = False

    @field_validator("provider_id", "request_id", "tool_call_id", "provider_execution_id")
    @classmethod
    def validate_opaque_ids(cls, value: str) -> str:
        return _validate_opaque_id(value, field_name="Provider response identity")

    @field_validator("started_at", "finished_at")
    @classmethod
    def validate_timestamps(cls, value: datetime) -> datetime:
        return _aware_utc(value, field_name="Provider execution timestamp")

    @model_validator(mode="after")
    def validate_envelope(self) -> ProviderToolCallResponse:
        if self.started_at > self.finished_at:
            raise ValueError("Provider execution timestamps are out of order")
        if self.status == ProviderToolCallStatus.SUCCEEDED:
            if self.result is None or self.error is not None:
                raise ValueError("succeeded Provider response requires result and no error")
        elif self.result is not None or self.error is None:
            raise ValueError("non-success Provider response requires error and no result")
        if self.result is not None and len(canonical_json_bytes(self.result)) > 10_485_760:
            raise ValueError("Provider response result exceeds the protocol hard limit")
        return self


class ProviderCancelRequest(FrozenContract):
    protocol_version: Literal["1"] = PROTOCOL_VERSION
    provider_id: str = Field(min_length=1, max_length=300)
    binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    request_id: str = Field(min_length=1, max_length=300)
    tenant_id: UUID
    project_id: UUID | None = None
    run_id: UUID
    reason: Literal["run_cancelled", "run_timed_out", "tool_timed_out", "worker_shutdown"]
    deadline: datetime

    @field_validator("provider_id", "request_id")
    @classmethod
    def validate_opaque_ids(cls, value: str) -> str:
        return _validate_opaque_id(value, field_name="Provider cancellation identity")

    @field_validator("deadline")
    @classmethod
    def validate_deadline(cls, value: datetime) -> datetime:
        return _aware_utc(value, field_name="Provider cancellation deadline")


class ProviderCancelResponse(FrozenContract):
    protocol_version: Literal["1"] = PROTOCOL_VERSION
    provider_id: str = Field(min_length=1, max_length=300)
    request_id: str = Field(min_length=1, max_length=300)
    status: ProviderCancelStatus
    provider_execution_id: str | None = Field(default=None, min_length=1, max_length=300)
    acknowledged_at: datetime
    side_effects_may_continue: bool

    @field_validator("provider_id", "request_id", "provider_execution_id")
    @classmethod
    def validate_opaque_ids(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_opaque_id(value, field_name="Provider cancellation identity")

    @field_validator("acknowledged_at")
    @classmethod
    def validate_acknowledged_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, field_name="Provider cancellation acknowledgment")


class RunToolBindingBudget(FrozenContract):
    max_calls: int = Field(ge=1, le=100_000)
    max_total_duration_ms: int = Field(ge=1, le=86_400_000)
    max_single_call_duration_ms: int = Field(ge=1, le=300_000)
    max_retries: int = Field(default=0, ge=0, le=4)

    @model_validator(mode="after")
    def validate_durations(self) -> RunToolBindingBudget:
        if self.max_single_call_duration_ms > self.max_total_duration_ms:
            raise ValueError("single-call duration cannot exceed total binding duration")
        return self


class ToolBindingRetryPolicy(FrozenContract):
    max_attempts: int = Field(default=1, ge=1, le=5)


class PublicRunToolBindingPolicy(FrozenContract):
    """Stable Run Create shape; durations are seconds at the public boundary."""

    timeout_seconds: int = Field(gt=0, le=300)
    max_calls: int = Field(ge=1, le=100_000)
    max_total_duration: int | None = Field(default=None, ge=1, le=86_400)
    max_single_call_duration: int | None = Field(default=None, ge=1, le=300)
    retry: ToolBindingRetryPolicy = Field(default_factory=ToolBindingRetryPolicy)
    approval_mode: ToolBindingApprovalMode = ToolBindingApprovalMode.INHERIT
    cancel_timeout_ms: int = Field(default=5_000, ge=100, le=30_000)
    max_response_bytes: int = Field(default=1_048_576, ge=1, le=10_485_760)

    @model_validator(mode="after")
    def resolve_duration_defaults(self) -> PublicRunToolBindingPolicy:
        single = self.max_single_call_duration or self.timeout_seconds
        total = self.max_total_duration or min(single * self.max_calls, 86_400)
        if single > self.timeout_seconds:
            raise ValueError("single-call duration cannot exceed timeout_seconds")
        if single > total:
            raise ValueError("single-call duration cannot exceed total binding duration")
        object.__setattr__(self, "max_single_call_duration", single)
        object.__setattr__(self, "max_total_duration", total)
        return self

    def to_frozen_policy(self) -> RunToolBindingPolicy:
        assert self.max_total_duration is not None
        assert self.max_single_call_duration is not None
        return RunToolBindingPolicy(
            approval=self.approval_mode,
            budget=RunToolBindingBudget(
                max_calls=self.max_calls,
                max_total_duration_ms=self.max_total_duration * 1000,
                max_single_call_duration_ms=self.max_single_call_duration * 1000,
                max_retries=self.retry.max_attempts - 1,
            ),
            provider_request_timeout_ms=self.timeout_seconds * 1000,
            cancel_timeout_ms=self.cancel_timeout_ms,
            max_response_bytes=self.max_response_bytes,
        )


class RunToolBindingPolicy(FrozenContract):
    approval: ToolBindingApprovalMode = ToolBindingApprovalMode.INHERIT
    budget: RunToolBindingBudget
    provider_request_timeout_ms: int = Field(ge=1, le=300_000)
    cancel_timeout_ms: int = Field(default=5_000, ge=100, le=30_000)
    max_response_bytes: int = Field(default=1_048_576, ge=1, le=10_485_760)

    @model_validator(mode="after")
    def validate_timeout_budget(self) -> RunToolBindingPolicy:
        if self.provider_request_timeout_ms > self.budget.max_single_call_duration_ms:
            raise ValueError("Provider request timeout cannot exceed the single-call budget")
        return self


class RunToolBindingCreate(FrozenContract):
    provider_id: UUID
    tool: ProviderToolIdentity
    policy: PublicRunToolBindingPolicy
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime | None) -> datetime | None:
        return _aware_utc(value, field_name="Tool binding expiry")


class RunToolBindingScope(FrozenContract):
    tenant_id: UUID
    project_id: UUID | None = None
    run_id: UUID
    task_id: UUID | None = None
    agent_id: UUID
    agent_version_id: UUID


class RunToolBindingSnapshot(FrozenContract):
    schema_version: Literal[1] = 1
    binding_id: UUID
    provider_id: UUID
    provider_name: str = Field(min_length=1, max_length=120)
    protocol: Literal["nico-tool-provider-v1"] = PROTOCOL_IDENTIFIER
    endpoint_identity: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    credential_ref: str = Field(min_length=1, max_length=300, repr=False)
    scope: RunToolBindingScope
    tool: ProviderToolContract
    capability_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    policy: RunToolBindingPolicy
    provider_expires_at: datetime | None = None
    binding_expires_at: datetime | None = None
    frozen_at: datetime
    binding_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("provider_name")
    @classmethod
    def validate_provider_name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("Provider name must be a lowercase stable identifier")
        return value

    @field_validator("credential_ref")
    @classmethod
    def validate_credential_ref(cls, value: str) -> str:
        if not _CREDENTIAL_REF.fullmatch(value):
            raise ValueError("credential_ref must be an env:NICO_TOOL_SECRET_* reference")
        return value

    @field_validator("provider_expires_at", "binding_expires_at", "frozen_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None) -> datetime | None:
        return _aware_utc(value, field_name="Tool binding timestamp")

    @model_validator(mode="after")
    def validate_snapshot(self) -> RunToolBindingSnapshot:
        if self.provider_expires_at is not None and self.provider_expires_at <= self.frozen_at:
            raise ValueError("cannot freeze an already expired Provider")
        if self.binding_expires_at is not None and self.binding_expires_at <= self.frozen_at:
            raise ValueError("cannot freeze an already expired binding")
        if (
            self.provider_expires_at is not None
            and self.binding_expires_at is not None
            and self.binding_expires_at > self.provider_expires_at
        ):
            raise ValueError("binding expiry cannot exceed Provider expiry")
        calculated = binding_digest(self)
        if self.binding_digest is None:
            object.__setattr__(self, "binding_digest", calculated)
        elif self.binding_digest != calculated:
            raise ValueError("binding_digest does not match the frozen binding payload")
        return self


_PROVIDER_TRANSITIONS: dict[ExternalToolProviderStatus, frozenset[ExternalToolProviderStatus]] = {
    ExternalToolProviderStatus.REGISTERED: frozenset(
        {
            ExternalToolProviderStatus.VERIFIED,
            ExternalToolProviderStatus.DISABLED,
            ExternalToolProviderStatus.EXPIRED,
            ExternalToolProviderStatus.REVOKED,
        }
    ),
    ExternalToolProviderStatus.VERIFIED: frozenset(
        {
            ExternalToolProviderStatus.ACTIVE,
            ExternalToolProviderStatus.DISABLED,
            ExternalToolProviderStatus.EXPIRED,
            ExternalToolProviderStatus.REVOKED,
        }
    ),
    ExternalToolProviderStatus.ACTIVE: frozenset(
        {
            ExternalToolProviderStatus.DISABLED,
            ExternalToolProviderStatus.EXPIRED,
            ExternalToolProviderStatus.REVOKED,
        }
    ),
    ExternalToolProviderStatus.DISABLED: frozenset(
        {
            ExternalToolProviderStatus.VERIFIED,
            ExternalToolProviderStatus.ACTIVE,
            ExternalToolProviderStatus.EXPIRED,
            ExternalToolProviderStatus.REVOKED,
        }
    ),
    ExternalToolProviderStatus.EXPIRED: frozenset(),
    ExternalToolProviderStatus.REVOKED: frozenset(),
}

_BINDING_TRANSITIONS: dict[RunToolBindingStatus, frozenset[RunToolBindingStatus]] = {
    RunToolBindingStatus.CREATED: frozenset(
        {RunToolBindingStatus.FROZEN, RunToolBindingStatus.REVOKED}
    ),
    RunToolBindingStatus.FROZEN: frozenset(
        {
            RunToolBindingStatus.ACTIVE,
            RunToolBindingStatus.EXPIRED,
            RunToolBindingStatus.REVOKED,
            RunToolBindingStatus.COMPLETED,
        }
    ),
    RunToolBindingStatus.ACTIVE: frozenset(
        {
            RunToolBindingStatus.EXPIRING,
            RunToolBindingStatus.EXPIRED,
            RunToolBindingStatus.REVOKED,
            RunToolBindingStatus.COMPLETED,
        }
    ),
    RunToolBindingStatus.EXPIRING: frozenset(
        {
            RunToolBindingStatus.EXPIRED,
            RunToolBindingStatus.REVOKED,
            RunToolBindingStatus.COMPLETED,
        }
    ),
    RunToolBindingStatus.EXPIRED: frozenset(),
    RunToolBindingStatus.REVOKED: frozenset(),
    RunToolBindingStatus.COMPLETED: frozenset(),
}


def provider_status_can_transition(
    current: ExternalToolProviderStatus | str,
    target: ExternalToolProviderStatus | str,
) -> bool:
    source = ExternalToolProviderStatus(current)
    return ExternalToolProviderStatus(target) in _PROVIDER_TRANSITIONS[source]


def binding_status_can_transition(
    current: RunToolBindingStatus | str,
    target: RunToolBindingStatus | str,
) -> bool:
    source = RunToolBindingStatus(current)
    return RunToolBindingStatus(target) in _BINDING_TRANSITIONS[source]


def canonical_json_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=False)
    return json.dumps(
        _json_ready(value),
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def canonical_digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"


def schema_digest(schema: dict[str, Any]) -> str:
    return canonical_digest(schema)


def binding_digest(value: RunToolBindingSnapshot | dict[str, Any]) -> str:
    if isinstance(value, RunToolBindingSnapshot):
        payload = value.model_dump(mode="json", exclude={"binding_digest"})
    else:
        payload = dict(value)
        payload.pop("binding_digest", None)
    return canonical_digest(payload)


def _aware_utc(value: datetime | None, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _validate_opaque_id(value: str, *, field_name: str) -> str:
    if not _OPAQUE_ID.fullmatch(value):
        raise ValueError(f"{field_name} must be an opaque safe string")
    return value


def _safe_text(value: str, *, maximum: int, field_name: str) -> str:
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} must contain 1-{maximum} characters")
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise ValueError(f"{field_name} contains control characters")
    return normalized


def _json_ready(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=False)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("canonical datetime values must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


# Concise protocol aliases retained for Provider implementers.
ToolProviderCapabilities = ProviderCapabilitiesResponse
ToolProviderHealth = ProviderHealthResponse
ToolProviderExecuteRequest = ProviderToolCallRequest
ToolProviderExecuteResponse = ProviderToolCallResponse
ToolProviderCancelRequest = ProviderCancelRequest
ToolProviderCancelResponse = ProviderCancelResponse
