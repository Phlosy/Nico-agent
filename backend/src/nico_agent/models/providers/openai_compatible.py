"""OpenAI-compatible Chat Completions streaming provider."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from nico_agent.models.contracts import (
    ModelCapability,
    ModelRequest,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelUsage,
)
from nico_agent.models.errors import (
    ModelCredentialUnavailable,
    ModelEndpointDenied,
    ModelProtocolError,
    ModelProviderError,
)
from nico_agent.tools.secrets import redact_value

_MODEL_ENV_REFERENCE = re.compile(r"^env:(NICO_MODEL_SECRET_[A-Z0-9_]{1,100})$")


class ModelSecretResolver(Protocol):
    def resolve(self, name: str, reference: str) -> str: ...


class EnvironmentModelSecretResolver:
    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self.environment = environment if environment is not None else os.environ

    def resolve(self, name: str, reference: str) -> str:
        match = _MODEL_ENV_REFERENCE.fullmatch(reference)
        if match is None:
            raise ModelCredentialUnavailable()
        value = self.environment.get(match.group(1))
        if not value:
            raise ModelCredentialUnavailable()
        return value


Resolver = Any


@dataclass(frozen=True, slots=True)
class _ResolvedEndpoint:
    url: str
    host_header: str
    sni_hostname: str


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
        self.client = client or httpx.AsyncClient(follow_redirects=False)
        self.secret_resolver = secret_resolver or EnvironmentModelSecretResolver()
        self.resolver = resolver or _resolve_addresses
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_response_bytes = max_response_bytes
        self.allow_http_loopback = allow_http_loopback
        self.trusted_private_hosts = frozenset(
            host.rstrip(".").encode("idna").decode("ascii").lower()
            for host in trusted_private_hosts
        )
        self.allow_http_trusted_hosts = allow_http_trusted_hosts

    def describe_capabilities(self) -> frozenset[ModelCapability]:
        return frozenset(
            {
                ModelCapability.STREAMING,
                ModelCapability.TOOLS,
                ModelCapability.STRUCTURED_OUTPUT,
            }
        )

    async def stream(self, request: ModelRequest):
        endpoint = await self._endpoint_url(request)
        credential_ref = request.endpoint.get("credential_ref")
        if not isinstance(credential_ref, str) or not credential_ref:
            raise ModelCredentialUnavailable()
        secret = self.secret_resolver.resolve("model_api_key", credential_ref)
        body = self._request_body(request)
        timeout = httpx.Timeout(
            connect=self.connect_timeout,
            read=request.timeout_seconds or self.read_timeout,
            write=self.connect_timeout,
            pool=self.connect_timeout,
        )
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
                    content = await self._read_bounded(response)
                    self._raise_response_error(response.status_code, content, secret)
                yield ModelStreamEvent(
                    type=ModelStreamEventType.RESPONSE_STARTED,
                    provider_request_id=request_id,
                )
                finish_reason = None
                usage = ModelUsage()
                async for line in self._iter_bounded_lines(response):
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
                            yield self._tool_delta(tool_call, request_id)
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
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise ModelProviderError(
                "MODEL_PROVIDER_UNAVAILABLE",
                "model provider connection failed",
                retryable=True,
            ) from exc
        except httpx.ReadTimeout as exc:
            raise ModelProviderError(
                "MODEL_PROVIDER_TIMEOUT",
                "model provider response timed out",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelProviderError(
                "MODEL_PROVIDER_NETWORK_ERROR",
                "model provider request failed",
                retryable=True,
            ) from exc

    async def _endpoint_url(self, request: ModelRequest) -> _ResolvedEndpoint:
        base_url = request.endpoint.get("base_url")
        if not isinstance(base_url, str):
            raise ModelEndpointDenied("model endpoint URL is invalid")
        try:
            parsed = urlsplit(base_url)
            hostname = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise ModelEndpointDenied("model endpoint URL is invalid") from exc
        if (
            not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.scheme not in {"https", "http"}
        ):
            raise ModelEndpointDenied("model endpoint URL is invalid")
        try:
            normalized_host = hostname.rstrip(".").encode("idna").decode("ascii").lower()
            addresses: Sequence[str] = await asyncio.to_thread(self.resolver, normalized_host, port)
        except (OSError, UnicodeError) as exc:
            raise ModelEndpointDenied("model endpoint DNS resolution failed") from exc
        if not addresses:
            raise ModelEndpointDenied("model endpoint DNS resolution returned no address")
        try:
            parsed_addresses = tuple(ipaddress.ip_address(value) for value in addresses)
        except ValueError as exc:
            raise ModelEndpointDenied(
                "model endpoint DNS resolution returned an invalid address"
            ) from exc
        loopback_allowed = (
            self.allow_http_loopback
            and bool(request.endpoint.get("tls_policy", {}).get("allow_http_loopback", False))
            and all(address.is_loopback for address in parsed_addresses)
        )
        trusted_private = normalized_host in self.trusted_private_hosts
        if any(not address.is_global for address in parsed_addresses) and not (
            loopback_allowed or trusted_private
        ):
            raise ModelEndpointDenied("model endpoint resolved to a non-public address")
        plain_http_allowed = loopback_allowed or (trusted_private and self.allow_http_trusted_hosts)
        if parsed.scheme != "https" and not plain_http_allowed:
            raise ModelEndpointDenied("model endpoint requires HTTPS")
        allowed_models = request.endpoint.get("allowed_models", [])
        if allowed_models and request.model not in allowed_models:
            raise ModelEndpointDenied("model is not allowed by this endpoint revision")
        # Connect to an address from the validated set instead of resolving the
        # hostname again inside the HTTP stack.  Host and SNI keep normal HTTP
        # routing and certificate verification semantics.
        pinned_address = str(parsed_addresses[0])
        normalized = parsed._replace(netloc=_netloc(pinned_address, port, parsed.scheme))
        return _ResolvedEndpoint(
            url=urlunsplit(normalized).rstrip("/") + "/chat/completions",
            host_header=_netloc(normalized_host, port, parsed.scheme),
            sni_hostname=normalized_host,
        )

    async def _read_bounded(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > self.max_response_bytes:
                raise ModelProtocolError("model response exceeded its byte limit")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _iter_bounded_lines(self, response: httpx.Response):
        buffer = bytearray()
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > self.max_response_bytes:
                raise ModelProtocolError("model response exceeded its byte limit")
            buffer.extend(chunk)
            while (newline := buffer.find(b"\n")) >= 0:
                line = bytes(buffer[:newline]).removesuffix(b"\r")
                del buffer[: newline + 1]
                try:
                    yield line.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ModelProtocolError("model stream is not valid UTF-8") from exc
        if buffer:
            try:
                yield bytes(buffer).removesuffix(b"\r").decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ModelProtocolError("model stream is not valid UTF-8") from exc

    @staticmethod
    def _request_body(request: ModelRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [_message_body(message) for message in request.messages],
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
                        "name": tool.name,
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
        prompt = _token_count(value.get("prompt_tokens"))
        completion = _token_count(value.get("completion_tokens"))
        total = _token_count(value.get("total_tokens"))
        exact = all(item is not None for item in (prompt, completion, total))
        partial = any(item is not None for item in (prompt, completion, total))
        return ModelUsage(
            input_tokens=prompt,
            output_tokens=completion,
            total_tokens=total,
            status="exact" if exact else "partial" if partial else "missing",
        )

    @staticmethod
    def _tool_delta(value: Any, request_id: str | None) -> ModelStreamEvent:
        if not isinstance(value, dict) or type(value.get("index")) is not int or value["index"] < 0:
            raise ModelProtocolError("tool call delta is invalid")
        function = value.get("function") or {}
        if not isinstance(function, dict):
            raise ModelProtocolError("tool call function delta is invalid")
        return ModelStreamEvent(
            type=ModelStreamEventType.TOOL_CALL_DELTA,
            tool_index=value["index"],
            tool_call_id=str(value["id"]) if value.get("id") is not None else None,
            tool_name=str(function["name"]) if function.get("name") is not None else None,
            tool_arguments_delta=(
                str(function["arguments"]) if function.get("arguments") is not None else None
            ),
            provider_request_id=request_id,
        )

    @staticmethod
    def _raise_response_error(status: int, content: bytes, secret: str) -> None:
        message = f"model provider rejected the request with HTTP {status}"
        try:
            body = json.loads(content[:65_536])
            detail = body.get("error", {}).get("message") if isinstance(body, dict) else None
            if isinstance(detail, str) and detail:
                message = detail[:1000]
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        message = str(redact_value(message, {"model_api_key": secret}))
        raise ModelProviderError(
            "MODEL_PROVIDER_REJECTED",
            message,
            retryable=status == 429 or 500 <= status < 600,
        )


def _resolve_addresses(hostname: str, port: int) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item[4][0] for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        )
    )


def _message_body(message: Any) -> dict[str, Any]:
    body = message.model_dump(exclude_none=True)
    if not message.tool_calls:
        body.pop("tool_calls", None)
    return body


def _token_count(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ModelProtocolError("model usage token counts must be non-negative integers")
    return value


def _netloc(hostname: str, port: int, scheme: str) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    return host if port == default_port else f"{host}:{port}"
