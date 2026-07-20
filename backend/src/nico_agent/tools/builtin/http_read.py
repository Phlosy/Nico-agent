"""Pinned-address HTTP reader with fail-closed SSRF and response controls."""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import ipaddress
import socket
import ssl
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

from nico_agent.tools.contracts import (
    ToolDefinitionSpec,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolIsolation,
    ToolRetryPolicy,
    ToolRisk,
    canonical_hash,
)
from nico_agent.tools.errors import ToolExecutorFailure

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_SAFE_RESPONSE_HEADERS = frozenset({"content-type", "etag", "last-modified", "cache-control"})
_DEFAULT_CONTENT_TYPES = (
    "text/",
    "application/json",
    "application/problem+json",
    "application/xml",
)


@dataclass(frozen=True)
class PinnedRequest:
    method: str
    scheme: str
    hostname: str
    port: int
    target: str
    ip_address: str
    connect_timeout: float
    read_timeout: float
    max_response_bytes: int


@dataclass(frozen=True)
class RawHttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class HttpTransport(Protocol):
    async def request(self, request: PinnedRequest) -> RawHttpResponse: ...


Resolver = Callable[[str, int], Sequence[str]]


class SocketHttpTransport:
    """Connect directly to the validated IP while preserving HTTP Host and TLS SNI."""

    async def request(self, request: PinnedRequest) -> RawHttpResponse:
        return await asyncio.to_thread(self._request, request)

    @staticmethod
    def _request(request: PinnedRequest) -> RawHttpResponse:
        try:
            raw_socket = socket.create_connection(
                (request.ip_address, request.port), timeout=request.connect_timeout
            )
            try:
                raw_socket.settimeout(request.read_timeout)
                if request.scheme == "https":
                    raw_socket = ssl.create_default_context().wrap_socket(
                        raw_socket, server_hostname=request.hostname
                    )
                connection = http.client.HTTPConnection(request.hostname, request.port)
                connection.sock = raw_socket
                connection.putrequest(
                    request.method,
                    request.target,
                    skip_host=True,
                    skip_accept_encoding=True,
                )
                host = request.hostname
                if ":" in host:
                    host = f"[{host}]"
                default_port = 443 if request.scheme == "https" else 80
                if request.port != default_port:
                    host = f"{host}:{request.port}"
                connection.putheader("Host", host)
                connection.putheader("Accept", "text/*, application/json, application/xml")
                connection.putheader("Accept-Encoding", "identity")
                connection.putheader("User-Agent", "Nico-Agent-HTTP-Reader/1.0")
                connection.putheader("Connection", "close")
                connection.endheaders()
                response = connection.getresponse()
                body = (
                    b""
                    if request.method == "HEAD"
                    else response.read(request.max_response_bytes + 1)
                )
                return RawHttpResponse(
                    status=response.status,
                    headers=tuple(response.getheaders()),
                    body=body,
                )
            finally:
                raw_socket.close()
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise ToolExecutorFailure("HTTP_NETWORK_ERROR", "HTTP request failed") from exc


