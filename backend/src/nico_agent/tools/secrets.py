"""Tool-only Secret resolution and recursive persistence redaction."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any, Protocol

from nico_agent.tools.errors import ToolSecretUnavailable

_ENV_REFERENCE = re.compile(r"^env:(NICO_TOOL_SECRET_[A-Z0-9_]{1,100})$")
_SENSITIVE_KEY = re.compile(r"(?i)(?:api[_-]?key|authorization|password|secret|token)")
_SECRET_PATTERN = re.compile(
    r"(?i)(?:(?:api[_-]?key|authorization|password|secret|token)\s*[=:]\s*)[^\s,;]+"
)


class SecretResolver(Protocol):
    def resolve(self, name: str, reference: str) -> str: ...


class EnvironmentSecretResolver:
    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = environment if environment is not None else os.environ

    def resolve(self, name: str, reference: str) -> str:
        match = _ENV_REFERENCE.fullmatch(reference)
        if match is None:
            raise ToolSecretUnavailable(name)
        value = self._environment.get(match.group(1))
        if not value:
            raise ToolSecretUnavailable(name)
        return value


def resolve_secrets(resolver: SecretResolver, references: dict[str, str]) -> dict[str, str]:
    return {name: resolver.resolve(name, reference) for name, reference in references.items()}


def redact_value(value: Any, secrets: Mapping[str, str] | None = None) -> Any:
    secrets = secrets or {}
    secret_values = tuple(item for item in secrets.values() if item)
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else redact_value(item, secrets)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item, secrets) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret in secret_values:
            redacted = redacted.replace(secret, "[REDACTED]")
        return _SECRET_PATTERN.sub("[REDACTED]", redacted)
    return value
