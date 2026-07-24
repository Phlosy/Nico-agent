"""OpenAI-compatible Chat Completions streaming provider."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx

from nico_agent.models.contracts import (
    DiscoveredModel,
    ModelCapability,
    ModelDiscoveryRequest,
    ModelDiscoveryResult,
    ModelRequest,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelUsage,
)
from nico_agent.models.errors import ModelProtocolError, ModelProviderError
from nico_agent.models.http_safety import (
    ModelSecretResolver,
    Resolver,
    SafeModelHttpTransport,
    clean_external_text,
    normalize_discovered_model_id,
    normalize_token_count,
    raise_transport_error,
)
from nico_agent.models.tool_names import ProviderToolNames


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        secret_resolver: ModelSecretResolver | None = None,
        resolver: Resolver | None = None,
        connect_timeout: float = 10,
        read_timeout: float = 120,
        max_response_bytes: int = 10_485_760,
        allow_http_loopback: bool = False,
        trusted_private_hosts: Sequence[str] = (),
        allow_http_trusted_hosts: bool = False,
    ) -> None:
        self.http = SafeModelHttpTransport(
            client=client,
            secret_resolver=secret_resolver,
            resolver=resolver,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            max_response_bytes=max_response_bytes,
            allow_http_loopback=allow_http_loopback,
            trusted_private_hosts=trusted_private_hosts,
            allow_http_trusted_hosts=allow_http_trusted_hosts,
        )
        self.client = self.http.client

    def describe_capabilities(self) -> frozenset[ModelCapability]:
        return frozenset(
            {
                ModelCapability.STREAMING,
                ModelCapability.JSON_OBJECT,
                ModelCapability.JSON_SCHEMA,
                ModelCapability.NATIVE_TOOL_CALLING,
            }
        )

    async def stream(self, request: ModelRequest):
        endpoint = await self.http.resolve_endpoint(
            request.endpoint, "chat/completions", model=request.model
        )
        secret = self.http.resolve_secret(request.endpoint)
        tool_names = ProviderToolNames.from_definitions(request.tools)
        body = self._request_body(request, tool_names)
        timeout = self.http.timeout(request.timeout_seconds)
        try:
            async with self.client.stream(
                "POST",
                endpoint.url,
                headers={
                    "Authorization": f"Bearer {secret}",
                    "Accept": "text/event-stream",
                    "Connection": "close",
                    "Content-Type": "application/json",
                    "Host": endpoint.host_header,
                    "User-Agent": "Nico-Agent-Model-Gateway/1.0",
                },
                json=body,
                timeout=timeout,
                follow_redirects=False,
                extensions={"sni_hostname": endpoint.sni_hostname},
            ) as response:
                request_id = response.headers.get("x-request-id")
                if response.status_code != 200:
                    content = await self.http.read_bounded(response)
                    self.http.raise_response_error(response.status_code, content, secret)
                yield ModelStreamEvent(
                    type=ModelStreamEventType.RESPONSE_STARTED,
                    provider_request_id=request_id,
                )
                finish_reason = None
                usage = ModelUsage()
                async for line in self.http.iter_bounded_lines(response):
                    if not line or line.startswith(":") or line.startswith("event:"):
                        continue
                    if not line.startswith("data:"):
                        raise ModelProtocolError("model stream contained an invalid SSE field")
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = self._chunk(data)
                    parsed_usage = self._usage(chunk.get("usage"))
                    if parsed_usage is not None:
                        usage = parsed_usage
                        yield ModelStreamEvent(
                            type=ModelStreamEventType.USAGE,
                            usage=usage,
                            provider_request_id=request_id,
                        )
                    choices = chunk.get("choices", [])
                    if not isinstance(choices, list):
                        raise ModelProtocolError("model chunk choices must be an array")
                    for choice in choices:
                        if not isinstance(choice, dict):
                            raise ModelProtocolError("model choice must be an object")
                        if choice.get("finish_reason") is not None:
                            finish_reason = str(choice["finish_reason"])
                        delta = choice.get("delta") or {}
                        if not isinstance(delta, dict):
                            raise ModelProtocolError("model choice delta must be an object")
                        content = delta.get("content")
                        if content is not None:
                            yield ModelStreamEvent(
                                type=ModelStreamEventType.TEXT_DELTA,
                                text_delta=str(content),
                                provider_request_id=request_id,
                            )
                        tool_calls = delta.get("tool_calls", [])
                        if not isinstance(tool_calls, list):
                            raise ModelProtocolError("tool call delta must be an array")
                        for tool_call in tool_calls:
                            yield self._tool_delta(tool_call, request_id, tool_names)
                yield ModelStreamEvent(
                    type=ModelStreamEventType.RESPONSE_COMPLETED,
                    finish_reason=finish_reason,
                    usage=usage,
                    provider_request_id=request_id,
                )
        except ModelProviderError:
            raise
        except ModelProtocolError:
            raise
        except httpx.HTTPError as exc:
            raise_transport_error(exc)

    async def discover(self, request: ModelDiscoveryRequest) -> ModelDiscoveryResult:
        endpoint = await self.http.resolve_endpoint(request.endpoint, "models")
        secret = self.http.resolve_secret(request.endpoint)
        try:
            response = await self.client.get(
                endpoint.url,
                headers={
                    "Authorization": f"Bearer {secret}",
                    "Connection": "close",
                    "Host": endpoint.host_header,
                    "User-Agent": "Nico-Agent-Model-Gateway/1.0",
                },
                timeout=self.http.timeout(30),
                follow_redirects=False,
                extensions={"sni_hostname": endpoint.sni_hostname},
            )
            payload = await self.http.json_response(response, secret)
        except ModelProviderError:
            raise
        except ModelProtocolError:
            raise
        except httpx.HTTPError as exc:
            raise_transport_error(exc)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ModelProtocolError("model discovery data must be an array")
        models: list[DiscoveredModel] = []
        seen: set[str] = set()
        overflow = False
        for item in payload["data"]:
            model_id = normalize_discovered_model_id(
                item.get("id") if isinstance(item, dict) else None
            )
            if model_id is None or model_id in seen:
                continue
            seen.add(model_id)
            if len(models) >= request.limit:
                overflow = True
                continue
            display = item.get("name") if isinstance(item, dict) else None
            models.append(
                DiscoveredModel(
                    id=model_id,
                    display_name=(
                        clean_external_text(display, 200) if isinstance(display, str) else None
                    ),
                )
            )
        return ModelDiscoveryResult(models=tuple(models), truncated=overflow)

    @staticmethod
    def _request_body(
        request: ModelRequest,
        tool_names: ProviderToolNames | None = None,
    ) -> dict[str, Any]:
        tool_names = tool_names or ProviderToolNames.from_definitions(request.tools)
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [_message_body(message, tool_names) for message in request.messages],
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            body["max_tokens"] = request.max_output_tokens
        if request.response_format is not None:
            body["response_format"] = request.response_format
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool_names.encode(tool.name),
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in request.tools
            ]
        return body

    @staticmethod
    def _chunk(data: str) -> dict[str, Any]:
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ModelProtocolError("model stream contained invalid JSON") from exc
        if not isinstance(chunk, dict):
            raise ModelProtocolError("model stream chunk must be an object")
        return chunk

    @staticmethod
    def _usage(value: Any) -> ModelUsage | None:
        if not isinstance(value, dict):
            return None
        prompt = normalize_token_count(value.get("prompt_tokens"))
        completion = normalize_token_count(value.get("completion_tokens"))
        total = normalize_token_count(value.get("total_tokens"))
        exact = all(item is not None for item in (prompt, completion, total))
        partial = any(item is not None for item in (prompt, completion, total))
        return ModelUsage(
            input_tokens=prompt,
            output_tokens=completion,
            total_tokens=total,
            status="exact" if exact else "partial" if partial else "missing",
        )

    @staticmethod
    def _tool_delta(
        value: Any,
        request_id: str | None,
        tool_names: ProviderToolNames,
    ) -> ModelStreamEvent:
        if not isinstance(value, dict) or type(value.get("index")) is not int or value["index"] < 0:
            raise ModelProtocolError("tool call delta is invalid")
        function = value.get("function") or {}
        if not isinstance(function, dict):
            raise ModelProtocolError("tool call function delta is invalid")
        return ModelStreamEvent(
            type=ModelStreamEventType.TOOL_CALL_DELTA,
            tool_index=value["index"],
            tool_call_id=str(value["id"]) if value.get("id") is not None else None,
            tool_name=(
                tool_names.decode(str(function["name"]))
                if function.get("name") is not None
                else None
            ),
            tool_arguments_delta=(
                str(function["arguments"]) if function.get("arguments") is not None else None
            ),
            provider_request_id=request_id,
        )


def _message_body(message: Any, tool_names: ProviderToolNames) -> dict[str, Any]:
    body = message.model_dump(exclude_none=True)
    if not message.tool_calls:
        body.pop("tool_calls", None)
    else:
        for call in body.get("tool_calls", []):
            function = call.get("function") if isinstance(call, dict) else None
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                function["name"] = tool_names.encode(function["name"])
    if isinstance(body.get("name"), str):
        body["name"] = tool_names.encode(body["name"])
    return body
