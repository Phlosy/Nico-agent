from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from nico_agent.config import Settings
from nico_agent.web.cache import RedisWebFetchCache, RedisWebSearchCache
from nico_agent.web.contracts import SearchPage, SearchRequest, SearchResult
from nico_agent.web.rate_limit import RedisWebRateLimiter

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with Redis running",
)


@pytest.mark.asyncio
async def test_real_redis_cache_isolation_and_atomic_rate_limit() -> None:
    settings = Settings(environment="test", _env_file=None)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    prefix = f"nico:test:web:{uuid4()}"
    cache = RedisWebSearchCache(redis, prefix=f"{prefix}:cache")
    fetch_cache = RedisWebFetchCache(redis, prefix=f"{prefix}:fetch")
    limiter = RedisWebRateLimiter(redis, prefix=f"{prefix}:limit")
    tenant = uuid4()
    request = SearchRequest(query="nico")
    page = SearchPage(
        provider="brave",
        results=(
            SearchResult(
                title="Nico",
                url="https://docs.example/nico",
                snippet="Current docs",
            ),
        ),
    )
    try:
        await cache.set(tenant, "brave", "a" * 64, request, page, ttl_seconds=30)
        assert await cache.get(tenant, "brave", "a" * 64, request) == page
        assert await cache.get(uuid4(), "brave", "a" * 64, request) is None

        fetch_output = {
            "url": "https://docs.example/guide",
            "final_url": "https://docs.example/guide",
            "status": 200,
            "content_type": "text/plain",
            "title": None,
            "extractor": "plain",
            "content": "Current guide",
            "bytes": 13,
            "sha256": "c" * 64,
            "fetched_at": "2026-07-22T00:00:00+00:00",
            "redirects": 0,
            "truncated": False,
            "cached": False,
            "external_content": {
                "source": "web_fetch",
                "untrusted": True,
                "wrapped": True,
                "origin": "https://docs.example",
            },
        }
        await fetch_cache.set(
            tenant,
            "b" * 64,
            "https://docs.example/guide",
            "text",
            1000,
            fetch_output,
            ttl_seconds=30,
            source_key="search-call-1",
        )
        assert (
            await fetch_cache.get(
                tenant,
                "b" * 64,
                "https://docs.example/guide",
                "text",
                1000,
                "search-call-1",
            )
            == fetch_output
        )
        assert (
            await fetch_cache.get(
                uuid4(),
                "b" * 64,
                "https://docs.example/guide",
                "text",
                1000,
                "search-call-1",
            )
            is None
        )

        decisions = await asyncio.gather(
            *(
                limiter.acquire(
                    tenant_id=tenant,
                    tool="web.search@1.0.0",
                    provider="brave",
                    limit=5,
                )
                for _ in range(20)
            )
        )
        assert sum(decision.allowed for decision in decisions) == 5
    finally:
        keys = [key async for key in redis.scan_iter(match=f"{prefix}:*")]
        if keys:
            await redis.delete(*keys)
        await redis.aclose()
