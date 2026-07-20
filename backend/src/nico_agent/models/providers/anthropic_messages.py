"""Anthropic Messages streaming and model discovery adapter."""

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
    parse_json_object,
    raise_transport_error,
)


class AnthropicMessagesProvider:
    name = "anthropic_messages"

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
        return frozenset({ModelCapability.STREAMING, ModelCapability.TOOLS})

    async def stream(self, request: ModelRequest):
        endpoint = await self.http.resolve_endpoint(
            request.endpoint, "v1/messages", model=request.model
        )
        secret = self.http.resolve_secret(request.endpoint)
        headers = self._headers(endpoint.host_header, secret)
        try:
            async with self.client.stream(
                "POST",
                endpoint.url,
                headers=headers,
                json=self._request_body(request),
                timeout=self.http.timeout(request.timeout_seconds),
                follow_redirects=False,
                extensions={"sni_hostname": endpoint.sni_hostname},
            ) as response:
                request_id = response.headers.get("request-id") or response.headers.get(
                    "x-request-id"
                )
                if response.status_code != 200:
                    content = await self.http.read_bounded(response)
                    self.http.raise_response_error(response.status_code, content, secret)
                yield ModelStreamEvent(
                    type=ModelStreamEventType.RESPONSE_STARTED,
                    provider_request_id=request_id,
                )
                input_tokens: int | None = None
                output_tokens: int | None = None
                finish_reason: str | None = None
                async for line in self.http.iter_bounded_lines(response):
                    if not line or line.startswith(":") or line.startswith("event:"):
                        continue
                    if not line.startswith("data:"):
                        raise ModelProtocolError("Anthropic stream contained an invalid SSE field")
                    event = parse_json_object(line[5:].strip(), "Anthropic stream event")
                    event_type = event.get("type")
                    if event_type == "message_start":
                        message = event.get("message")
                        if not isinstance(message, dict):
                            raise ModelProtocolError("Anthropic message_start is invalid")
                        usage = message.get("usage")
                        if isinstance(usage, dict):
                            input_tokens = normalize_token_count(usage.get("input_tokens"))
                    elif event_type == "content_block_start":
                        block = event.get("content_block")
                        index = event.get("index")
                        if not isinstance(block, dict) or type(index) is not int or index < 0:
                            raise ModelProtocolError("Anthropic content block is invalid")
                        if block.get("type") == "text" and block.get("text"):
                            yield ModelStreamEvent(
                                type=ModelStreamEventType.TEXT_DELTA,
                                text_delta=str(block["text"]),
                                provider_request_id=request_id,
                            )
                        elif block.get("type") == "tool_use":
                            arguments = block.get("input")
                            yield ModelStreamEvent(
                                type=ModelStreamEventType.TOOL_CALL_DELTA,
                                tool_index=index,
                                tool_call_id=str(block.get("id") or ""),
                                tool_name=str(block.get("name") or ""),
                                tool_arguments_delta=(
                                    json.dumps(arguments, separators=(",", ":"))
                                    if isinstance(arguments, dict) and arguments
                                    else None
                                ),
                                provider_request_id=request_id,
                            )
                    elif event_type == "content_block_delta":
                        delta = event.get("delta")
                        index = event.get("index")
                        if not isinstance(delta, dict) or type(index) is not int or index < 0:
                            raise ModelProtocolError("Anthropic content delta is invalid")
                        if delta.get("type") == "text_delta":
                            yield ModelStreamEvent(
                                type=ModelStreamEventType.TEXT_DELTA,
                                text_delta=str(delta.get("text") or ""),
                                provider_request_id=request_id,
                            )
                        elif delta.get("type") == "input_json_delta":
                            yield ModelStreamEvent(
                                type=ModelStreamEventType.TOOL_CALL_DELTA,
                                tool_index=index,
                                tool_arguments_delta=str(delta.get("partial_json") or ""),
                                provider_request_id=request_id,
                            )
                    elif event_type == "message_delta":
                        delta = event.get("delta")
                        usage = event.get("usage")
                        if isinstance(delta, dict) and delta.get("stop_reason") is not None:
                            finish_reason = str(delta["stop_reason"])
                        if isinstance(usage, dict):
                            output_tokens = normalize_token_count(usage.get("output_tokens"))
                    elif event_type == "error":
                        raise ModelProtocolError("Anthropic stream reported an error event")
                    elif event_type == "message_stop":
                        break
                usage = _usage(input_tokens, output_tokens)
                yield ModelStreamEvent(
                    type=ModelStreamEventType.RESPONSE_COMPLETED,
                    finish_reason=finish_reason,
                    usage=usage,
                    provider_request_id=request_id,
                )
        except (ModelProviderError, ModelProtocolError):
            raise
        except httpx.HTTPError as exc:
            raise_transport_error(exc)

    async def discover(self, request: ModelDiscoveryRequest) -> ModelDiscoveryResult:
        endpoint = await self.http.resolve_endpoint(request.endpoint, "v1/models")
        secret = self.http.resolve_secret(request.endpoint)
        models: list[DiscoveredModel] = []
        seen: set[str] = set()
        after_id: str | None = None
        truncated = False
        for _page in range(20):
            params = {"limit": min(100, request.limit + 1)}
            if after_id:
                params["after_id"] = after_id
            try:
                response = await self.client.get(
                    endpoint.url,
                    params=params,
                    headers=self._headers(endpoint.host_header, secret),
                    timeout=self.http.timeout(30),
                    follow_redirects=False,
                    extensions={"sni_hostname": endpoint.sni_hostname},
                )
                payload = await self.http.json_response(response, secret)
            except (ModelProviderError, ModelProtocolError):
                raise
            except httpx.HTTPError as exc:
                raise_transport_error(exc)
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ModelProtocolError("Anthropic model discovery data must be an array")
            for item in payload["data"]:
                model_id = normalize_discovered_model_id(
                    item.get("id") if isinstance(item, dict) else None
                )
                if model_id is None or model_id in seen:
                    continue
                seen.add(model_id)
                if len(models) >= request.limit:
                    truncated = True
                    continue
                display = item.get("display_name") if isinstance(item, dict) else None
                models.append(
                    DiscoveredModel(
                        id=model_id,
                        display_name=(
                            clean_external_text(display, 200) if isinstance(display, str) else None
                        ),
                    )
                )
            if not payload.get("has_more"):
                break
            after_id = payload.get("last_id") if isinstance(payload.get("last_id"), str) else None
            if not after_id or len(models) >= request.limit:
                truncated = True
                break
        else:
            truncated = True
        return ModelDiscoveryResult(models=tuple(models), truncated=truncated)

    @staticmethod
    def _headers(host_header: str, secret: str) -> dict[str, str]:
        return {
            "x-api-key": secret,
            "anthropic-version": "2023-06-01",
            "Accept": "application/json",
            "Connection": "close",
            "Content-Type": "application/json",
            "Host": host_header,
            "User-Agent": "Nico-Agent-Model-Gateway/1.0",
        }

    @staticmethod
    def _request_body(request: ModelRequest) -> dict[str, Any]:
        system = "\n\n".join(
            message.content or "" for message in request.messages if message.role == "system"
        )
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [
                _message_body(message) for message in request.messages if message.role != "system"
            ],
            "max_tokens": request.max_output_tokens or 4096,
            "stream": True,
        }
        if system:
            body["system"] = system
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.tools:
            body["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in request.tools
            ]
        return body


def _message_body(message: Any) -> dict[str, Any]:
    if message.role == "tool":
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content or "",
                }
            ],
        }
    content: Any = message.content or ""
    if message.role == "assistant" and message.tool_calls:
        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        for call in message.tool_calls:
            function = call.get("function", {}) if isinstance(call, dict) else {}
            arguments = function.get("arguments", {}) if isinstance(function, dict) else {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise ModelProtocolError("assistant tool arguments are not valid JSON") from exc
            blocks.append(
                {
                    "type": "tool_use",
                    "id": str(call.get("id") or ""),
                    "name": str(function.get("name") or ""),
                    "input": arguments,
                }
            )
        content = blocks
    return {"role": "assistant" if message.role == "assistant" else "user", "content": content}


def _usage(input_tokens: int | None, output_tokens: int | None) -> ModelUsage:
    total = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else None
    )
    return ModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total,
        status="exact" if total is not None else "partial",
    )
