"""Typed, non-sensitive errors for external Tool Provider operations."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nico_agent.domain.errors import DomainError

_CAUSE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")


class ToolProviderErrorCode(StrEnum):
    NOT_FOUND = "TOOL_PROVIDER_NOT_FOUND"
    DISABLED = "TOOL_PROVIDER_DISABLED"
    EXPIRED = "TOOL_PROVIDER_EXPIRED"
    SCOPE_MISMATCH = "TOOL_PROVIDER_SCOPE_MISMATCH"
    PROTOCOL_MISMATCH = "TOOL_PROVIDER_PROTOCOL_MISMATCH"
    CAPABILITY_MISMATCH = "TOOL_PROVIDER_CAPABILITY_MISMATCH"
    SCHEMA_MISMATCH = "TOOL_PROVIDER_SCHEMA_MISMATCH"
    AUTH_ERROR = "TOOL_PROVIDER_AUTH_ERROR"
    PERMISSION_DENIED = "TOOL_PROVIDER_PERMISSION_DENIED"
    CONNECTION_ERROR = "TOOL_PROVIDER_CONNECTION_ERROR"
    TIMEOUT = "TOOL_PROVIDER_TIMEOUT"
    RATE_LIMIT = "TOOL_PROVIDER_RATE_LIMIT"
    PROTOCOL_ERROR = "TOOL_PROVIDER_PROTOCOL_ERROR"
    INVALID_RESPONSE = "TOOL_PROVIDER_INVALID_RESPONSE"
    CANCEL_ERROR = "TOOL_PROVIDER_CANCEL_ERROR"
    BINDING_NOT_FOUND = "TOOL_BINDING_NOT_FOUND"
    BINDING_IMMUTABLE = "TOOL_BINDING_IMMUTABLE"
    BINDING_BUDGET_EXCEEDED = "TOOL_BINDING_BUDGET_EXCEEDED"
    BINDING_VERSION_MISMATCH = "TOOL_BINDING_VERSION_MISMATCH"


class ToolProviderErrorSource(StrEnum):
    REGISTRY = "registry"
    BINDING = "binding"
    WORKER = "worker"
    TRANSPORT = "transport"
    PROVIDER = "provider"
    PROTOCOL = "protocol"


class ToolProviderErrorCategory(StrEnum):
    NOT_FOUND = "not_found"
    LIFECYCLE = "lifecycle"
    SCOPE = "scope"
    CONTRACT = "contract"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    PROTOCOL = "protocol"
    RESPONSE = "response"
    CANCELLATION = "cancellation"
    IMMUTABILITY = "immutability"
    BUDGET = "budget"


class ToolProviderErrorContext(BaseModel):
    """Stable context suitable for API responses, Events, and Audit rows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: ToolProviderErrorSource
    category: ToolProviderErrorCategory
    code: ToolProviderErrorCode
    retryable: bool
    run_id: UUID | None = None
    tool_call_id: UUID | None = None
    provider_id: UUID | None = None
    attempt: int | None = Field(default=None, ge=1)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    cause: str | None = Field(default=None, max_length=100)

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("error timestamp must be timezone-aware")
        return value

    @field_validator("cause")
    @classmethod
    def validate_cause(cls, value: str | None) -> str | None:
        if value is not None and not _CAUSE_CODE.fullmatch(value):
            raise ValueError("cause must be a stable non-sensitive error code")
        return value


