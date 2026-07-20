from __future__ import annotations

import threading
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest

from nico_agent.tools import ToolExecutionContext
from nico_agent.tools.builtin import HttpReadExecutor, PinnedRequest, RawHttpResponse
from nico_agent.tools.errors import ToolExecutorFailure


class FakeTransport:
    def __init__(self, responses: Sequence[RawHttpResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[PinnedRequest] = []

    async def request(self, request: PinnedRequest) -> RawHttpResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def _context(config: dict | None = None) -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id=uuid4(),
        run_id=uuid4(),
        run_step_id=uuid4(),
        actor_id="http-test",
        correlation_id=uuid4(),
        tool_config=config or {"allowed_domains": ["example.com"]},
    )


def _response(*, status: int = 200, body: bytes = b'{"ok":true}', headers=()) -> RawHttpResponse:
    return RawHttpResponse(
        status=status,
        headers=(("Content-Type", "application/json; charset=utf-8"), *headers),
        body=body,
    )


@pytest.mark.asyncio
async def test_https_request_is_bound_to_validated_ip_and_filters_headers() -> None:
    transport = FakeTransport(
        [_response(headers=(("Set-Cookie", "secret=value"), ("ETag", '"abc"')))]
    )
    executor = HttpReadExecutor(
        transport=transport,
        resolver=lambda host, port: ["93.184.216.34"],
    )

    result = await executor.execute(
        _context(), {"url": "https://example.com/data?q=public", "method": "GET"}, {}
    )

    request = transport.requests[0]
    assert request.hostname == "example.com"
    assert request.ip_address == "93.184.216.34"
    assert request.target == "/data?q=public"
    assert result.output["content"] == '{"ok":true}'
    assert result.output["headers"] == {
        "content-type": "application/json; charset=utf-8",
        "etag": '"abc"',
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.1.2.3"],
        ["169.254.169.254"],
        ["0.0.0.0"],
        ["224.0.0.1"],
        ["2001:db8::1"],
        ["::ffff:127.0.0.1"],
        ["93.184.216.34", "192.168.1.10"],
    ],
)
async def test_private_reserved_mapped_and_mixed_dns_fail_closed(addresses) -> None:
    transport = FakeTransport([_response()])
    executor = HttpReadExecutor(transport=transport, resolver=lambda host, port: addresses)

    with pytest.raises(ToolExecutorFailure) as captured:
        await executor.execute(_context(), {"url": "https://example.com"}, {})

    assert captured.value.code == "HTTP_ADDRESS_DENIED"
    assert transport.requests == []


@pytest.mark.asyncio
async def test_redirect_is_reauthorized_reresolved_and_pinned() -> None:
    transport = FakeTransport(
        [
            _response(status=302, headers=(("Location", "https://api.example.com/final"),)),
            _response(body=b"done", headers=(("Content-Type", "text/plain"),)),
        ]
    )
    lookups: list[str] = []

    def resolve(hostname: str, port: int) -> list[str]:
        lookups.append(hostname)
        return ["93.184.216.34" if hostname == "example.com" else "142.250.72.14"]

    executor = HttpReadExecutor(transport=transport, resolver=resolve)
    context = _context({"allowed_domains": ["example.com", "*.example.com"]})

    result = await executor.execute(context, {"url": "https://example.com/start"}, {})

    assert lookups == ["example.com", "api.example.com"]
    assert [item.ip_address for item in transport.requests] == [
        "93.184.216.34",
        "142.250.72.14",
    ]
    assert result.output["redirects"] == 1
    assert result.output["url"] == "https://api.example.com/final"


@pytest.mark.asyncio
async def test_redirect_to_private_address_is_denied_before_second_request() -> None:
    transport = FakeTransport(
        [_response(status=302, headers=(("Location", "https://internal.example.com/"),))]
    )

    def resolve(hostname: str, port: int) -> list[str]:
        return ["93.184.216.34"] if hostname == "example.com" else ["10.0.0.8"]

    executor = HttpReadExecutor(transport=transport, resolver=resolve)
    context = _context({"allowed_domains": ["example.com", "*.example.com"]})

    with pytest.raises(ToolExecutorFailure) as captured:
        await executor.execute(context, {"url": "https://example.com/start"}, {})

    assert captured.value.code == "HTTP_ADDRESS_DENIED"
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_http_only_allows_explicit_loopback_test_mode() -> None:
    transport = FakeTransport([_response(body=b"ok", headers=(("Content-Type", "text/plain"),))])
    executor = HttpReadExecutor(
        transport=transport,
        resolver=lambda host, port: ["127.0.0.1"],
        allow_http_loopback=True,
    )
    context = _context(
        {
            "allowed_domains": ["localhost"],
            "allowed_ports": [8765],
            "allow_http_loopback": True,
        }
    )

    result = await executor.execute(context, {"url": "http://localhost:8765/health"}, {})

    assert result.output["content"] == "ok"
    assert transport.requests[0].ip_address == "127.0.0.1"


