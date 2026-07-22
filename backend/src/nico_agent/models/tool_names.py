"""Stable mapping between Nico Tool names and provider-safe function names."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_PROVIDER_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_UNSAFE_CHARACTER = re.compile(r"[^A-Za-z0-9_-]")


def provider_safe_tool_name(name: str) -> str:
    """Return a deterministic provider-safe spelling for one internal Tool name."""

    if _PROVIDER_TOOL_NAME.fullmatch(name):
        return name
    stem = _UNSAFE_CHARACTER.sub("_", name).strip("_") or "tool"
    digest = hashlib.sha256(name.encode()).hexdigest()[:10]
    return f"{stem[: 64 - len(digest) - 1]}_{digest}"


@dataclass(frozen=True, slots=True)
class ProviderToolNames:
    """Encode outgoing names and restore provider-returned calls to Nico names."""

    internal_to_wire: dict[str, str]
    wire_to_internal: dict[str, str]

    @classmethod
    def from_definitions(cls, definitions: Iterable[Any]) -> ProviderToolNames:
        names = tuple(dict.fromkeys(str(item.name) for item in definitions))
        reserved = {name for name in names if _PROVIDER_TOOL_NAME.fullmatch(name)}
        internal_to_wire: dict[str, str] = {}
        wire_to_internal: dict[str, str] = {}
        for name in names:
            candidate = provider_safe_tool_name(name)
            if candidate in reserved and candidate != name:
                candidate = _collision_safe_name(name, reserved | set(wire_to_internal))
            elif candidate in wire_to_internal and wire_to_internal[candidate] != name:
                candidate = _collision_safe_name(name, reserved | set(wire_to_internal))
            internal_to_wire[name] = candidate
            wire_to_internal[candidate] = name
        return cls(internal_to_wire, wire_to_internal)

    def encode(self, name: str) -> str:
        return self.internal_to_wire.get(name, provider_safe_tool_name(name))

    def decode(self, name: str) -> str:
        return self.wire_to_internal.get(name, name)


def _collision_safe_name(name: str, used: set[str]) -> str:
    for attempt in range(100):
        digest = hashlib.sha256(f"{attempt}:{name}".encode()).hexdigest()
        candidate = f"nico_{digest[:59]}"
        if candidate not in used:
            return candidate
    raise ValueError("could not allocate a unique provider Tool name")