_ERROR_DEFAULTS: dict[
    ToolProviderErrorCode,
    tuple[ToolProviderErrorSource, ToolProviderErrorCategory, bool],
] = {
    ToolProviderErrorCode.NOT_FOUND: (
        ToolProviderErrorSource.REGISTRY,
        ToolProviderErrorCategory.NOT_FOUND,
        False,
    ),
    ToolProviderErrorCode.DISABLED: (
        ToolProviderErrorSource.REGISTRY,
        ToolProviderErrorCategory.LIFECYCLE,
        False,
    ),
    ToolProviderErrorCode.EXPIRED: (
        ToolProviderErrorSource.REGISTRY,
        ToolProviderErrorCategory.LIFECYCLE,
        False,
    ),
    ToolProviderErrorCode.SCOPE_MISMATCH: (
        ToolProviderErrorSource.BINDING,
        ToolProviderErrorCategory.SCOPE,
        False,
    ),
    ToolProviderErrorCode.PROTOCOL_MISMATCH: (
        ToolProviderErrorSource.PROTOCOL,
        ToolProviderErrorCategory.CONTRACT,
        False,
    ),
    ToolProviderErrorCode.CAPABILITY_MISMATCH: (
        ToolProviderErrorSource.PROTOCOL,
        ToolProviderErrorCategory.CONTRACT,
        False,
    ),
    ToolProviderErrorCode.SCHEMA_MISMATCH: (
        ToolProviderErrorSource.PROTOCOL,
        ToolProviderErrorCategory.CONTRACT,
        False,
    ),
    ToolProviderErrorCode.AUTH_ERROR: (
        ToolProviderErrorSource.PROVIDER,
        ToolProviderErrorCategory.AUTHENTICATION,
        False,
    ),
    ToolProviderErrorCode.PERMISSION_DENIED: (
        ToolProviderErrorSource.PROVIDER,
        ToolProviderErrorCategory.AUTHORIZATION,
        False,
    ),
    ToolProviderErrorCode.CONNECTION_ERROR: (
        ToolProviderErrorSource.TRANSPORT,
        ToolProviderErrorCategory.CONNECTION,
        True,
    ),
    ToolProviderErrorCode.TIMEOUT: (
        ToolProviderErrorSource.TRANSPORT,
        ToolProviderErrorCategory.TIMEOUT,
        True,
    ),
    ToolProviderErrorCode.RATE_LIMIT: (
        ToolProviderErrorSource.PROVIDER,
        ToolProviderErrorCategory.RATE_LIMIT,
        True,
    ),
    ToolProviderErrorCode.PROTOCOL_ERROR: (
        ToolProviderErrorSource.PROTOCOL,
        ToolProviderErrorCategory.PROTOCOL,
        False,
    ),
    ToolProviderErrorCode.INVALID_RESPONSE: (
        ToolProviderErrorSource.PROTOCOL,
        ToolProviderErrorCategory.RESPONSE,
        False,
    ),
    ToolProviderErrorCode.CANCEL_ERROR: (
        ToolProviderErrorSource.PROVIDER,
        ToolProviderErrorCategory.CANCELLATION,
        False,
    ),
    ToolProviderErrorCode.BINDING_NOT_FOUND: (
        ToolProviderErrorSource.BINDING,
        ToolProviderErrorCategory.NOT_FOUND,
        False,
    ),
    ToolProviderErrorCode.BINDING_IMMUTABLE: (
        ToolProviderErrorSource.BINDING,
        ToolProviderErrorCategory.IMMUTABILITY,
        False,
    ),
    ToolProviderErrorCode.BINDING_BUDGET_EXCEEDED: (
        ToolProviderErrorSource.BINDING,
        ToolProviderErrorCategory.BUDGET,
        False,
    ),
    ToolProviderErrorCode.BINDING_VERSION_MISMATCH: (
        ToolProviderErrorSource.BINDING,
        ToolProviderErrorCategory.CONTRACT,
        False,
    ),
}


class ToolProviderError(DomainError):
    """ToolError carrying the complete stable Provider failure context."""

    def __init__(self, message: str, *, context: ToolProviderErrorContext) -> None:
        safe_message = _safe_message(message)
        details: dict[str, Any] = context.model_dump(mode="json", exclude_none=False)
        super().__init__(context.code.value, safe_message, details=details)
        self.context = context
        self.source = context.source
        self.category = context.category
        self.retryable = context.retryable
        self.run_id = context.run_id
        self.tool_call_id = context.tool_call_id
        self.provider_id = context.provider_id
        self.attempt = context.attempt
        self.timestamp = context.timestamp
        self.cause = context.cause


def provider_error(
    code: ToolProviderErrorCode | str,
    message: str,
    *,
    run_id: UUID | None = None,
    tool_call_id: UUID | None = None,
    provider_id: UUID | None = None,
    attempt: int | None = None,
    timestamp: datetime | None = None,
    cause: str | None = None,
    source: ToolProviderErrorSource | None = None,
    category: ToolProviderErrorCategory | None = None,
    retryable: bool | None = None,
) -> ToolProviderError:
    """Construct one of the public error codes without leaking an exception message."""

    stable_code = ToolProviderErrorCode(code)
    default_source, default_category, default_retryable = _ERROR_DEFAULTS[stable_code]
    context = ToolProviderErrorContext(
        source=source or default_source,
        category=category or default_category,
        code=stable_code,
        retryable=default_retryable if retryable is None else retryable,
        run_id=run_id,
        tool_call_id=tool_call_id,
        provider_id=provider_id,
        attempt=attempt,
        timestamp=timestamp or datetime.now(UTC),
        cause=cause,
    )
    return ToolProviderError(message, context=context)


def _safe_message(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized:
        return "external Tool Provider operation failed"
    return "".join(
        character
        for character in normalized[:1000]
        if not unicodedata.category(character).startswith("C")
    )
