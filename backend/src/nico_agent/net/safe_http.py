"""DNS-pinned HTTP targets with explicit, fail-closed network policies."""

from __future__ import annotations

import asyncio
import http.client
import inspect
import ipaddress
import socket
import ssl
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


class SafeHttpError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SafeHttpMode(StrEnum):
    STRICT_PUBLIC = "strict_public"
    ALLOWLISTED = "allowlisted"
    EXACT_ENDPOINT = "exact_endpoint"


@dataclass(frozen=True, slots=True)
class SafeHttpPolicy:
    mode: SafeHttpMode
    allowed_domains: tuple[str, ...] = ()
    allowed_ports: frozenset[int] = frozenset({443})
    exact_origin: str | None = None
    allow_http: bool = False
    allow_loopback: bool = False
    allow_private: bool = False
    allow_cross_origin_redirects: bool = False

    @classmethod
    def strict_public(
        cls,
        *,
        allowed_ports: frozenset[int] = frozenset({443}),
        allow_cross_origin_redirects: bool = False,
    ) -> SafeHttpPolicy:
        return cls(
            mode=SafeHttpMode.STRICT_PUBLIC,
            allowed_ports=_validated_ports(allowed_ports),
            allow_cross_origin_redirects=allow_cross_origin_redirects,
        )

    @classmethod
    def allowlisted(
        cls,
        domains: Sequence[str],
        *,
        allowed_ports: frozenset[int] = frozenset({443}),
        allow_http: bool = False,
        allow_loopback: bool = False,
        allow_cross_origin_redirects: bool = False,
    ) -> SafeHttpPolicy:
        return cls(
            mode=SafeHttpMode.ALLOWLISTED,
            allowed_domains=tuple(domains),
            allowed_ports=_validated_ports(allowed_ports),
            allow_http=allow_http,
            allow_loopback=allow_loopback,
            allow_cross_origin_redirects=allow_cross_origin_redirects,
        )

    @classmethod
    def exact_endpoint(
        cls,
        endpoint: str,
        *,
        allow_http: bool = False,
        allow_loopback: bool = False,
        allow_private: bool = False,
    ) -> SafeHttpPolicy:
        parsed = canonicalize_http_url(endpoint)
        return cls(
            mode=SafeHttpMode.EXACT_ENDPOINT,
            allowed_ports=frozenset({parsed.port}),
            exact_origin=parsed.origin,
            allow_http=allow_http,
            allow_loopback=allow_loopback,
            allow_private=allow_private,
        )


@dataclass(frozen=True, slots=True)
class CanonicalHttpUrl:
    scheme: str
    hostname: str
    port: int
    path: str
    query: str

    @property
    def host_header(self) -> str:
        return _netloc(self.hostname, self.port, self.scheme)

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host_header}"

    @property
    def url(self) -> str:
        return urlunsplit((self.scheme, self.host_header, self.path, self.query, ""))


@dataclass(frozen=True, slots=True)
class ResolvedHttpTarget:
    canonical_url: str
    pinned_url: str
    origin: str
    scheme: str
    hostname: str
    port: int
    host_header: str
    sni_hostname: str
    path_and_query: str
    addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
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
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class RawHttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True, slots=True)
class SafeHttpResult:
    response: RawHttpResponse
    target: ResolvedHttpTarget
    redirects: int


class HttpTransport(Protocol):
    async def request(self, request: PinnedRequest) -> RawHttpResponse: ...


Resolver = Callable[[str, int], Sequence[str] | Awaitable[Sequence[str]]]
RequestAuthorizer = Callable[[str, str | None], Awaitable[None]]


class SocketHttpTransport:
    """Connect to the validated IP while preserving HTTP Host and TLS SNI."""

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
                connection.putheader(
                    "Host", _netloc(request.hostname, request.port, request.scheme)
                )
                supplied = {name.lower() for name, _ in request.headers}
                if "accept" not in supplied:
                    connection.putheader("Accept", "text/*, application/json, application/xml")
                connection.putheader("Accept-Encoding", "identity")
                if "user-agent" not in supplied:
                    connection.putheader("User-Agent", "Nico-Agent-Safe-HTTP/1.0")
                connection.putheader("Connection", "close")
                for name, value in request.headers:
                    if name.lower() in {"host", "accept-encoding", "connection", "content-length"}:
                        continue
                    connection.putheader(name, value)
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
            raise SafeHttpError("NETWORK_ERROR", "HTTP request failed") from exc


