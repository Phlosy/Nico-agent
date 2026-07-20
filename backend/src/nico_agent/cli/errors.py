"""Stable, user-safe CLI failures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class CliError(Exception):
    code: str
    message: str
    exit_code: int = 1
    request_id: str | None = None
    status_code: int | None = None
    details: Any | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.status_code is not None:
            error["status_code"] = self.status_code
        if self.request_id is not None:
            error["request_id"] = self.request_id
        if self.details is not None:
            error["details"] = self.details
        return {"error": error}
