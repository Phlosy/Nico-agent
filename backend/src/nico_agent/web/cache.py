"""Tenant-isolated semantic cache for normalized Web search pages."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import ValidationError

from nico_agent.tools.contracts import canonical_hash
from nico_agent.web.contracts import SearchPage, SearchRequest


class RedisWebSearchCache:
    def __init__(self, redis_client: Any, *, prefix: str = "nico:web-search-cache:v1") -> None:
        self.redis = redis_client
        self.prefix = prefix

    async def get(
        self,
        tenant_id: UUID,
        provider: str,
        policy_hash: str,
        request: SearchRequest,
    ) -> SearchPage | None:
        key = self._key(tenant_id, provider, policy_hash, request)
        try:
            value = await self.redis.get(key)
        except Exception:
            return None
        if value is None:
            return None
        try:
            return SearchPage.model_validate_json(value)
        except (ValidationError, ValueError, TypeError):
            try:
                await self.redis.delete(key)
            except Exception:
                pass
            return None

    async def set(
        self,
        tenant_id: UUID,
        provider: str,
        policy_hash: str,
        request: SearchRequest,
        page: SearchPage,
        *,
        ttl_seconds: int,
    ) -> None:
        if not 1 <= ttl_seconds <= 86_400:
            raise ValueError("Web cache TTL must be between 1 and 86400 seconds")
        key = self._key(tenant_id, provider, policy_hash, request)
        try:
            await self.redis.set(key, page.model_dump_json(), ex=ttl_seconds)
        except Exception:
            return

    def _key(
        self,
        tenant_id: UUID,
        provider: str,
        policy_hash: str,
        request: SearchRequest,
    ) -> str:
        request_hash = canonical_hash(request.normalized_payload())
        return f"{self.prefix}:{tenant_id}:{provider}:{policy_hash}:{request_hash}"
