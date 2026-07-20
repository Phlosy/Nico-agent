"""Provider-neutral model orchestration, retries, rate limiting and assembly."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from typing import Any, Protocol

from nico_agent.models.contracts import (
    ModelCapability,
    ModelDiscoveryProvider,
    ModelDiscoveryRequest,
    ModelDiscoveryResult,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelToolCall,
    ModelUsage,
)
from nico_agent.models.errors import (
    ModelCapabilityMismatch,
    ModelDiscoveryUnavailable,
    ModelProtocolError,
    ModelProviderError,
    ModelRateLimited,
    ModelRateLimitUnavailable,
)
from nico_agent.models.registry import ModelProviderRegistry


class ModelRateLimiter(Protocol):
    async def acquire(self, key: str, limit: int, period_seconds: int) -> bool: ...


class RedisModelRateLimiter:
    """Small atomic fixed-window limiter shared by every worker process."""

    _SCRIPT = """
    local current = redis.call('INCR', KEYS[1])
    if current == 1 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
    if current > tonumber(ARGV[1]) then return 0 end
    return 1
    """

    def __init__(self, redis_client: Any, *, prefix: str = "nico:model-limit") -> None:
        self.redis = redis_client
        self.prefix = prefix

    async def acquire(self, key: str, limit: int, period_seconds: int) -> bool:
        result = await self.redis.eval(
            self._SCRIPT,
            1,
            f"{self.prefix}:{key}",
            limit,
            period_seconds,
        )
        return bool(result)


class ModelGateway:
    def __init__(
        self,
        registry: ModelProviderRegistry,
        *,
        rate_limiter: ModelRateLimiter | None = None,
        max_attempts: int = 3,
        retry_base_seconds: float = 0.2,
    ) -> None:
        if max_attempts < 1 or max_attempts > 10:
            raise ValueError("model max_attempts must be between 1 and 10")
        if retry_base_seconds < 0:
            raise ValueError("model retry_base_seconds must not be negative")
        self.registry = registry
        self.rate_limiter = rate_limiter
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        protocol = str(request.endpoint.get("protocol", ""))
        provider = self.registry.get(protocol)
        self._require_capabilities(provider.describe_capabilities(), request)
        await self._apply_rate_limit(request)

        attempt = 0
        while True:
            attempt += 1
            emitted = False
            try:
                async for event in provider.stream(request):
                    if event.type in {
                        ModelStreamEventType.TEXT_DELTA,
                        ModelStreamEventType.TOOL_CALL_DELTA,
                    }:
                        emitted = True
                    yield event
                return
            except ModelProviderError as exc:
                if emitted or not exc.retryable or attempt >= self.max_attempts:
                    raise
                if self.retry_base_seconds:
                    backoff = self.retry_base_seconds * (2 ** (attempt - 1))
                    await asyncio.sleep(backoff + random.uniform(0, self.retry_base_seconds))

    async def complete(self, request: ModelRequest) -> ModelResponse:
        text_parts: list[str] = []
        tool_parts: dict[int, dict[str, str]] = {}
        finish_reason = None
        usage = ModelUsage()
        request_id = None
        async for event in self.stream(request):
            if event.text_delta:
                text_parts.append(event.text_delta)
            if event.type is ModelStreamEventType.TOOL_CALL_DELTA:
                index = event.tool_index or 0
                part = tool_parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if event.tool_call_id:
                    part["id"] = event.tool_call_id
                if event.tool_name:
                    part["name"] = event.tool_name
                if event.tool_arguments_delta:
                    part["arguments"] += event.tool_arguments_delta
            if event.finish_reason is not None:
                finish_reason = event.finish_reason
            if event.usage is not None:
                usage = event.usage
            if event.provider_request_id is not None:
                request_id = event.provider_request_id
        tool_calls = tuple(
            self._tool_call(index, value) for index, value in sorted(tool_parts.items())
        )
        return ModelResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            provider_request_id=request_id,
        )

    async def discover(self, request: ModelDiscoveryRequest) -> ModelDiscoveryResult:
        protocol = str(request.endpoint.get("protocol", ""))
        provider = self.registry.get(protocol)
        if not isinstance(provider, ModelDiscoveryProvider):
            raise ModelDiscoveryUnavailable()
        try:
            result = await provider.discover(request)
        except ModelProviderError as exc:
            raise ModelDiscoveryUnavailable(exc.message) from exc
        if not isinstance(result, ModelDiscoveryResult):
            raise ModelProtocolError("model discovery returned an invalid result")
        return result

    @staticmethod
    def _tool_call(index: int, value: dict[str, str]) -> ModelToolCall:
        if not value["id"] or not value["name"]:
            raise ModelProtocolError(f"tool call {index} is missing id or function name")
        try:
            arguments = json.loads(value["arguments"] or "{}")
        except json.JSONDecodeError as exc:
            raise ModelProtocolError(f"tool call {index} arguments are not valid JSON") from exc
        if not isinstance(arguments, dict):
            raise ModelProtocolError(f"tool call {index} arguments must be a JSON object")
        return ModelToolCall(id=value["id"], name=value["name"], arguments=arguments)

    @staticmethod
    def _require_capabilities(
        capabilities: frozenset[ModelCapability], request: ModelRequest
    ) -> None:
        endpoint_capabilities = request.endpoint.get("capabilities", {})
        if ModelCapability.STREAMING not in capabilities or not endpoint_capabilities.get(
            "streaming", True
        ):
            raise ModelCapabilityMismatch(ModelCapability.STREAMING.value)
        if request.tools and (
            ModelCapability.TOOLS not in capabilities
            or not endpoint_capabilities.get("tools", False)
        ):
            raise ModelCapabilityMismatch(ModelCapability.TOOLS.value)
        if request.response_format and (
            ModelCapability.STRUCTURED_OUTPUT not in capabilities
            or not endpoint_capabilities.get("structured_output", False)
        ):
            raise ModelCapabilityMismatch(ModelCapability.STRUCTURED_OUTPUT.value)

    async def _apply_rate_limit(self, request: ModelRequest) -> None:
        config = request.endpoint.get("rate_limit", {})
        if not isinstance(config, dict):
            return
        limit = config.get("requests_per_minute")
        if not isinstance(limit, int) or limit <= 0:
            return
        mode = config.get("mode", "hard")
        if self.rate_limiter is None:
            if mode == "hard":
                raise ModelRateLimitUnavailable()
            return
        key = f"{request.endpoint.get('id', 'unknown')}:{request.model}"
        try:
            allowed = await self.rate_limiter.acquire(key, limit, 60)
        except Exception as exc:
            if mode == "hard":
                raise ModelRateLimitUnavailable() from exc
            return
        if not allowed:
            raise ModelRateLimited()
