"""Runtime provider registration and explicit selection."""

from __future__ import annotations

from collections.abc import Iterable

from nico_agent.runtime.contracts import (
    AgentRuntimeProvider,
    AgentRuntimeProviderV2,
    RuntimeCapability,
    RuntimeProviderDescriptor,
)
from nico_agent.runtime.errors import RuntimeProviderNotFound


class RuntimeProviderRegistry:
    def __init__(
        self, providers: Iterable[AgentRuntimeProvider | AgentRuntimeProviderV2] = ()
    ) -> None:
        self._providers: dict[str, AgentRuntimeProvider | AgentRuntimeProviderV2] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: AgentRuntimeProvider | AgentRuntimeProviderV2) -> None:
        name = provider.descriptor.name
        if name in self._providers:
            raise ValueError(f"runtime provider {name} is already registered")
        self._providers[name] = provider

    def get(self, name: str) -> AgentRuntimeProvider | AgentRuntimeProviderV2:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise RuntimeProviderNotFound(name) from exc

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    @property
    def descriptors(self) -> tuple[RuntimeProviderDescriptor, ...]:
        return tuple(self._providers[name].descriptor for name in sorted(self._providers))

    def capability_matrix(self) -> tuple[dict[str, object], ...]:
        """Return a stable, machine-readable parity view of registered providers."""

        return tuple(
            {
                "name": descriptor.name,
                "version": descriptor.version,
                "protocol_version": descriptor.protocol_version,
                "implementation": descriptor.implementation,
                "capabilities": {
                    capability.value: capability in descriptor.capabilities
                    for capability in RuntimeCapability
                },
                "compatibility": descriptor.compatibility,
            }
            for descriptor in self.descriptors
        )
