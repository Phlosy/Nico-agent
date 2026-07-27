from __future__ import annotations

import pytest

import nico_agent.net.safe_http as safe_http
from nico_agent.net.safe_http import (
    PinnedRequest,
    RawHttpResponse,
    SafeHttpClient,
    SafeHttpError,
    SafeHttpPolicy,
    SocketHttpTransport,
    resolve_http_target,
)


class FakeTransport:
    def __init__(self, responses: list[RawHttpResponse]) -> None:
        self.responses = responses
        self.requests: list[PinnedRequest] = []

    async def request(self, request: PinnedRequest) -> RawHttpResponse:
        self.requests.append(request)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_public_target_is_canonicalized_idna_resolved_and_pinned() -> None:
    lookups: list[tuple[str, int]] = []

    def resolver(hostname: str, port: int) -> list[str]:
        lookups.append((hostname, port))
        return ["93.184.216.34", "93.184.216.34"]

    target = await resolve_http_target(
        "HTTPS://bücher.example:443/a/../report?q=one",
        policy=SafeHttpPolicy.strict_public(),
        resolver=resolver,
    )

    assert lookups == [("xn--bcher-kva.example", 443)]
    assert target.canonical_url == "https://xn--bcher-kva.example/a/../report?q=one"
    assert target.pinned_url == "https://93.184.216.34/a/../report?q=one"
    assert target.host_header == "xn--bcher-kva.example"
    assert target.addresses == ("93.184.216.34",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.com/",
        "https://example.com/#fragment",
        "https://example.com/\x00hidden",
    ],
)
async def test_userinfo_fragment_and_control_characters_are_rejected(url: str) -> None:
    with pytest.raises(SafeHttpError) as captured:
        await resolve_http_target(
            url,
            policy=SafeHttpPolicy.strict_public(),
            resolver=lambda host, port: ["93.184.216.34"],
        )

    assert captured.value.code == "URL_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addresses",
    [
        ["93.184.216.34", "10.0.0.8"],
        ["::ffff:127.0.0.1"],
        ["169.254.169.254"],
        ["2001:db8::1"],
    ],
)
async def test_strict_public_policy_rejects_any_non_public_dns_result(addresses) -> None:
    with pytest.raises(SafeHttpError) as captured:
        await resolve_http_target(
            "https://example.com/",
            policy=SafeHttpPolicy.strict_public(),
            resolver=lambda host, port: addresses,
        )

    assert captured.value.code == "ADDRESS_DENIED"


@pytest.mark.asyncio
async def test_empty_dns_and_non_default_ports_fail_closed() -> None:
    with pytest.raises(SafeHttpError) as empty:
        await resolve_http_target(
            "https://example.com/",
            policy=SafeHttpPolicy.strict_public(),
            resolver=lambda host, port: [],
        )
    assert empty.value.code == "DNS_ERROR"

    with pytest.raises(SafeHttpError) as port:
        await resolve_http_target(
            "https://example.com:8443/",
            policy=SafeHttpPolicy.strict_public(),
            resolver=lambda host, requested_port: ["93.184.216.34"],
        )
    assert port.value.code == "PORT_DENIED"


@pytest.mark.asyncio
async def test_exact_endpoint_can_explicitly_allow_loopback_http_only_for_that_origin() -> None:
    policy = SafeHttpPolicy.exact_endpoint(
        "http://localhost:8080",
        allow_loopback=True,
        allow_http=True,
    )
    target = await resolve_http_target(
        "http://localhost:8080/search?q=nico",
        policy=policy,
        resolver=lambda host, port: ["127.0.0.1"],
    )

    assert target.canonical_url == "http://localhost:8080/search?q=nico"
    assert target.pinned_url == "http://127.0.0.1:8080/search?q=nico"

    with pytest.raises(SafeHttpError) as other_origin:
        await resolve_http_target(
            "http://127.0.0.1:8080/search?q=nico",
            policy=policy,
            resolver=lambda host, port: ["127.0.0.1"],
        )
    assert other_origin.value.code == "DOMAIN_DENIED"