class SafeHttpClient:
    """Bounded request loop that reauthorizes and re-resolves every redirect hop."""

    _REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

    def __init__(
        self,
        *,
        transport: HttpTransport | None = None,
        resolver: Resolver | None = None,
        connect_timeout: float = 5,
        read_timeout: float = 10,
    ) -> None:
        self.transport = transport or SocketHttpTransport()
        self.resolver = resolver or resolve_addresses
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout

    async def request(
        self,
        method: str,
        url: str,
        *,
        policy: SafeHttpPolicy,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        max_response_bytes: int = 1_048_576,
        max_redirects: int = 0,
    ) -> RawHttpResponse:
        result = await self.request_with_metadata(
            method,
            url,
            policy=policy,
            headers=headers,
            params=params,
            max_response_bytes=max_response_bytes,
            max_redirects=max_redirects,
        )
        return result.response

    async def request_with_metadata(
        self,
        method: str,
        url: str,
        *,
        policy: SafeHttpPolicy,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        max_response_bytes: int = 1_048_576,
        max_redirects: int = 0,
        authorize: RequestAuthorizer | None = None,
    ) -> SafeHttpResult:
        if method not in {"GET", "HEAD"}:
            raise SafeHttpError("METHOD_DENIED", "HTTP method is not allowed")
        if not 1 <= max_response_bytes <= 10_485_760:
            raise ValueError("max_response_bytes is outside the platform range")
        if not 0 <= max_redirects <= 10:
            raise ValueError("max_redirects is outside the platform range")
        current_url = _with_query_params(url, params)
        redirect_from: str | None = None
        redirects = 0
        while True:
            if authorize is not None:
                await authorize(current_url, redirect_from)
            try:
                target = await resolve_http_target(
                    current_url,
                    policy=policy,
                    resolver=self.resolver,
                    redirect_from=redirect_from,
                )
            except SafeHttpError as exc:
                if redirect_from is not None and exc.code in {
                    "URL_INVALID",
                    "DOMAIN_DENIED",
                    "PORT_DENIED",
                    "SCHEME_DENIED",
                    "ADDRESS_DENIED",
                    "REDIRECT_DENIED",
                }:
                    raise SafeHttpError(
                        "REDIRECT_DENIED",
                        "HTTP redirect target is not allowed",
                    ) from exc
                raise
            response = await self.transport.request(
                PinnedRequest(
                    method=method,
                    scheme=target.scheme,
                    hostname=target.hostname,
                    port=target.port,
                    target=target.path_and_query,
                    ip_address=target.addresses[0],
                    connect_timeout=self.connect_timeout,
                    read_timeout=self.read_timeout,
                    max_response_bytes=max_response_bytes,
                    headers=tuple((headers or {}).items()),
                )
            )
            if len(response.body) > max_response_bytes:
                raise SafeHttpError("RESPONSE_TOO_LARGE", "HTTP response exceeded its byte limit")
            response_headers = normalize_response_headers(response.headers)
            encoding = response_headers.get("content-encoding", "identity").strip().lower()
            if encoding not in {"", "identity"}:
                raise SafeHttpError("ENCODING_DENIED", "compressed HTTP responses are not accepted")
            if response.status not in self._REDIRECT_STATUSES:
                return SafeHttpResult(response=response, target=target, redirects=redirects)
            location = response_headers.get("location")
            if not location:
                raise SafeHttpError("REDIRECT_INVALID", "HTTP redirect has no location")
            if redirects >= max_redirects:
                raise SafeHttpError("REDIRECT_LIMIT", "HTTP redirect limit was exceeded")
            redirect_from = target.canonical_url
            current_url = urljoin(target.canonical_url, location)
            redirects += 1


def canonicalize_http_url(value: str) -> CanonicalHttpUrl:
    if not isinstance(value, str) or not value or _contains_control(value):
        raise SafeHttpError("URL_INVALID", "HTTP URL is invalid")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        explicit_port = parsed.port
    except ValueError as exc:
        raise SafeHttpError("URL_INVALID", "HTTP URL is invalid") from exc
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.scheme.lower() not in {"http", "https"}
    ):
        raise SafeHttpError("URL_INVALID", "HTTP URL is invalid")
    try:
        hostname = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise SafeHttpError("URL_INVALID", "HTTP hostname is invalid") from exc
    if not hostname or len(hostname) > 253:
        raise SafeHttpError("URL_INVALID", "HTTP hostname is invalid")
    scheme = parsed.scheme.lower()
    port = explicit_port or (443 if scheme == "https" else 80)
    return CanonicalHttpUrl(
        scheme=scheme,
        hostname=hostname,
        port=port,
        path=parsed.path or "/",
        query=parsed.query,
    )


async def resolve_http_target(
    value: str,
    *,
    policy: SafeHttpPolicy,
    resolver: Resolver | None = None,
    redirect_from: str | None = None,
) -> ResolvedHttpTarget:
    parsed = canonicalize_http_url(value)
    _authorize_url(parsed, policy, redirect_from=redirect_from)
    resolver = resolver or resolve_addresses
    try:
        raw_addresses = await _call_resolver(resolver, parsed.hostname, parsed.port)
    except OSError as exc:
        raise SafeHttpError("DNS_ERROR", "HTTP hostname resolution failed") from exc
    if not raw_addresses:
        raise SafeHttpError("DNS_ERROR", "HTTP hostname resolution returned no address")
    addresses = tuple(
        dict.fromkeys(_validate_address(value, policy=policy) for value in raw_addresses)
    )
    address_scopes = {_address_scope(value) for value in addresses}
    if len(address_scopes) != 1:
        raise SafeHttpError("ADDRESS_DENIED", "HTTP DNS returned mixed address scopes")
    pinned_host = addresses[0]
    pinned_url = urlunsplit(
        (
            parsed.scheme,
            _netloc(pinned_host, parsed.port, parsed.scheme),
            parsed.path,
            parsed.query,
            "",
        )
    )
    path_and_query = urlunsplit(("", "", parsed.path, parsed.query, ""))
    return ResolvedHttpTarget(
        canonical_url=parsed.url,
        pinned_url=pinned_url,
        origin=parsed.origin,
        scheme=parsed.scheme,
        hostname=parsed.hostname,
        port=parsed.port,
        host_header=parsed.host_header,
        sni_hostname=parsed.hostname,
        path_and_query=path_and_query,
        addresses=addresses,
    )


