"""Tenant-isolated semantic cache for normalized Web search pages."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from nico_agent.net.safe_http import SafeHttpError, canonicalize_http_url
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


class RedisWebFetchCache:
    def __init__(self, redis_client: Any, *, prefix: str = "nico:web-fetch-cache:v1") -> None:
        self.redis = redis_client
        self.prefix = prefix

    async def get(
        self,
        tenant_id: UUID,
        policy_hash: str,
        url: str,
        extract_mode: str,
        max_chars: int,
        source_key: str,
    ) -> dict[str, Any] | None:
        key = self._key(tenant_id, policy_hash, url, extract_mode, max_chars, source_key)
        try:
            value = await self.redis.get(key)
        except Exception:
            return None
        if value is None:
            return None
        try:
            payload = json.loads(value)
        except (TypeError, ValueError):
            await self._delete(key)
            return None
        try:
            entry = _FetchCacheEntry.model_validate(payload)
        except ValidationError:
            await self._delete(key)
            return None
        return entry.model_dump(mode="json")

    async def set(
        self,
        tenant_id: UUID,
        policy_hash: str,
        url: str,
        extract_mode: str,
        max_chars: int,
        output: dict[str, Any],
        *,
        ttl_seconds: int,
        source_key: str,
    ) -> None:
        if not 1 <= ttl_seconds <= 86_400:
            raise ValueError("Web cache TTL must be between 1 and 86400 seconds")
        key = self._key(tenant_id, policy_hash, url, extract_mode, max_chars, source_key)
        try:
            await self.redis.set(
                key,
                json.dumps(output, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
                ex=ttl_seconds,
            )
        except Exception:
            return

    async def _delete(self, key: str) -> None:
        try:
            await self.redis.delete(key)
        except Exception:
            return

    def _key(
        self,
        tenant_id: UUID,
        policy_hash: str,
        url: str,
        extract_mode: str,
        max_chars: int,
        source_key: str,
    ) -> str:
        request_hash = canonical_hash(
            {
                "url": url,
                "extract_mode": extract_mode,
                "max_chars": max_chars,
                "source_key": source_key,
            }
        )
        return f"{self.prefix}:{tenant_id}:{policy_hash}:{request_hash}"


class _FetchExternalContent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    untrusted: Literal[True]
    source: Literal["web_fetch"]
    wrapped: Literal[True]
    origin: str = Field(min_length=1, max_length=4096)

    @field_validator("origin")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        try:
            parsed = canonicalize_http_url(f"{value}/")
        except SafeHttpError as exc:
            raise ValueError("cached Web origin is invalid") from exc
        if parsed.origin != value:
            raise ValueError("cached Web origin is not canonical")
        return value


class _FetchCacheEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str = Field(min_length=1, max_length=4096)
    final_url: str = Field(min_length=1, max_length=4096)
    status: int = Field(ge=200, le=299)
    content_type: str = Field(min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=500)
    extractor: Literal["trafilatura", "html_fallback", "plain", "markdown", "json"]
    content: str = Field(min_length=1, max_length=20_000)
    bytes: int = Field(ge=0, le=768_000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    fetched_at: str = Field(min_length=1, max_length=100)
    redirects: int = Field(ge=0, le=3)
    truncated: bool
    cached: bool
    external_content: _FetchExternalContent

    @field_validator("url", "final_url")
    @classmethod
    def canonicalize_url(cls, value: str) -> str:
        try:
            return canonicalize_http_url(value).url
        except SafeHttpError as exc:
            raise ValueError("cached Web URL is invalid") from exc

    @field_validator("fetched_at")
    @classmethod
    def validate_fetched_at(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("cached Web timestamp is invalid") from exc
        if parsed.tzinfo is None:
            raise ValueError("cached Web timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_final_origin(self):
        if canonicalize_http_url(self.final_url).origin != self.external_content.origin:
            raise ValueError("cached Web final URL origin does not match its metadata")
        return self