@pytest.mark.asyncio
async def test_cross_origin_redirect_requires_explicit_policy() -> None:
    def resolver(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    strict = SafeHttpPolicy.strict_public()

    with pytest.raises(SafeHttpError) as denied:
        await resolve_http_target(
            "https://cdn.example.net/final",
            policy=strict,
            resolver=resolver,
            redirect_from="https://example.com/start",
        )
    assert denied.value.code == "REDIRECT_DENIED"

    allowed = SafeHttpPolicy.strict_public(allow_cross_origin_redirects=True)
    target = await resolve_http_target(
        "https://cdn.example.net/final",
        policy=allowed,
        resolver=resolver,
        redirect_from="https://example.com/start",
    )
    assert target.canonical_url == "https://cdn.example.net/final"


@pytest.mark.asyncio
async def test_safe_client_encodes_params_and_passes_only_pinned_request_data() -> None:
    transport = FakeTransport(
        [RawHttpResponse(200, (("Content-Type", "application/json"),), b"{}")]
    )
    client = SafeHttpClient(
        transport=transport,
        resolver=lambda host, port: ["93.184.216.34"],
    )

    response = await client.request(
        "GET",
        "https://search.example/api",
        policy=SafeHttpPolicy.exact_endpoint("https://search.example/api"),
        headers={"Authorization": "secret"},
        params={"q": "nico search", "format": "json"},
        max_response_bytes=100,
    )

    assert response.body == b"{}"
    assert transport.requests == [
        PinnedRequest(
            method="GET",
            scheme="https",
            hostname="search.example",
            port=443,
            target="/api?q=nico+search&format=json",
            ip_address="93.184.216.34",
            connect_timeout=5,
            read_timeout=10,
            max_response_bytes=100,
            headers=(("Authorization", "secret"),),
        )
    ]


@pytest.mark.asyncio
async def test_safe_client_applies_per_request_socket_timeouts() -> None:
    transport = FakeTransport([RawHttpResponse(200, (), b"ok")])
    client = SafeHttpClient(
        transport=transport,
        resolver=lambda host, port: ["93.184.216.34"],
        connect_timeout=5,
        read_timeout=10,
    )

    await client.request(
        "GET",
        "https://provider.example/tool",
        policy=SafeHttpPolicy.exact_endpoint("https://provider.example"),
        connect_timeout=0.25,
        read_timeout=0.5,
    )

    assert transport.requests[0].connect_timeout == 0.25
    assert transport.requests[0].read_timeout == 0.5


@pytest.mark.asyncio
async def test_safe_client_reauthorizes_redirect_before_second_request() -> None:
    transport = FakeTransport(
        [
            RawHttpResponse(
                302,
                (("Location", "https://other.example/final"),),
                b"",
            )
        ]
    )
    client = SafeHttpClient(
        transport=transport,
        resolver=lambda host, port: ["93.184.216.34"],
    )

    with pytest.raises(SafeHttpError) as captured:
        await client.request(
            "GET",
            "https://search.example/start",
            policy=SafeHttpPolicy.strict_public(),
            max_redirects=1,
        )

    assert captured.value.code == "REDIRECT_DENIED"
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_safe_client_calls_external_authorizer_before_each_redirect_hop() -> None:
    transport = FakeTransport(
        [
            RawHttpResponse(302, (("Location", "https://cdn.example/final"),), b""),
            RawHttpResponse(200, (("Content-Type", "text/plain"),), b"done"),
        ]
    )
    client = SafeHttpClient(
        transport=transport,
        resolver=lambda host, port: ["93.184.216.34"],
    )
    authorized = []

    async def authorize(url, redirect_from):
        authorized.append((url, redirect_from))

    result = await client.request_with_metadata(
        "GET",
        "https://docs.example/start",
        policy=SafeHttpPolicy.strict_public(allow_cross_origin_redirects=True),
        max_redirects=1,
        authorize=authorize,
    )

    assert authorized == [
        ("https://docs.example/start", None),
        ("https://cdn.example/final", "https://docs.example/start"),
    ]
    assert result.response.body == b"done"
    assert result.target.canonical_url == "https://cdn.example/final"
    assert result.redirects == 1
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_safe_client_reports_private_redirect_target_as_redirect_denied() -> None:
    transport = FakeTransport(
        [RawHttpResponse(302, (("Location", "https://private.example/final"),), b"")]
    )
    client = SafeHttpClient(
        transport=transport,
        resolver=lambda host, port: ["10.0.0.8" if host == "private.example" else "93.184.216.34"],
    )

    with pytest.raises(SafeHttpError) as captured:
        await client.request(
            "GET",
            "https://docs.example/start",
            policy=SafeHttpPolicy.strict_public(allow_cross_origin_redirects=True),
            max_redirects=1,
        )

    assert captured.value.code == "REDIRECT_DENIED"
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_safe_client_passes_bounded_post_bytes_to_pinned_transport() -> None:
    transport = FakeTransport(
        [RawHttpResponse(200, (("Content-Type", "application/json"),), b'{"ok":true}')]
    )
    client = SafeHttpClient(
        transport=transport,
        resolver=lambda host, port: ["93.184.216.34"],
    )
    body = b'{"arguments":{"query":"nico"}}'

    response = await client.request(
        "POST",
        "https://tools.example/v1/execute",
        policy=SafeHttpPolicy.exact_endpoint("https://tools.example"),
        headers={
            "Authorization": "Bearer must-not-appear-in-repr",
            "Content-Type": "application/json",
        },
        body=body,
        max_request_bytes=len(body),
        max_response_bytes=100,
    )

    request = transport.requests[0]
    assert response.body == b'{"ok":true}'
    assert request.method == "POST"
    assert request.target == "/v1/execute"
    assert request.body == body
    assert request.max_request_bytes == len(body)
    assert request.headers == (
        ("Authorization", "Bearer must-not-appear-in-repr"),
        ("Content-Type", "application/json"),
    )
    assert "must-not-appear-in-repr" not in repr(request)
    assert body.decode() not in repr(request)


@pytest.mark.asyncio
async def test_safe_client_rejects_unsupported_methods_bodies_and_oversized_posts() -> None:
    client = SafeHttpClient(
        transport=FakeTransport([]),
        resolver=lambda host, port: ["93.184.216.34"],
    )
    policy = SafeHttpPolicy.strict_public()

    with pytest.raises(SafeHttpError) as method:
        await client.request("PUT", "https://tools.example/", policy=policy)
    assert method.value.code == "METHOD_DENIED"

    with pytest.raises(SafeHttpError) as get_body:
        await client.request("GET", "https://tools.example/", policy=policy, body=b"not allowed")
    assert get_body.value.code == "BODY_DENIED"

    with pytest.raises(SafeHttpError) as oversized:
        await client.request(
            "POST",
            "https://tools.example/",
            policy=policy,
            body=b"too large",
            max_request_bytes=3,
        )
    assert oversized.value.code == "REQUEST_TOO_LARGE"


@pytest.mark.asyncio
async def test_post_redirect_is_reresolved_and_private_target_is_denied() -> None:
    transport = FakeTransport(
        [RawHttpResponse(307, (("Location", "https://private.example/execute"),), b"")]
    )
    lookups: list[str] = []

    def resolver(host: str, port: int) -> list[str]:
        lookups.append(host)
        if host == "private.example":
            return ["10.0.0.8"]
        return ["93.184.216.34"]

    client = SafeHttpClient(transport=transport, resolver=resolver)

    with pytest.raises(SafeHttpError) as captured:
        await client.request(
            "POST",
            "https://tools.example/execute",
            policy=SafeHttpPolicy.strict_public(allow_cross_origin_redirects=True),
            body=b"{}",
            max_redirects=1,
        )

    assert captured.value.code == "REDIRECT_DENIED"
    assert lookups == ["tools.example", "private.example"]
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_post_dns_policy_rejects_mixed_public_and_private_addresses() -> None:
    transport = FakeTransport([])
    client = SafeHttpClient(
        transport=transport,
        resolver=lambda host, port: ["93.184.216.34", "10.0.0.8"],
    )

    with pytest.raises(SafeHttpError) as captured:
        await client.request(
            "POST",
            "https://tools.example/execute",
            policy=SafeHttpPolicy.strict_public(),
            body=b"{}",
        )

    assert captured.value.code == "ADDRESS_DENIED"
    assert transport.requests == []


def test_socket_transport_sends_exact_post_length_type_and_body(monkeypatch) -> None:
    class FakeSocket:
        def settimeout(self, timeout: float) -> None:
            self.timeout = timeout

        def close(self) -> None:
            self.closed = True

    class FakeResponse:
        status = 200

        def getheaders(self):
            return (("Content-Type", "application/json"),)

        def read(self, amount: int) -> bytes:
            return b"{}"

    class FakeConnection:
        instance = None

        def __init__(self, hostname: str, port: int) -> None:
            self.hostname = hostname
            self.port = port
            self.headers: list[tuple[str, str]] = []
            FakeConnection.instance = self

        def putrequest(self, method: str, target: str, **kwargs) -> None:
            self.method = method
            self.target = target

        def putheader(self, name: str, value: str) -> None:
            self.headers.append((name, value))

        def endheaders(self, body: bytes | None = None) -> None:
            self.body = body

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

    fake_socket = FakeSocket()
    monkeypatch.setattr(
        safe_http.socket,
        "create_connection",
        lambda target, timeout: fake_socket,
    )
    monkeypatch.setattr(safe_http.http.client, "HTTPConnection", FakeConnection)
    body = b'{"tool":"echo"}'

    response = SocketHttpTransport._request(
        PinnedRequest(
            method="POST",
            scheme="http",
            hostname="tools.example",
            port=80,
            target="/v1/execute",
            ip_address="93.184.216.34",
            connect_timeout=1,
            read_timeout=2,
            max_response_bytes=100,
            max_request_bytes=100,
            headers=(
                ("Authorization", "Bearer secret"),
                ("Content-Type", "application/json"),
                ("Content-Length", "999"),
            ),
            body=body,
        )
    )

    connection = FakeConnection.instance
    assert response.body == b"{}"
    assert connection.method == "POST"
    assert connection.target == "/v1/execute"
    assert connection.body == body
    assert ("Content-Length", str(len(body))) in connection.headers
    assert ("Content-Type", "application/json") in connection.headers
    assert ("Content-Length", "999") not in connection.headers
    assert ("Authorization", "Bearer secret") in connection.headers


def test_socket_transport_reports_socket_deadline_as_timeout(monkeypatch) -> None:
    def time_out(_target, *, timeout):
        del timeout
        raise TimeoutError

    monkeypatch.setattr(safe_http.socket, "create_connection", time_out)

    with pytest.raises(SafeHttpError) as captured:
        SocketHttpTransport._request(
            PinnedRequest(
                method="GET",
                scheme="https",
                hostname="provider.example",
                port=443,
                target="/v1/health",
                ip_address="93.184.216.34",
                connect_timeout=0.1,
                read_timeout=0.1,
                max_response_bytes=100,
            )
        )

    assert captured.value.code == "TIMEOUT"
