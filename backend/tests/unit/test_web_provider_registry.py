from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from nico_agent.web.contracts import SearchPage, SearchProviderError, SearchRequest
from nico_agent.web.registry import WebProviderRegistry


@dataclass
class FakeProvider:
    key: str

    async def search(self, request, *, secret=None):
        return SearchPage(provider=self.key, results=())


def test_search_request_normalizes_stable_cross_provider_fields() -> None:
    request = SearchRequest(
        query="  current Nico release  ",
        count=10,
        language="zh-CN",
        country="cn",
        freshness="week",
        domains=("BÜCHER.example.", "docs.example"),
    )

    assert request.query == "current Nico release"
    assert request.country == "CN"
    assert request.domains == ("xn--bcher-kva.example", "docs.example")


@pytest.mark.parametrize(
    "overrides",
    [
        {"query": ""},
        {"query": "x" * 2001},
        {"count": 11},
        {"language": "not a language"},
        {"country": "CHN"},
        {"freshness": "hour"},
        {"domains": tuple(f"{item}.example" for item in range(11))},
        {"domains": ("*.example.com",)},
    ],
)
def test_search_request_rejects_fields_outside_the_shared_contract(overrides) -> None:
    values = {"query": "nico", **overrides}
    with pytest.raises(ValidationError):
        SearchRequest(**values)


def test_registry_uses_explicit_provider_keys_without_fallback() -> None:
    brave = FakeProvider("brave")
    searxng = FakeProvider("searxng")
    registry = WebProviderRegistry([brave, searxng])

    assert registry.get("brave") is brave
    assert registry.get("searxng") is searxng
    with pytest.raises(SearchProviderError) as captured:
        registry.get("automatic")
    assert captured.value.code == "WEB_SEARCH_NOT_CONFIGURED"


def test_registry_rejects_duplicate_or_untrusted_provider_keys() -> None:
    with pytest.raises(ValueError):
        WebProviderRegistry([FakeProvider("brave"), FakeProvider("brave")])
    with pytest.raises(ValueError):
        WebProviderRegistry([FakeProvider("custom-import-path")])