@pytest.mark.asyncio
async def test_agent_config_cannot_enable_platform_loopback_switch() -> None:
    transport = FakeTransport([_response()])
    executor = HttpReadExecutor(
        transport=transport,
        resolver=lambda host, port: ["127.0.0.1"],
        allow_http_loopback=False,
    )
    context = _context(
        {
            "allowed_domains": ["localhost"],
            "allow_http_loopback": True,
        }
    )

    with pytest.raises(ToolExecutorFailure) as captured:
        await executor.execute(context, {"url": "http://localhost/"}, {})

    assert captured.value.code == "HTTP_ADDRESS_DENIED"
    assert transport.requests == []


@pytest.mark.asyncio
async def test_socket_transport_connects_to_pinned_loopback_without_proxy(monkeypatch) -> None:
    observed: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed["host"] = self.headers["Host"]
            observed["encoding"] = self.headers["Accept-Encoding"]
            body = b'{"transport":"pinned"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    port = server.server_address[1]
    try:
        executor = HttpReadExecutor(
            resolver=lambda host, requested_port: ["127.0.0.1"],
            allow_http_loopback=True,
        )
        context = _context(
            {
                "allowed_domains": ["localhost"],
                "allowed_ports": [port],
                "allow_http_loopback": True,
            }
        )
        result = await executor.execute(context, {"url": f"http://localhost:{port}/document"}, {})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.output["content"] == '{"transport":"pinned"}'
    assert observed == {"host": f"localhost:{port}", "encoding": "identity"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("https://user:password@example.com", "HTTP_URL_INVALID"),
        ("https://example.com/#fragment", "HTTP_URL_INVALID"),
        ("http://example.com", "HTTP_SCHEME_DENIED"),
        ("https://not-example.com", "HTTP_DOMAIN_DENIED"),
        ("https://example.com:8443", "HTTP_PORT_DENIED"),
    ],
)
async def test_url_scheme_domain_and_port_policy(url: str, code: str) -> None:
    executor = HttpReadExecutor(
        transport=FakeTransport([_response()]),
        resolver=lambda host, port: ["93.184.216.34"],
    )

    with pytest.raises(ToolExecutorFailure) as captured:
        await executor.execute(_context(), {"url": url}, {})

    assert captured.value.code == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "config", "code"),
    [
        (_response(body=b"12345"), {"max_response_bytes": 4}, "HTTP_RESPONSE_TOO_LARGE"),
        (
            _response(headers=(("Content-Encoding", "gzip"),)),
            {},
            "HTTP_ENCODING_DENIED",
        ),
        (
            RawHttpResponse(200, (("Content-Type", "application/octet-stream"),), b"raw"),
            {},
            "HTTP_CONTENT_TYPE_DENIED",
        ),
        (
            RawHttpResponse(200, (("Content-Type", "text/plain"),), b"\xff"),
            {},
            "HTTP_ENCODING_INVALID",
        ),
    ],
)
async def test_response_size_compression_type_and_utf8_limits(response, config, code) -> None:
    transport = FakeTransport([response])
    executor = HttpReadExecutor(
        transport=transport,
        resolver=lambda host, port: ["93.184.216.34"],
        max_response_bytes=100,
    )
    context = _context({"allowed_domains": ["example.com"], **config})

    with pytest.raises(ToolExecutorFailure) as captured:
        await executor.execute(context, {"url": "https://example.com"}, {})

    assert captured.value.code == code


@pytest.mark.asyncio
async def test_redirect_limit_and_missing_location_are_stable_errors() -> None:
    redirect = _response(status=302, headers=(("Location", "/again"),))
    executor = HttpReadExecutor(
        transport=FakeTransport([redirect]),
        resolver=lambda host, port: ["93.184.216.34"],
        max_redirects=0,
    )
    with pytest.raises(ToolExecutorFailure) as limited:
        await executor.execute(_context(), {"url": "https://example.com"}, {})
    assert limited.value.code == "HTTP_REDIRECT_LIMIT"

    executor = HttpReadExecutor(
        transport=FakeTransport([_response(status=302)]),
        resolver=lambda host, port: ["93.184.216.34"],
    )
    with pytest.raises(ToolExecutorFailure) as invalid:
        await executor.execute(_context(), {"url": "https://example.com"}, {})
    assert invalid.value.code == "HTTP_REDIRECT_INVALID"