class HttpReadExecutor:
    spec = ToolDefinitionSpec(
        name="http.read",
        version="1.0.0",
        description="Read a public allow-listed HTTPS text or JSON resource",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "minLength": 1, "maxLength": 4096},
                "method": {"enum": ["GET", "HEAD"], "default": "GET"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "status": {"type": "integer", "minimum": 100, "maximum": 599},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                "content": {"type": "string"},
                "bytes": {"type": "integer", "minimum": 0},
                "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                "redirects": {"type": "integer", "minimum": 0},
            },
            "required": ["url", "status", "headers", "content", "bytes", "sha256", "redirects"],
            "additionalProperties": False,
        },
        permission="network.http.read",
        timeout_seconds=30,
        retry_policy=ToolRetryPolicy(
            max_attempts=2,
            backoff_seconds=0.2,
            retryable_codes=frozenset({"HTTP_NETWORK_ERROR"}),
        ),
        isolation=ToolIsolation.NETWORK,
        risk=ToolRisk.MEDIUM,
        max_output_bytes=1_100_000,
    )
    implementation_hash = canonical_hash({"executor": "http.read", "revision": 1})

    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        resolver: Resolver | None = None,
        max_response_bytes: int = 1_048_576,
        connect_timeout: float = 5.0,
        read_timeout: float = 10.0,
        max_redirects: int = 3,
        allow_http_loopback: bool = False,
        allowed_content_types: tuple[str, ...] = _DEFAULT_CONTENT_TYPES,
    ) -> None:
        self.transport = transport or SocketHttpTransport()
        self.resolver = resolver or _resolve_addresses
        self.max_response_bytes = max_response_bytes
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_redirects = max_redirects
        self.allow_http_loopback = allow_http_loopback
        self.allowed_content_types = allowed_content_types

    async def execute(self, context, arguments, secrets):
        method = arguments.get("method", "GET")
        if method not in {"GET", "HEAD"}:
            raise ToolExecutorFailure("HTTP_METHOD_DENIED", "HTTP method is not allowed")
        current_url = arguments["url"]
        redirects = 0
        while True:
            parsed, hostname, port, addresses = await self._validate_target(context, current_url)
            request = PinnedRequest(
                method=method,
                scheme=parsed.scheme,
                hostname=hostname,
                port=port,
                target=urlunsplit(("", "", parsed.path or "/", parsed.query, "")),
                ip_address=addresses[0],
                connect_timeout=self._limit_float(
                    context.tool_config.get("connect_timeout_seconds"), self.connect_timeout
                ),
                read_timeout=self._limit_float(
                    context.tool_config.get("read_timeout_seconds"), self.read_timeout
                ),
                max_response_bytes=self._limit_int(
                    context.tool_config.get("max_response_bytes"), self.max_response_bytes
                ),
            )
            response = await self.transport.request(request)
            headers = _headers(response.headers)
            if response.status in _REDIRECT_STATUSES:
                location = headers.get("location")
                if not location:
                    raise ToolExecutorFailure(
                        "HTTP_REDIRECT_INVALID", "HTTP redirect did not include a location"
                    )
                allowed_redirects = self._limit_int(
                    context.tool_config.get("max_redirects"), self.max_redirects, allow_zero=True
                )
                if redirects >= allowed_redirects:
                    raise ToolExecutorFailure(
                        "HTTP_REDIRECT_LIMIT", "HTTP redirect limit was exceeded"
                    )
                current_url = urljoin(_safe_url(parsed), location)
                redirects += 1
                continue
            return ToolExecutionResult(
                output=self._result(context, parsed, response, headers, redirects)
            )

    async def _validate_target(
        self, context: ToolExecutionContext, url: str
    ) -> tuple[SplitResult, str, int, tuple[str, ...]]:
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise ToolExecutorFailure("HTTP_URL_INVALID", "HTTP URL is invalid") from exc
        if (
            not hostname
            or any(ord(character) < 32 or ord(character) == 127 for character in url)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.scheme not in {"http", "https"}
        ):
            raise ToolExecutorFailure("HTTP_URL_INVALID", "HTTP URL is invalid")
        try:
            hostname = hostname.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ToolExecutorFailure("HTTP_URL_INVALID", "HTTP hostname is invalid") from exc
        if (
            not hostname
            or len(hostname) > 253
            or not _domain_allowed(hostname, context.tool_config.get("allowed_domains"))
        ):
            raise ToolExecutorFailure("HTTP_DOMAIN_DENIED", "HTTP hostname is not allowed")
        port = port or (443 if parsed.scheme == "https" else 80)
        if port not in {80, 443} and port not in _allowed_ports(context.tool_config):
            raise ToolExecutorFailure("HTTP_PORT_DENIED", "HTTP port is not allowed")
        try:
            resolved = tuple(dict.fromkeys(await asyncio.to_thread(self.resolver, hostname, port)))
        except OSError as exc:
            raise ToolExecutorFailure("HTTP_DNS_ERROR", "HTTP hostname resolution failed") from exc
        if not resolved:
            raise ToolExecutorFailure(
                "HTTP_DNS_ERROR", "HTTP hostname resolution returned no address"
            )
        allow_loopback_http = self.allow_http_loopback and bool(
            context.tool_config.get("allow_http_loopback", False)
        )
        addresses = tuple(
            _validate_address(value, allow_loopback=allow_loopback_http and parsed.scheme == "http")
            for value in resolved
        )
        if parsed.scheme != "https" and not (
            allow_loopback_http
            and all(ipaddress.ip_address(value).is_loopback for value in addresses)
        ):
            raise ToolExecutorFailure("HTTP_SCHEME_DENIED", "HTTP requires HTTPS")
        normalized = parsed._replace(
            scheme=parsed.scheme.lower(), netloc=_netloc(hostname, port, parsed.scheme)
        )
        return normalized, hostname, port, addresses

    def _result(
        self,
        context: ToolExecutionContext,
        parsed: SplitResult,
        response: RawHttpResponse,
        headers: dict[str, str],
        redirects: int,
    ) -> dict[str, Any]:
        max_bytes = self._limit_int(
            context.tool_config.get("max_response_bytes"), self.max_response_bytes
        )
        if len(response.body) > max_bytes:
            raise ToolExecutorFailure(
                "HTTP_RESPONSE_TOO_LARGE", "HTTP response exceeded its byte limit"
            )
        encoding = headers.get("content-encoding", "identity").strip().lower()
        if encoding not in {"", "identity"}:
            raise ToolExecutorFailure(
                "HTTP_ENCODING_DENIED", "Compressed HTTP responses are not accepted"
            )
        media_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        configured_types = context.tool_config.get("allowed_content_types")
        if not _content_type_allowed(media_type, self.allowed_content_types) or (
            configured_types is not None and not _content_type_allowed(media_type, configured_types)
        ):
            raise ToolExecutorFailure(
                "HTTP_CONTENT_TYPE_DENIED", "HTTP content type is not allowed"
            )
        try:
            content = response.body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ToolExecutorFailure(
                "HTTP_ENCODING_INVALID", "HTTP response is not UTF-8"
            ) from exc
        return {
            "url": _safe_url(parsed),
            "status": response.status,
            "headers": {
                name: value for name, value in headers.items() if name in _SAFE_RESPONSE_HEADERS
            },
            "content": content,
            "bytes": len(response.body),
            "sha256": hashlib.sha256(response.body).hexdigest(),
            "redirects": redirects,
        }

    @staticmethod
    def _limit_int(value: Any, platform_limit: int, *, allow_zero: bool = False) -> int:
        minimum = 0 if allow_zero else 1
        if isinstance(value, int) and not isinstance(value, bool) and value >= minimum:
            return min(value, platform_limit)
        return platform_limit

    @staticmethod
    def _limit_float(value: Any, platform_limit: float) -> float:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return min(float(value), platform_limit)
        return platform_limit


