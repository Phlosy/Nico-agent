from __future__ import annotations

from uuid import uuid4

import pytest

from nico_agent.web.cache import RedisWebSearchCache
from nico_agent.web.contracts import SearchPage, SearchRequest, SearchResult


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.fail = False

    async def get(self, key):
        if self.fail:
            raise RuntimeError("redis unavailable")
        return self.values.get(key)

    async def set(self, key, value, *, ex):
        if self.fail:
            raise RuntimeError("redis unavailable")
        self.values[key] = value
        self.ttls[key] = ex

    async def delete(self, key):
        self.values.pop(key, None)


def _page(provider: str = "brave") -> SearchPage:
    return SearchPage(
        provider=provider,
        results=(
            SearchResult(
                title="Nico",
                url="https://docs.example/nico",
                snippet="Current docs",
            ),
        ),
    )


@pytest.mark.asyncio
async def test_cache_key_is_tenant_provider_policy_and_request_isolated() -> None:
    redis = FakeRedis()
    cache = RedisWebSearchCache(redis)
    tenant = uuid4()
    request = SearchRequest(query="sensitive query")

    await cache.set(tenant, "brave", "a" * 64, request, _page(), ttl_seconds=60)

    assert await cache.get(tenant, "brave", "a" * 64, request) == _page()
    assert await cache.get(uuid4(), "brave", "a" * 64, request) is None
    assert await cache.get(tenant, "searxng", "a" * 64, request) is None
    assert await cache.get(tenant, "brave", "b" * 64, request) is None
    assert await cache.get(tenant, "brave", "a" * 64, SearchRequest(query="other")) is None
    assert all("sensitive query" not in key for key in redis.values)
    assert set(redis.ttls.values()) == {60}


@pytest.mark.asyncio
async def test_cache_corruption_and_redis_outage_degrade_to_a_miss() -> None:
    redis = FakeRedis()
    cache = RedisWebSearchCache(redis)
    tenant = uuid4()
    request = SearchRequest(query="nico")
    await cache.set(tenant, "brave", "a" * 64, request, _page(), ttl_seconds=60)
    key = next(iter(redis.values))
    redis.values[key] = "not-json"

    assert await cache.get(tenant, "brave", "a" * 64, request) is None

    redis.fail = True
    assert await cache.get(tenant, "brave", "a" * 64, request) is None
    await cache.set(tenant, "brave", "a" * 64, request, _page(), ttl_seconds=60)
