"""Shared DNS-pinned, bounded HTTP transport policy for model adapters."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import unicodedata
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

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
class ResolvedEndpoint:
    url: str
    host_header: str
    sni_hostname: str


class SafeModelHttpTransport:
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
        self.resolver = resolver or resolve_addresses
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_response_bytes = max_response_bytes
        self.allow_http_loopback = allow_http_loopback
        self.trusted_private_hosts = frozenset(
            host.rstrip(".").encode("idna").decode("ascii").lower()
            for host in trusted_private_hosts
        )
        self.allow_http_trusted_hosts = allow_http_trusted_hosts

    def resolve_secret(self, endpoint: Mapping[str, Any]) -> str:
        reference = endpoint.get("credential_ref")
        if not isinstance(reference, str) or not reference:
            raise ModelCredentialUnavailable()
        return self.secret_resolver.resolve("model_api_key", reference)

    def timeout(self, read_timeout: float | None = None) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_timeout,
            read=read_timeout or self.read_timeout,
            write=self.connect_timeout,
            pool=self.connect_timeout,
        )

    async def resolve_endpoint(
        self,
        endpoint: Mapping[str, Any],
        suffix: str,
        *,
        model: str | None = None,
    ) -> ResolvedEndpoint:
        base_url = endpoint.get("base_url")
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
        tls_policy = endpoint.get("tls_policy", {})
        loopback_allowed = (
            self.allow_http_loopback
            and isinstance(tls_policy, dict)
            and bool(tls_policy.get("allow_http_loopback", False))
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
        allowed_models = endpoint.get("allowed_models", [])
        if model is not None and allowed_models and model not in allowed_models:
            raise ModelEndpointDenied("model is not allowed by this endpoint revision")

        pinned_address = str(parsed_addresses[0])
        path = f"{parsed.path.rstrip('/')}/{suffix.lstrip('/')}"
        normalized = parsed._replace(
            netloc=netloc(pinned_address, port, parsed.scheme),
            path=path,
        )
        return ResolvedEndpoint(
            url=urlunsplit(normalized),
            host_header=netloc(normalized_host, port, parsed.scheme),
            sni_hostname=normalized_host,
        )

    async def read_bounded(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > self.max_response_bytes:
                raise ModelProtocolError("model response exceeded its byte limit")
            chunks.append(chunk)
        return b"".join(chunks)

    async def iter_bounded_lines(self, response: httpx.Response) -> AsyncIterator[str]:
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
                yield _decode_line(line)
        if buffer:
            yield _decode_line(bytes(buffer).removesuffix(b"\r"))

    async def json_response(self, response: httpx.Response, secret: str) -> Any:
        content = await self.read_bounded(response)
        if response.status_code != 200:
            self.raise_response_error(response.status_code, content, secret)
        try:
            return json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelProtocolError("model provider returned invalid JSON") from exc

    @staticmethod
    def raise_response_error(status: int, content: bytes, secret: str) -> None:
        message = f"model provider rejected the request with HTTP {status}"
        try:
            body = json.loads(content[:65_536])
            detail = _error_detail(body)
            if detail:
                message = detail[:500]
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        message = clean_external_text(str(redact_value(message, {"model_api_key": secret})), 500)
        if status in {401, 403}:
            code, retryable = "MODEL_AUTH_FAILED", False
        elif status == 404:
            code, retryable = "MODEL_MODEL_UNAVAILABLE", False
        elif status == 429:
            code, retryable = "MODEL_PROVIDER_RATE_LIMITED", True
        elif 500 <= status < 600:
            code, retryable = "MODEL_PROVIDER_UNAVAILABLE", True
        else:
            code, retryable = "MODEL_PROVIDER_REJECTED", False
        raise ModelProviderError(code, message, retryable=retryable)


def raise_transport_error(exc: httpx.HTTPError) -> None:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        raise ModelProviderError(
            "MODEL_PROVIDER_UNAVAILABLE",
            "model provider connection failed",
            retryable=True,
        ) from exc
    if isinstance(exc, httpx.ReadTimeout):
        raise ModelProviderError(
            "MODEL_PROVIDER_TIMEOUT",
            "model provider response timed out",
            retryable=True,
        ) from exc
    raise ModelProviderError(
        "MODEL_PROVIDER_NETWORK_ERROR",
        "model provider request failed",
        retryable=True,
    ) from exc


def clean_external_text(value: str, maximum: int = 200) -> str:
    normalized = unicodedata.normalize("NFC", value)
    cleaned = "".join(
        character for character in normalized if not unicodedata.category(character).startswith("C")
    ).strip()
    return cleaned[:maximum]


def normalize_discovered_model_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = clean_external_text(value, 200)
    return cleaned or None


def parse_json_object(value: str, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ModelProtocolError(f"{label} contained invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ModelProtocolError(f"{label} must be an object")
    return payload


def normalize_token_count(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ModelProtocolError("model usage token counts must be non-negative integers")
    return value


def resolve_addresses(hostname: str, port: int) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item[4][0] for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        )
    )


def netloc(hostname: str, port: int, scheme: str) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    return host if port == default_port else f"{host}:{port}"


def _decode_line(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ModelProtocolError("model stream is not valid UTF-8") from exc


def _error_detail(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, dict):
        for key in ("message", "status"):
            if isinstance(error.get(key), str):
                return error[key]
    if isinstance(error, str):
        return error
    if isinstance(body.get("message"), str):
        return body["message"]
    return None