def _resolve_addresses(hostname: str, port: int) -> Sequence[str]:
    return [item[4][0] for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)]


def _validate_address(value: str, *, allow_loopback: bool) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ToolExecutorFailure("HTTP_DNS_ERROR", "HTTP DNS returned an invalid address") from exc
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    denied = (
        address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or address.is_loopback
        or not address.is_global
    )
    if denied and not (allow_loopback and address.is_loopback):
        raise ToolExecutorFailure("HTTP_ADDRESS_DENIED", "HTTP target address is not public")
    return str(address)


def _domain_allowed(hostname: str, configured: Any) -> bool:
    if not isinstance(configured, list) or not configured or len(configured) > 100:
        return False
    for raw_pattern in configured:
        if not isinstance(raw_pattern, str):
            continue
        pattern = raw_pattern.rstrip(".").lower()
        if pattern.startswith("*."):
            suffix = pattern[2:]
            if suffix and hostname != suffix and hostname.endswith(f".{suffix}"):
                return True
        elif hostname == pattern:
            return True
    return False


def _allowed_ports(config: dict[str, Any]) -> set[int]:
    values = config.get("allowed_ports", [])
    if not isinstance(values, list) or len(values) > 20:
        return set()
    return {
        value
        for value in values
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535
    }


def _content_type_allowed(media_type: str, configured: Any) -> bool:
    if not media_type or not isinstance(configured, (list, tuple)) or len(configured) > 30:
        return False
    for raw in configured:
        if not isinstance(raw, str):
            continue
        pattern = raw.strip().lower()
        if pattern.endswith("/") and media_type.startswith(pattern):
            return True
        if media_type == pattern:
            return True
    return False


def _headers(values: tuple[tuple[str, str], ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in values:
        lowered = name.lower()
        if lowered not in result:
            result[lowered] = value.strip()
    return result


def _netloc(hostname: str, port: int, scheme: str) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    return host if port == default_port else f"{host}:{port}"


def _safe_url(parsed: SplitResult) -> str:
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