def resolve_addresses(hostname: str, port: int) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item[4][0] for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        )
    )


async def _call_resolver(
    resolver: Resolver,
    hostname: str,
    port: int,
) -> Sequence[str]:
    call = type(resolver).__call__
    if inspect.iscoroutinefunction(resolver) or inspect.iscoroutinefunction(call):
        result = resolver(hostname, port)
        if not inspect.isawaitable(result):
            raise SafeHttpError("DNS_ERROR", "HTTP resolver returned an invalid result")
        return await result
    result = await asyncio.to_thread(resolver, hostname, port)
    if inspect.isawaitable(result):
        raise SafeHttpError("DNS_ERROR", "HTTP resolver returned an invalid result")
    return result


def _authorize_url(
    parsed: CanonicalHttpUrl,
    policy: SafeHttpPolicy,
    *,
    redirect_from: str | None,
) -> None:
    if policy.mode is SafeHttpMode.EXACT_ENDPOINT and parsed.origin != policy.exact_origin:
        raise SafeHttpError("DOMAIN_DENIED", "HTTP target is outside the configured endpoint")
    if policy.mode is SafeHttpMode.ALLOWLISTED and not domain_allowed(
        parsed.hostname, policy.allowed_domains
    ):
        raise SafeHttpError("DOMAIN_DENIED", "HTTP hostname is not allowed")
    if parsed.port not in policy.allowed_ports:
        raise SafeHttpError("PORT_DENIED", "HTTP port is not allowed")
    if parsed.scheme != "https" and not policy.allow_http:
        raise SafeHttpError("SCHEME_DENIED", "HTTP requires HTTPS")
    if redirect_from is not None:
        previous = canonicalize_http_url(redirect_from)
        if previous.origin != parsed.origin and not policy.allow_cross_origin_redirects:
            raise SafeHttpError("REDIRECT_DENIED", "cross-origin HTTP redirect is not allowed")


def _validate_address(value: str, *, policy: SafeHttpPolicy) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise SafeHttpError("DNS_ERROR", "HTTP DNS returned an invalid address") from exc
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback:
        if policy.allow_loopback:
            return str(address)
        raise SafeHttpError("ADDRESS_DENIED", "HTTP target address is not public")
    if (
        address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        raise SafeHttpError("ADDRESS_DENIED", "HTTP target address is not public")
    if address.is_private:
        if policy.allow_private:
            return str(address)
        raise SafeHttpError("ADDRESS_DENIED", "HTTP target address is not public")
    if not address.is_global:
        raise SafeHttpError("ADDRESS_DENIED", "HTTP target address is not public")
    return str(address)


def domain_allowed(hostname: str, configured: Sequence[str]) -> bool:
    if not configured or len(configured) > 100:
        return False
    for raw_pattern in configured:
        if not isinstance(raw_pattern, str):
            continue
        try:
            pattern = raw_pattern.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError:
            continue
        if pattern.startswith("*."):
            suffix = pattern[2:]
            if suffix and hostname != suffix and hostname.endswith(f".{suffix}"):
                return True
        elif hostname == pattern:
            return True
    return False


def _validated_ports(values: frozenset[int]) -> frozenset[int]:
    if (
        not values
        or len(values) > 20
        or any(
            not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65_535
            for value in values
        )
    ):
        raise ValueError("allowed ports must contain 1 to 20 valid TCP ports")
    return values


def _address_scope(value: str) -> str:
    address = ipaddress.ip_address(value)
    if address.is_loopback:
        return "loopback"
    if address.is_private:
        return "private"
    return "public"


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _with_query_params(url: str, params: Mapping[str, str] | None) -> str:
    if not params:
        return url
    parsed = urlsplit(url)
    query = [*parse_qsl(parsed.query, keep_blank_values=True), *params.items()]
    return urlunsplit(parsed._replace(query=urlencode(query)))


def normalize_response_headers(values: tuple[tuple[str, str], ...]) -> dict[str, str]:
    """Lowercase response header names and retain the first value received."""

    result: dict[str, str] = {}
    for name, value in values:
        result.setdefault(name.lower(), value.strip())
    return result


def _netloc(hostname: str, port: int, scheme: str) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    return host if port == default_port else f"{host}:{port}"
