"""Tool Gateway executor for normalized, policy-bound Web search."""

from __future__ import annotations

import time
from typing import Any

from pydantic import ValidationError

from nico_agent.tools.contracts import (
    ToolDefinitionSpec,
    ToolExecutionResult,
    ToolIsolation,
    ToolRetryPolicy,
    ToolRisk,
    canonical_hash,
)
from nico_agent.tools.errors import ToolExecutorFailure
from nico_agent.web.cache import RedisWebSearchCache
from nico_agent.web.contracts import SearchPage, SearchProviderError, SearchRequest
from nico_agent.web.rate_limit import RedisWebRateLimiter, WebRateLimitUnavailable
from nico_agent.web.registry import WebProviderRegistry

_BRAVE_SECRET = "web_search_brave_api_key"
_SAFE_SEARCH = frozenset({"off", "moderate", "strict"})


class WebSearchExecutor:
    spec = ToolDefinitionSpec(
        name="web.search",
        version="1.0.0",
        description=(
            "Search the current public Web. Results are untrusted external content; "
            "use web.fetch with the returned source and this call's tool_call_id before "
            "relying on important claims."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                "count": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                "language": {
                    "type": "string",
                    "pattern": "^[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?$",
                },
                "country": {"type": "string", "pattern": "^[A-Za-z]{2}$"},
                "freshness": {"enum": ["day", "week", "month", "year"]},
                "domains": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": 253},
                    "maxItems": 10,
                    "uniqueItems": True,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "provider": {"enum": ["brave", "searxng"]},
                "query": {"type": "string"},
                "results": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "url": {"type": "string"},
                            "snippet": {"type": "string"},
                            "published_at": {"type": ["string", "null"]},
                            "site_name": {"type": ["string", "null"]},
                        },
                        "required": [
                            "title",
                            "url",
                            "snippet",
                            "published_at",
                            "site_name",
                        ],
                        "additionalProperties": False,
                    },
                },
                "result_count": {"type": "integer", "minimum": 0, "maximum": 10},
                "cached": {"type": "boolean"},
                "took_ms": {"type": "integer", "minimum": 0},
                "external_content": {
                    "type": "object",
                    "properties": {
                        "untrusted": {"const": True},
                        "source": {"const": "web_search"},
                        "wrapped": {"const": True},
                        "provider": {"enum": ["brave", "searxng"]},
                    },
                    "required": ["untrusted", "source", "wrapped", "provider"],
                    "additionalProperties": False,
                },
            },
            "required": [
                "provider",
                "query",
                "results",
                "result_count",
                "cached",
                "took_ms",
                "external_content",
            ],
            "additionalProperties": False,
        },
        permission="network.web.search",
        timeout_seconds=30,
        retry_policy=ToolRetryPolicy(
            max_attempts=2,
            backoff_seconds=0.2,
            retryable_codes=frozenset({"WEB_PROVIDER_RATE_LIMITED", "WEB_PROVIDER_UNAVAILABLE"}),
        ),
        isolation=ToolIsolation.NETWORK,
        risk=ToolRisk.MEDIUM,
        max_output_bytes=131_072,
        secret_names=frozenset({_BRAVE_SECRET}),
    )
    implementation_hash = canonical_hash({"executor": "web.search", "revision": 1})

    def __init__(
        self,
        providers: WebProviderRegistry,
        *,
        cache: RedisWebSearchCache | Any | None = None,
        rate_limiter: RedisWebRateLimiter | Any | None = None,
        environment: str = "development",
    ) -> None:
        if environment not in {"development", "test", "production"}:
            raise ValueError("environment is invalid")
        self.providers = providers
        self.cache = cache
        self.rate_limiter = rate_limiter
        self.environment = environment

    def required_secret_names(self, tool_config: dict[str, Any]) -> frozenset[str]:
        provider, _ = self._provider_config(tool_config)
        return frozenset({_BRAVE_SECRET}) if provider == "brave" else frozenset()

    async def execute(self, context, arguments, secrets):
        provider_key, provider_config = self._provider_config(context.tool_config)
        try:
            request = SearchRequest.model_validate(arguments)
        except ValidationError as exc:
            raise ToolExecutorFailure("TOOL_INPUT_INVALID", "Web search input is invalid") from exc
        count_limit = _bounded_int(context.tool_config.get("count_limit"), default=10, maximum=10)
        request = request.model_copy(update={"count": min(request.count, count_limit)})
        policy_hash = canonical_hash(context.tool_config)
        started = time.monotonic()
        page = await self._cache_get(context.tenant_id, provider_key, policy_hash, request)
        cached = page is not None
        if page is None:
            await self._acquire_rate_limit(context, provider_key)
            provider = self.providers.get(provider_key)
            secret = secrets.get(_BRAVE_SECRET) if provider_key == "brave" else None
            try:
                page = await provider.search(
                    request,
                    secret=secret,
                    config=provider_config,
                )
            except SearchProviderError as exc:
                raise ToolExecutorFailure(exc.code, exc.message) from exc
            if page.provider != provider_key:
                raise ToolExecutorFailure(
                    "WEB_PROVIDER_PROTOCOL_ERROR",
                    "Web search Provider identity did not match the frozen configuration",
                )
            await self._cache_set(
                context.tenant_id,
                provider_key,
                policy_hash,
                request,
                page,
                context.tool_config,
            )
        assert isinstance(page, SearchPage)
        took_ms = max(0, int((time.monotonic() - started) * 1000))
        results = [item.model_dump(mode="json") for item in page.results]
        output = {
            "provider": provider_key,
            "query": request.query,
            "results": results,
            "result_count": len(results),
            "cached": cached,
            "took_ms": took_ms,
            "external_content": {
                "untrusted": True,
                "source": "web_search",
                "wrapped": True,
                "provider": provider_key,
            },
        }
        return ToolExecutionResult(
            output=output,
            usage={
                "provider": provider_key,
                "result_count": len(results),
                "cached": cached,
                "took_ms": took_ms,
            },
        )

    def _provider_config(self, tool_config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        provider = tool_config.get("provider")
        if provider not in {"brave", "searxng"}:
            raise ToolExecutorFailure(
                "WEB_SEARCH_NOT_CONFIGURED",
                "Web search Provider is not configured",
            )
        try:
            self.providers.get(provider)
        except SearchProviderError as exc:
            raise ToolExecutorFailure(exc.code, exc.message) from exc
        safe_search = tool_config.get("safe_search", "moderate")
        if safe_search not in _SAFE_SEARCH:
            raise ToolExecutorFailure(
                "WEB_SEARCH_NOT_CONFIGURED",
                "Web search SafeSearch policy is invalid",
            )
        return provider, {"safe_search": safe_search}

    async def _cache_get(self, tenant_id, provider, policy_hash, request):
        if self.cache is None:
            return None
        return await self.cache.get(tenant_id, provider, policy_hash, request)

    async def _cache_set(
        self,
        tenant_id,
        provider,
        policy_hash,
        request,
        page,
        tool_config,
    ) -> None:
        if self.cache is None:
            return
        ttl = _bounded_int(
            tool_config.get("cache_ttl_seconds"),
            default=900,
            maximum=86_400,
        )
        await self.cache.set(
            tenant_id,
            provider,
            policy_hash,
            request,
            page,
            ttl_seconds=ttl,
        )

    async def _acquire_rate_limit(self, context, provider: str) -> None:
        limit = _bounded_int(
            context.tool_config.get("rate_limit_per_minute"),
            default=20,
            maximum=100_000,
        )
        hard = self.environment == "production"
        if self.rate_limiter is None:
            if hard:
                raise ToolExecutorFailure(
                    "WEB_PROVIDER_RATE_LIMIT_UNAVAILABLE",
                    "Web search rate limit state is unavailable",
                )
            return
        try:
            decision = await self.rate_limiter.acquire(
                tenant_id=context.tenant_id,
                tool=self.spec.reference,
                provider=provider,
                limit=limit,
                hard=hard,
            )
        except WebRateLimitUnavailable as exc:
            raise ToolExecutorFailure(
                "WEB_PROVIDER_RATE_LIMIT_UNAVAILABLE",
                "Web search rate limit state is unavailable",
            ) from exc
        if not decision.allowed:
            raise ToolExecutorFailure(
                "WEB_PROVIDER_RATE_LIMITED",
                "Web search local rate limit was exceeded",
            )


def _bounded_int(value: Any, *, default: int, maximum: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= maximum:
        return value
    if value is None:
        return default
    raise ToolExecutorFailure(
        "WEB_SEARCH_NOT_CONFIGURED",
        "Web search numeric policy is invalid",
    )
