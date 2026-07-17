"""Stable domain errors shared by application and API layers."""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class InvalidStateTransition(DomainError):
    def __init__(self, entity: str, current: str, target: str) -> None:
        super().__init__(
            "INVALID_STATE_TRANSITION",
            f"{entity} cannot transition from {current} to {target}",
            details={"entity": entity, "current": current, "target": target},
        )


class RevisionConflict(DomainError):
    def __init__(self, entity: str, expected: int, actual: int) -> None:
        super().__init__(
            "REVISION_CONFLICT",
            f"{entity} revision is {actual}, expected {expected}",
            details={"entity": entity, "expected_revision": expected, "actual_revision": actual},
        )


class ResourceNotFound(DomainError):
    def __init__(self, entity: str, resource_id: str) -> None:
        super().__init__(
            "RESOURCE_NOT_FOUND",
            f"{entity} {resource_id} was not found",
            details={"entity": entity, "resource_id": resource_id},
        )


class DomainConflict(DomainError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code, message, details=details)
