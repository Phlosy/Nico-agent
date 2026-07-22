"""Static trusted Web search Provider registry."""

from __future__ import annotations

from collections.abc import Iterable

from nico_agent.web.contracts import SearchProvider, SearchProviderError

_TRUSTED_PROVIDER_KEYS = frozenset({"brave", "searxng"})


class WebProviderRegistry:
    def __init__(self, providers: Iterable[SearchProvider]) -> None:
        self._providers: dict[str, SearchProvider] = {}
        for provider in providers:
            if provider.key not in _TRUSTED_PROVIDER_KEYS:
                raise ValueError("Web Provider key is not part of the trusted registry")
            if provider.key in self._providers:
                raise ValueError(f"duplicate Web Provider key: {provider.key}")
            self._providers[provider.key] = provider

    def get(self, key: str) -> SearchProvider:
        try:
            return self._providers[key]
        except KeyError as exc:
            raise SearchProviderError(
                "WEB_SEARCH_NOT_CONFIGURED",
                "Web search Provider is not configured",
            ) from exc

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))
