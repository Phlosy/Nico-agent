"""Model provider registration by protocol name."""

from collections.abc import Iterable

from nico_agent.models.contracts import ModelProvider
from nico_agent.models.errors import ModelProviderNotFound


class ModelProviderRegistry:
    def __init__(self, providers: Iterable[ModelProvider] = ()) -> None:
        self._providers: dict[str, ModelProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: ModelProvider) -> None:
        if provider.name in self._providers:
            raise ValueError(f"model provider {provider.name} is already registered")
        self._providers[provider.name] = provider

    def get(self, name: str) -> ModelProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise ModelProviderNotFound(name) from exc

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))
