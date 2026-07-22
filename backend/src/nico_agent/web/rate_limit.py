"""Atomic Redis rate limiting for external Web Providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID


class WebRateLimitUnavailable(Exception):
    pass


@dataclass(frozen=True, slots=True)
class WebRateLimitDecision:
    allowed: bool
    retry_after_seconds: int
    available: bool = True


class RedisWebRateLimiter:
    _SCRIPT = """
    local current = redis.call('INCR', KEYS[1])
    if current == 1 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
    local ttl = redis.call('TTL', KEYS[1])
    if ttl < 0 then
      redis.call('EXPIRE', KEYS[1], ARGV[2])
      ttl = tonumber(ARGV[2])
    end
    if current > tonumber(ARGV[1]) then return {0, ttl} end
    return {1, ttl}
    """

    def __init__(self, redis_client: Any, *, prefix: str = "nico:web-limit:v1") -> None:
        self.redis = redis_client
        self.prefix = prefix

    async def acquire(
        self,
        *,
        tenant_id: UUID,
        tool: str,
        provider: str,
        limit: int,
        period_seconds: int = 60,
        hard: bool = True,
    ) -> WebRateLimitDecision:
        if not 1 <= limit <= 100_000:
            raise ValueError("Web rate limit must be between 1 and 100000")
        if not 1 <= period_seconds <= 3600:
            raise ValueError("Web rate period must be between 1 and 3600 seconds")
        key = f"{self.prefix}:{tenant_id}:{tool}:{provider}"
        try:
            result = await self.redis.eval(
                self._SCRIPT,
                1,
                key,
                limit,
                period_seconds,
            )
            allowed, retry_after = int(result[0]), max(1, int(result[1]))
        except Exception as exc:
            if hard:
                raise WebRateLimitUnavailable("Web rate limit state is unavailable") from exc
            return WebRateLimitDecision(allowed=True, retry_after_seconds=0, available=False)
        return WebRateLimitDecision(
            allowed=bool(allowed),
            retry_after_seconds=retry_after,
        )
