from __future__ import annotations

import pytest

from nico_agent.provider_onboarding.worker import ProviderProbeWorker
from nico_agent.web.contracts import SearchPage, SearchResult
from nico_agent.web.registry import WebProviderRegistry


class FakeWebProvider:
    def __init__(self, key: str) -> None:
        self.key = key
        self.calls = []

    async def search(self, request, *, secret=None, config=None):
        self.calls.append((request, secret, config))
        return SearchPage(
            provider=self.key,
            results=(
                SearchResult(
                    title="Nico",
                    url="https://docs.example/nico",
                    snippet="Documentation",
                ),
            ),
        )


class FakeSecretResolver:
    def __init__(self) -> None:
        self.calls = []

    def resolve(self, name, reference):
        self.calls.append((name, reference))
        return "brave-secret"


@pytest.mark.asyncio
async def test_web_probe_uses_registry_and_resolves_only_brave_secret() -> None:
    brave = FakeWebProvider("brave")
    searxng = FakeWebProvider("searxng")
    resolver = FakeSecretResolver()
    worker = ProviderProbeWorker(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        worker_id="web-probe-test",
        web_providers=WebProviderRegistry([brave, searxng]),
        tool_secret_resolver=resolver,
    )
    base = {
        "kind": "verify_web",
        "model": None,
        "endpoint": {
            "credential_ref": "env:NICO_TOOL_SECRET_WEB_SEARCH_BRAVE_TEST",
            "provider_options": {"policy": {"safe_search": "moderate"}},
        },
    }

    brave_result, brave_verified = await worker._execute(
        {**base, "provider_key": "brave"}
    )
    searxng_result, searxng_verified = await worker._execute(
        {
            **base,
            "provider_key": "searxng",
            "endpoint": {
                "credential_ref": "none",
                "provider_options": {"policy": {"safe_search": "moderate"}},
            },
        }
    )

    assert brave_verified is True and brave_result["provider"] == "brave"
    assert searxng_verified is True and searxng_result["provider"] == "searxng"
    assert resolver.calls == [
        (
            "web_search_brave_api_key",
            "env:NICO_TOOL_SECRET_WEB_SEARCH_BRAVE_TEST",
        )
    ]
    assert brave.calls[0][1] == "brave-secret"
    assert searxng.calls[0][1] is None
