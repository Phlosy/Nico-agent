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
