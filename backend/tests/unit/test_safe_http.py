from __future__ import annotations

import pytest

from nico_agent.net.safe_http import (
    PinnedRequest,
    RawHttpResponse,
    SafeHttpClient,
    SafeHttpError,
    SafeHttpPolicy,
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
