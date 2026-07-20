"""Google Gemini generateContent streaming and model discovery adapter."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

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


class GoogleGeminiProvider:
    name = "google_gemini"

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
        model = request.model.removeprefix("models/")
        endpoint = await self.http.resolve_endpoint(
            request.endpoint,
            f"models/{quote(model, safe='-._~')}:streamGenerateContent",
            model=request.model,
        )
        secret = self.http.resolve_secret(request.endpoint)
        try:
            async with self.client.stream(
                "POST",
                endpoint.url,
                params={"alt": "sse"},
                headers=self._headers(endpoint.host_header, secret),
                json=self._request_body(request),
                timeout=self.http.timeout(request.timeout_seconds),
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
                finish_reason: str | None = None
                usage = ModelUsage()
                tool_index = 0
                async for line in self.http.iter_bounded_lines(response):
                    if not line or line.startswith(":") or line.startswith("event:"):
                        continue
                    if not line.startswith("data:"):
                        raise ModelProtocolError("Gemini stream contained an invalid SSE field")
                    chunk = parse_json_object(line[5:].strip(), "Gemini stream chunk")
                    parsed_usage = _usage(chunk.get("usageMetadata"))
                    if parsed_usage is not None:
                        usage = parsed_usage
                        yield ModelStreamEvent(
                            type=ModelStreamEventType.USAGE,
                            usage=usage,
                            provider_request_id=request_id,
                        )
                    candidates = chunk.get("candidates", [])
                    if not isinstance(candidates, list):
                        raise ModelProtocolError("Gemini candidates must be an array")
                    for candidate in candidates:
                        if not isinstance(candidate, dict):
                            raise ModelProtocolError("Gemini candidate must be an object")
                        if candidate.get("finishReason") is not None:
                            finish_reason = str(candidate["finishReason"])
                        content = candidate.get("content") or {}
                        parts = content.get("parts", []) if isinstance(content, dict) else []
                        if not isinstance(parts, list):
                            raise ModelProtocolError("Gemini content parts must be an array")
                        for part in parts:
                            if not isinstance(part, dict):
                                raise ModelProtocolError("Gemini content part must be an object")
                            if part.get("text") is not None:
                                yield ModelStreamEvent(
                                    type=ModelStreamEventType.TEXT_DELTA,
                                    text_delta=str(part["text"]),
                                    provider_request_id=request_id,
                                )
                            function = part.get("functionCall")
                            if isinstance(function, dict):
                                yield ModelStreamEvent(
                                    type=ModelStreamEventType.TOOL_CALL_DELTA,
                                    tool_index=tool_index,
                                    tool_call_id=f"gemini-{tool_index}",
                                    tool_name=str(function.get("name") or ""),
                                    tool_arguments_delta=json.dumps(
                                        function.get("args") or {}, separators=(",", ":")
                                    ),
                                    provider_request_id=request_id,
                                )
                                tool_index += 1
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
        endpoint = await self.http.resolve_endpoint(request.endpoint, "models")
        secret = self.http.resolve_secret(request.endpoint)
        models: list[DiscoveredModel] = []
        seen: set[str] = set()
        page_token: str | None = None
        truncated = False
        for _page in range(20):
            params: dict[str, Any] = {"pageSize": min(1000, request.limit + 1)}
            if page_token:
                params["pageToken"] = page_token
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
            if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
                raise ModelProtocolError("Gemini model discovery data must be an array")
            for item in payload["models"]:
                if not isinstance(item, dict):
                    continue
                methods = item.get("supportedGenerationMethods", [])
                if not isinstance(methods, list) or "generateContent" not in methods:
                    continue
                raw_id = item.get("name")
                model_id = normalize_discovered_model_id(
                    raw_id.removeprefix("models/") if isinstance(raw_id, str) else None
                )
                if model_id is None or model_id in seen:
                    continue
                seen.add(model_id)
                if len(models) >= request.limit:
                    truncated = True
                    continue
                display = item.get("displayName")
                models.append(
                    DiscoveredModel(
                        id=model_id,
                        display_name=(
                            clean_external_text(display, 200) if isinstance(display, str) else None
                        ),
                    )
                )
            page_token = (
                payload.get("nextPageToken")
                if isinstance(payload.get("nextPageToken"), str)
                else None
            )
            if not page_token:
                break
            if len(models) >= request.limit:
                truncated = True
                break
        else:
            truncated = True
        return ModelDiscoveryResult(models=tuple(models), truncated=truncated)

    @staticmethod
    def _headers(host_header: str, secret: str) -> dict[str, str]:
        return {
            "x-goog-api-key": secret,
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
            "contents": [
                _message_body(message) for message in request.messages if message.role != "system"
            ],
            "generationConfig": {},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if request.temperature is not None:
            body["generationConfig"]["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            body["generationConfig"]["maxOutputTokens"] = request.max_output_tokens
        if request.tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.input_schema,
                        }
                        for tool in request.tools
                    ]
                }
            ]
        return body


def _message_body(message: Any) -> dict[str, Any]:
    role = "model" if message.role == "assistant" else "user"
    if message.role == "tool":
        return {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "name": message.name or "tool",
                        "response": {"result": message.content or ""},
                    }
                }
            ],
        }
    parts: list[dict[str, Any]] = []
    if message.content is not None:
        parts.append({"text": message.content})
    for call in message.tool_calls:
        function = call.get("function", {}) if isinstance(call, dict) else {}
        arguments = function.get("arguments", {}) if isinstance(function, dict) else {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ModelProtocolError("assistant tool arguments are not valid JSON") from exc
        parts.append(
            {
                "functionCall": {
                    "name": str(function.get("name") or ""),
                    "args": arguments,
                }
            }
        )
    return {"role": role, "parts": parts}


def _usage(value: Any) -> ModelUsage | None:
    if not isinstance(value, dict):
        return None
    prompt = normalize_token_count(value.get("promptTokenCount"))
    output = normalize_token_count(value.get("candidatesTokenCount"))
    total = normalize_token_count(value.get("totalTokenCount"))
    exact = all(item is not None for item in (prompt, output, total))
    partial = any(item is not None for item in (prompt, output, total))
    return ModelUsage(
        input_tokens=prompt,
        output_tokens=output,
        total_tokens=total,
        status="exact" if exact else "partial" if partial else "missing",
    )
