"""Stable, non-sensitive Tool Gateway errors."""

from __future__ import annotations

from typing import Any

from nico_agent.domain.errors import DomainError


class ToolError(DomainError):
    pass


class ToolDefinitionInvalid(ToolError):
    def __init__(self, message: str) -> None:
        super().__init__("TOOL_DEFINITION_INVALID", message)


class ToolNotFound(ToolError):
    def __init__(self, name: str, version: str) -> None:
        super().__init__(
            "TOOL_NOT_FOUND",
            f"tool {name}@{version} is not registered",
            details={"name": name, "version": version},
        )


class ToolRegistryConflict(ToolError):
    def __init__(self, name: str, version: str) -> None:
        super().__init__(
            "TOOL_REGISTRY_CONFLICT",
            f"tool {name}@{version} is already registered",
            details={"name": name, "version": version},
        )


class ToolSchemaViolation(ToolError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        path: list[str | int],
        validator: str | None,
    ) -> None:
        details: dict[str, Any] = {"path": path}
        if validator is not None:
            details["validator"] = validator
        super().__init__(code, message, details=details)


class ToolImplementationMismatch(ToolError):
    def __init__(self, name: str, version: str) -> None:
        super().__init__(
            "TOOL_IMPLEMENTATION_MISMATCH",
            f"tool {name}@{version} does not match its persisted definition",
            details={"name": name, "version": version},
        )


class ToolAccessDenied(ToolError):
    def __init__(self, name: str, version: str, reason: str = "policy denied the tool") -> None:
        super().__init__(
            "TOOL_ACCESS_DENIED",
            f"tool {name}@{version} is not authorized for this Run",
            details={"name": name, "version": version, "reason": reason},
        )


class ToolDisabled(ToolError):
    def __init__(self, name: str, version: str) -> None:
        super().__init__(
            "TOOL_DISABLED",
            f"tool {name}@{version} is disabled",
            details={"name": name, "version": version},
        )


class ToolIdempotencyConflict(ToolError):
    def __init__(self, idempotency_key: str) -> None:
        super().__init__(
            "TOOL_IDEMPOTENCY_CONFLICT",
            "the idempotency key was already used with different arguments",
            details={"idempotency_key": idempotency_key},
        )


class ToolCallInProgress(ToolError):
    def __init__(self, tool_call_id: str) -> None:
        super().__init__(
            "TOOL_CALL_IN_PROGRESS",
            "the idempotent tool call is already executing",
            details={"tool_call_id": tool_call_id},
        )


class ToolSecretUnavailable(ToolError):
    def __init__(self, name: str) -> None:
        super().__init__(
            "TOOL_SECRET_UNAVAILABLE",
            f"required tool secret {name} is unavailable",
            details={"name": name},
        )


class ToolLeaseLost(ToolError):
    def __init__(self, run_id: str) -> None:
        super().__init__(
            "TOOL_LEASE_LOST",
            "the Run lease no longer authorizes this tool call",
            details={"run_id": run_id},
        )


class ToolExecutorFailure(ToolError):
    def __init__(self, code: str, message: str) -> None:
        if not code or len(code) > 100:
            raise ValueError("tool executor error code must contain 1 to 100 characters")
        super().__init__(code, message[:1000])
