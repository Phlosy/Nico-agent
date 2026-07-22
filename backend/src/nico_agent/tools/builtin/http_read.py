"""Pinned-address HTTP reader with fail-closed SSRF and response controls."""

from __future__ import annotations

import hashlib
import ipaddress
from typing import Any
from urllib.parse import urljoin

from nico_agent.net.safe_http import (
    HttpTransport,
    PinnedRequest,
    RawHttpResponse,
    ResolvedHttpTarget,
    SafeHttpError,
    SafeHttpPolicy,
    SocketHttpTransport,
    normalize_response_headers,
    resolve_addresses,
    resolve_http_target,
)
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
    implementation_hash = canonical_hash({"executor": "http.read", "revision": 2})

    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        resolver=None,
        max_response_bytes: int = 1_048_576,
        connect_timeout: float = 5.0,
        read_timeout: float = 10.0,
        max_redirects: int = 3,
        allow_http_loopback: bool = False,
        allowed_content_types: tuple[str, ...] = _DEFAULT_CONTENT_TYPES,
    ) -> None:
        self.transport = transport or SocketHttpTransport()
        self.resolver = resolver or resolve_addresses
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
        redirect_from: str | None = None
        while True:
            target = await self._validate_target(
                context,
                current_url,
                redirect_from=redirect_from,
            )
            request = PinnedRequest(
                method=method,
                scheme=target.scheme,
                hostname=target.hostname,
                port=target.port,
                target=target.path_and_query,
                ip_address=target.addresses[0],
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
            try:
                response = await self.transport.request(request)
            except SafeHttpError as exc:
                raise _tool_http_error(exc) from exc
            headers = normalize_response_headers(response.headers)
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
                redirect_from = target.canonical_url
                current_url = urljoin(target.canonical_url, location)
                redirects += 1
                continue
            return ToolExecutionResult(
                output=self._result(context, target, response, headers, redirects)
            )

    async def _validate_target(
        self,
        context: ToolExecutionContext,
        url: str,
        *,
        redirect_from: str | None,
    ) -> ResolvedHttpTarget:
        allow_loopback_http = self.allow_http_loopback and bool(
            context.tool_config.get("allow_http_loopback", False)
        )
        domains = context.tool_config.get("allowed_domains")
        configured_domains = (
            tuple(item for item in domains if isinstance(item, str))
            if isinstance(domains, list) and len(domains) <= 100
            else ()
        )
        policy = SafeHttpPolicy.allowlisted(
            configured_domains,
            allowed_ports=frozenset({80, 443, *_allowed_ports(context.tool_config)}),
            allow_http=True,
            allow_loopback=allow_loopback_http,
            allow_cross_origin_redirects=True,
        )
        try:
            target = await resolve_http_target(
                url,
                policy=policy,
                resolver=self.resolver,
                redirect_from=redirect_from,
            )
        except SafeHttpError as exc:
            raise _tool_http_error(exc) from exc
        if target.scheme == "http" and not all(
            ipaddress.ip_address(value).is_loopback for value in target.addresses
        ):
            raise ToolExecutorFailure("HTTP_SCHEME_DENIED", "HTTP requires HTTPS")
        return target

    def _result(
        self,
        context: ToolExecutionContext,
        target: ResolvedHttpTarget,
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
            "url": target.canonical_url,
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


def _tool_http_error(exc: SafeHttpError) -> ToolExecutorFailure:
    return ToolExecutorFailure(f"HTTP_{exc.code}", exc.message)
