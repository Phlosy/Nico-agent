"""Runtime provider registration and explicit selection."""

from __future__ import annotations

from collections.abc import Iterable

from nico_agent.runtime.contracts import AgentRuntimeProvider
from nico_agent.runtime.errors import RuntimeProviderNotFound


class RuntimeProviderRegistry:
    def __init__(self, providers: Iterable[AgentRuntimeProvider] = ()) -> None:
        self._providers: dict[str, AgentRuntimeProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: AgentRuntimeProvider) -> None:
        name = provider.descriptor.name
        if name in self._providers:
            raise ValueError(f"runtime provider {name} is already registered")
        self._providers[name] = provider

    def get(self, name: str) -> AgentRuntimeProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise RuntimeProviderNotFound(name) from exc

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))
