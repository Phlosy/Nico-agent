from __future__ import annotations

import json

import pytest

from nico_agent.net.doh import DnsOverHttpsResolver
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


def _dns_response(
    *answers: tuple[int, str],
    status: int = 0,
    content_type: str = "application/dns-json",
) -> RawHttpResponse:
    return RawHttpResponse(
        status=200,
        headers=(("Content-Type", content_type),),
        body=json.dumps(
            {
                "Status": status,
                "Answer": [
                    {"name": "docs.example", "type": answer_type, "data": value}
                    for answer_type, value in answers
                ],
            }
        ).encode(),
    )


@pytest.mark.asyncio
async def test_doh_bootstraps_through_fake_ip_but_returns_real_target_addresses() -> None:
    transport = FakeTransport(
        [
            _dns_response((5, "edge.example."), (1, "93.184.216.34")),
            _dns_response((28, "2606:2800:220:1:248:1893:25c8:1946")),
        ]
    )
    bootstrap_http = SafeHttpClient(
        transport=transport,
        resolver=lambda host, port: ["198.18.0.66"],
    )
    resolver = DnsOverHttpsResolver(
        "https://cloudflare-dns.com/dns-query",
        http=bootstrap_http,
        allow_private_bootstrap=True,
    )

    addresses = await resolver("docs.example", 443)

    assert addresses == (
        "93.184.216.34",
        "2606:2800:220:1:248:1893:25c8:1946",
    )
    assert [request.ip_address for request in transport.requests] == [
        "198.18.0.66",
        "198.18.0.66",
    ]
    assert [request.target for request in transport.requests] == [
        "/dns-query?name=docs.example&type=A",
        "/dns-query?name=docs.example&type=AAAA",
    ]


@pytest.mark.asyncio
async def test_google_doh_application_json_content_type_is_accepted() -> None:
    transport = FakeTransport(
        [
            _dns_response(
                (1, "93.184.216.34"),
                content_type="application/json; charset=UTF-8",
            ),
            _dns_response(content_type="application/json; charset=UTF-8"),
        ]
    )
    resolver = DnsOverHttpsResolver(
        "https://dns.google/resolve",
        http=SafeHttpClient(
            transport=transport,
            resolver=lambda host, port: ["198.18.0.67"],
        ),
        allow_private_bootstrap=True,
    )

    assert await resolver("docs.example", 443) == ("93.184.216.34",)


@pytest.mark.asyncio
async def test_doh_results_still_pass_through_strict_public_ssrf_validation() -> None:
    async def mixed_scope_resolver(_hostname: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34", "10.0.0.8")

    with pytest.raises(SafeHttpError) as captured:
        await resolve_http_target(
            "https://docs.example/guide",
            policy=SafeHttpPolicy.strict_public(),
            resolver=mixed_scope_resolver,
        )

    assert captured.value.code == "ADDRESS_DENIED"


@pytest.mark.asyncio
async def test_doh_rejects_failed_or_addressless_dns_answers() -> None:
    transport = FakeTransport([_dns_response(status=2), _dns_response()])
    resolver = DnsOverHttpsResolver(
        "https://dns.google/resolve",
        http=SafeHttpClient(
            transport=transport,
            resolver=lambda host, port: ["198.18.0.67"],
        ),
        allow_private_bootstrap=True,
    )

    with pytest.raises(SafeHttpError) as captured:
        await resolver("missing.example", 443)

    assert captured.value.code == "DNS_ERROR"
