"""Authenticated sandbox runner request and response contracts."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nico_agent.tools.contracts import canonical_json

_SENSITIVE_KEY = re.compile(r"(?i)(?:api[_-]?key|authorization|password|secret|token)")


class SandboxExecutionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1, max_length=50_000)
    input: Any = None
    wall_time_seconds: int = Field(default=10, ge=1, le=30)
    memory_bytes: int = Field(default=134_217_728, ge=33_554_432, le=536_870_912)
    nano_cpus: int = Field(default=500_000_000, ge=100_000_000, le=2_000_000_000)
    pids_limit: int = Field(default=32, ge=1, le=128)
    output_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)

    @field_validator("input")
    @classmethod
    def validate_non_sensitive_json(cls, value: Any) -> Any:
        if _contains_sensitive_key(value):
            raise ValueError("sandbox input cannot contain sensitive keys")
        try:
            encoded = canonical_json(value).encode()
        except (TypeError, ValueError) as exc:
            raise ValueError("sandbox input must be finite JSON data") from exc
        if len(encoded) > 100_000:
            raise ValueError("sandbox input exceeds its byte limit")
        return value


class SandboxExecutionResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["succeeded", "failed", "timed_out", "oom_killed"]
    result: Any = None
    stdout: str = Field(default="", max_length=1_048_576)
    stderr: str = Field(default="", max_length=1_048_576)
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    error: dict[str, str] | None = None
    exit_code: int | None = None
    duration_ms: int = Field(ge=0)


def _contains_sensitive_key(value: Any, *, depth: int = 0) -> bool:
    if depth > 20:
        return True
    if isinstance(value, dict):
        return any(
            _SENSITIVE_KEY.search(str(key)) or _contains_sensitive_key(item, depth=depth + 1)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_sensitive_key(item, depth=depth + 1) for item in value)
    return False
