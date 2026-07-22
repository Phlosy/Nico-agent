from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from nico_agent.web.rate_limit import RedisWebRateLimiter, WebRateLimitUnavailable


class AtomicFakeRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.lock = asyncio.Lock()
        self.fail = False

    async def eval(self, script, key_count, key, limit, period_seconds):
        if self.fail:
            raise RuntimeError("redis unavailable")
        async with self.lock:
            self.counts[key] = self.counts.get(key, 0) + 1
            allowed = int(self.counts[key] <= int(limit))
            return [allowed, int(period_seconds)]


@pytest.mark.asyncio
async def test_rate_limit_is_atomic_and_tenant_provider_isolated() -> None:
    redis = AtomicFakeRedis()
    limiter = RedisWebRateLimiter(redis)
    tenant = uuid4()

    decisions = await asyncio.gather(
        *(
            limiter.acquire(
                tenant_id=tenant,
                tool="web.search@1.0.0",
                provider="brave",
                limit=4,
            )
            for _ in range(8)
        )
    )

    assert sum(decision.allowed for decision in decisions) == 4
    assert {decision.retry_after_seconds for decision in decisions[4:]} == {60}
    other = await limiter.acquire(
        tenant_id=uuid4(),
        tool="web.search@1.0.0",
        provider="brave",
        limit=4,
    )
    assert other.allowed is True


@pytest.mark.asyncio
async def test_rate_limit_outage_is_hard_in_production_and_soft_when_explicit() -> None:
    redis = AtomicFakeRedis()
    redis.fail = True
    limiter = RedisWebRateLimiter(redis)
    arguments = {
        "tenant_id": uuid4(),
        "tool": "web.search@1.0.0",
        "provider": "brave",
        "limit": 10,
    }

    with pytest.raises(WebRateLimitUnavailable):
        await limiter.acquire(**arguments, hard=True)

    decision = await limiter.acquire(**arguments, hard=False)
    assert decision.allowed is True
    assert decision.available is False
