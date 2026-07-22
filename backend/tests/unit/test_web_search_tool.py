from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

import pytest

from nico_agent.tools import ToolExecutionContext
from nico_agent.tools.builtin.web_search import WebSearchExecutor
from nico_agent.tools.errors import ToolExecutorFailure, ToolSchemaViolation
from nico_agent.web.contracts import (
    SearchPage,
    SearchProviderError,
    SearchResult,
)
from nico_agent.web.rate_limit import WebRateLimitDecision
from nico_agent.web.registry import WebProviderRegistry


@dataclass
class FakeProvider:
    key: str
    calls: int = 0
    seen_config: list[dict] = field(default_factory=list)
    error: SearchProviderError | None = None

    async def search(self, request, *, secret=None, config=None):
        self.calls += 1
        self.seen_config.append(config or {})
        if self.error is not None:
            raise self.error
        return SearchPage(
            provider=self.key,
            results=(
                SearchResult(
                    title="Nico docs",
                    url="https://docs.example/nico",
                    snippet="Current documentation",
                ),
            ),
        )


class FakeCache:
    def __init__(self, hit: SearchPage | None = None) -> None:
        self.hit = hit
        self.sets = []

    async def get(self, *args):
        return self.hit

    async def set(self, *args, **kwargs):
        self.sets.append((args, kwargs))


class FakeLimiter:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls = 0

    async def acquire(self, **kwargs):
        self.calls += 1
        return WebRateLimitDecision(
            allowed=self.allowed,
            retry_after_seconds=30,
        )


def _context(config: dict) -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id=uuid4(),
        run_id=uuid4(),
        run_step_id=uuid4(),
        actor_id="runtime:test",
        correlation_id=uuid4(),
        tool_config=config,
    )


def _executor(*, cache=None, limiter=None, provider=None, environment="test"):
    selected = provider or FakeProvider("brave")
    other = FakeProvider("searxng")
    providers = [selected, other] if selected.key == "brave" else [FakeProvider("brave"), selected]
    return WebSearchExecutor(
        WebProviderRegistry(providers),
        cache=cache,
        rate_limiter=limiter,
        environment=environment,
    ), selected


def test_web_search_contract_is_exact_bounded_and_medium_risk() -> None:
    spec = WebSearchExecutor.spec

    assert spec.reference == "web.search@1.0.0"
    assert spec.permission == "network.web.search"
    assert spec.risk.value == "medium"
    assert spec.max_output_bytes == 131_072
    assert spec.secret_names == frozenset({"web_search_brave_api_key"})
    spec.validate_input({"query": "nico", "count": 10, "freshness": "week"})
    with pytest.raises(ToolSchemaViolation):
        spec.validate_input({"query": "nico", "count": 11})
    with pytest.raises(ToolSchemaViolation):
        spec.validate_input({"query": "nico", "provider": "searxng"})


def test_required_secret_names_follow_validated_frozen_provider_config() -> None:
    executor, _ = _executor()

    assert executor.required_secret_names({"provider": "brave"}) == frozenset(
        {"web_search_brave_api_key"}
    )
    assert executor.required_secret_names({"provider": "searxng"}) == frozenset()
    with pytest.raises(ToolExecutorFailure) as captured:
        executor.required_secret_names({"provider": "automatic"})
    assert captured.value.code == "WEB_SEARCH_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_execute_returns_authoritative_query_normalized_results_and_safe_usage() -> None:
    cache = FakeCache()
    limiter = FakeLimiter()
    executor, provider = _executor(cache=cache, limiter=limiter)

    result = await executor.execute(
        _context(
            {
                "provider": "brave",
                "count_limit": 3,
                "safe_search": "strict",
                "cache_ttl_seconds": 60,
                "rate_limit_per_minute": 10,
            }
        ),
        {"query": "current nico", "count": 5, "domains": ["docs.example"]},
        {"web_search_brave_api_key": "secret"},
    )

    assert result.output == {
        "provider": "brave",
        "query": "current nico",
        "results": [
            {
                "title": "Nico docs",
                "url": "https://docs.example/nico",
                "snippet": "Current documentation",
                "published_at": None,
                "site_name": None,
            }
        ],
        "result_count": 1,
        "cached": False,
        "took_ms": result.output["took_ms"],
        "external_content": {
            "untrusted": True,
            "source": "web_search",
            "wrapped": True,
            "provider": "brave",
        },
    }
    assert result.output["took_ms"] >= 0
    assert result.usage == {
        "provider": "brave",
        "result_count": 1,
        "cached": False,
        "took_ms": result.output["took_ms"],
    }
    assert provider.seen_config == [{"safe_search": "strict"}]
    assert limiter.calls == 1
    assert len(cache.sets) == 1


@pytest.mark.asyncio
async def test_cache_hit_avoids_limiter_and_provider_but_returns_fresh_tool_output() -> None:
    page = SearchPage(provider="brave", results=())
    cache = FakeCache(hit=page)
    limiter = FakeLimiter()
    executor, provider = _executor(cache=cache, limiter=limiter)

    result = await executor.execute(
        _context({"provider": "brave", "cache_ttl_seconds": 60}),
        {"query": "nico"},
        {"web_search_brave_api_key": "secret"},
    )

    assert result.output["cached"] is True
    assert provider.calls == 0
    assert limiter.calls == 0


@pytest.mark.asyncio
async def test_rate_and_provider_failures_map_to_stable_tool_errors() -> None:
    limited, _ = _executor(cache=FakeCache(), limiter=FakeLimiter(allowed=False))
    with pytest.raises(ToolExecutorFailure) as rate_error:
        await limited.execute(
            _context({"provider": "brave", "rate_limit_per_minute": 1}),
            {"query": "nico"},
            {"web_search_brave_api_key": "secret"},
        )
    assert rate_error.value.code == "WEB_PROVIDER_RATE_LIMITED"

    provider = FakeProvider(
        "brave",
        error=SearchProviderError(
            "WEB_PROVIDER_UNAVAILABLE",
            "Provider unavailable",
            retryable=True,
        ),
    )
    failed, _ = _executor(cache=FakeCache(), limiter=FakeLimiter(), provider=provider)
    with pytest.raises(ToolExecutorFailure) as provider_error:
        await failed.execute(
            _context({"provider": "brave"}),
            {"query": "nico"},
            {"web_search_brave_api_key": "secret"},
        )
    assert provider_error.value.code == "WEB_PROVIDER_UNAVAILABLE"
