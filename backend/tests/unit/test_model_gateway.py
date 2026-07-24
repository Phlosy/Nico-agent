from __future__ import annotations

import json

import httpx
import pytest

from nico_agent.models.contracts import (
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelToolDefinition,
)
from nico_agent.models.errors import (
    ModelCapabilityMismatch,
    ModelEndpointDenied,
    ModelProtocolError,
    ModelProviderError,
    ModelRateLimitUnavailable,
)
from nico_agent.models.gateway import ModelGateway
from nico_agent.models.providers.openai_compatible import OpenAICompatibleProvider
from nico_agent.models.registry import ModelProviderRegistry


class FakeSecrets:
    def resolve(self, name: str, reference: str) -> str:
        assert name == "model_api_key"
        assert reference == "env:NICO_MODEL_SECRET_TEST"
        return "canary-model-secret"


def _request(**changes) -> ModelRequest:
    values = {
        "model": "test-model",
        "messages": (ModelMessage(role="user", content="hello"),),
        "endpoint": {
            "id": "00000000-0000-0000-0000-000000000010",
            "protocol": "openai_compatible",
            "base_url": "https://models.example/v1",
            "credential_ref": "env:NICO_MODEL_SECRET_TEST",
            "allowed_models": ["test-model"],
            "capabilities": {
                "streaming": True,
                "native_tool_calling": True,
                "json_object": True,
            },
        },
    }
    values.update(changes)
    return ModelRequest(**values)


def _provider(handler) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        secret_resolver=FakeSecrets(),
        resolver=lambda host, port: ["93.184.216.34"],
    )


@pytest.mark.asyncio
async def test_gateway_normalizes_text_stream_usage_and_request_id() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "models.example"
        assert request.headers["connection"] == "close"
        assert request.extensions["sni_hostname"] == "models.example"
        assert request.headers["authorization"] == "Bearer canary-model-secret"
        body = request.content.decode()
        assert "canary-model-secret" not in body
        chunks = [
            {"id": "chatcmpl-1", "choices": [{"delta": {"content": "Hel"}}]},
            {"id": "chatcmpl-1", "choices": [{"delta": {"content": "lo"}}]},
            {
                "id": "chatcmpl-1",
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        ]
        payload = "".join(f"data: {json.dumps(item)}\n\n" for item in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "x-request-id": "req-1"},
            content=(payload + "data: [DONE]\n\n").encode(),
        )

    gateway = ModelGateway(ModelProviderRegistry([_provider(handler)]))
    events = [event async for event in gateway.stream(_request())]

    assert [event.type for event in events].count(ModelStreamEventType.TEXT_DELTA) == 2
    assert "".join(event.text_delta or "" for event in events) == "Hello"
    assert events[-1].type is ModelStreamEventType.RESPONSE_COMPLETED
    assert events[-1].usage is not None
    assert events[-1].usage.total_tokens == 5
    assert events[-1].provider_request_id == "req-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability", "response_format"),
    [
        ("json_object", {"type": "json_object"}),
        (
            "json_schema",
            {
                "type": "json_schema",
                "json_schema": {
                    "name": "answer",
                    "strict": True,
                    "schema": {"type": "object"},
                },
            },
        ),
    ],
)
async def test_gateway_requires_the_exact_json_response_capability(
    capability: str,
    response_format: dict,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["response_format"] == response_format
        return httpx.Response(
            200,
            content=(
                b'data: {"choices":[{"delta":{"content":"{}"},'
                b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            ),
        )

    endpoint = {
        **_request().endpoint,
        "capabilities": {
            "streaming": True,
            "native_tool_calling": True,
            capability: True,
        },
    }
    response = await ModelGateway(ModelProviderRegistry([_provider(handler)])).complete(
        _request(endpoint=endpoint, response_format=response_format)
    )

    assert response.text == "{}"


@pytest.mark.asyncio
async def test_gateway_does_not_treat_json_object_as_json_schema() -> None:
    endpoint = {
        **_request().endpoint,
        "capabilities": {
            "streaming": True,
            "native_tool_calling": True,
            "json_object": True,
        },
    }
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "answer",
            "strict": True,
            "schema": {"type": "object"},
        },
    }

    with pytest.raises(ModelCapabilityMismatch) as captured:
        await ModelGateway(
            ModelProviderRegistry([_provider(lambda request: httpx.Response(500))])
        ).complete(_request(endpoint=endpoint, response_format=response_format))

    assert "json_schema" in captured.value.message


@pytest.mark.asyncio
async def test_gateway_assembles_fragmented_tool_call_arguments() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": '{"q":'},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": '"nico"}'}}]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
        payload = "".join(f"data: {json.dumps(item)}\n\n" for item in chunks)
        return httpx.Response(200, content=(payload + "data: [DONE]\n\n").encode())

    request = _request(
        tools=(
            ModelToolDefinition(
                name="lookup",
                description="lookup",
                input_schema={"type": "object"},
            ),
        )
    )
    response = await ModelGateway(ModelProviderRegistry([_provider(handler)])).complete(request)

    assert response.finish_reason == "tool_calls"
    assert response.tool_calls[0].id == "call-1"
    assert response.tool_calls[0].arguments == {"q": "nico"}


@pytest.mark.asyncio
async def test_gateway_retries_429_before_stream_starts() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(
            200,
            content=(
                b'data: {"choices":[{"delta":{"content":"ok"},'
                b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            ),
        )

    gateway = ModelGateway(
        ModelProviderRegistry([_provider(handler)]), max_attempts=2, retry_base_seconds=0
    )
    response = await gateway.complete(_request())

    assert attempts == 2
    assert response.text == "ok"


@pytest.mark.asyncio
async def test_provider_redacts_secret_from_error_body() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "bad token canary-model-secret"}},
        )

    gateway = ModelGateway(ModelProviderRegistry([_provider(handler)]), max_attempts=1)
    with pytest.raises(ModelProviderError) as captured:
        await gateway.complete(_request())

    assert captured.value.code == "MODEL_PROVIDER_REJECTED"
    assert "canary-model-secret" not in str(captured.value)
    assert "[REDACTED]" in captured.value.message


@pytest.mark.asyncio
async def test_provider_rejects_private_or_plain_http_endpoint() -> None:
    provider = _provider(lambda request: httpx.Response(500))
    gateway = ModelGateway(ModelProviderRegistry([provider]))

    with pytest.raises(ModelEndpointDenied):
        await gateway.complete(
            _request(
                endpoint={
                    **_request().endpoint,
                    "base_url": "http://127.0.0.1:8000/v1",
                }
            )
        )


@pytest.mark.asyncio
async def test_deployment_allowlist_can_enable_hermetic_private_model() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "172.20.0.10"
        assert request.headers["host"] == "fake-model:8100"
        return httpx.Response(
            200,
            content=(
                b'data: {"choices":[{"delta":{"content":"ok"},'
                b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            ),
        )

    provider = OpenAICompatibleProvider(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        secret_resolver=FakeSecrets(),
        resolver=lambda host, port: ["172.20.0.10"],
        trusted_private_hosts=("fake-model",),
        allow_http_trusted_hosts=True,
    )
    gateway = ModelGateway(ModelProviderRegistry([provider]))
    endpoint = {
        **_request().endpoint,
        "base_url": "http://fake-model:8100/v1",
    }

    response = await gateway.complete(_request(endpoint=endpoint))

    assert response.text == "ok"


@pytest.mark.asyncio
async def test_hard_rate_limit_fails_closed_when_limiter_is_unavailable() -> None:
    endpoint = {
        **_request().endpoint,
        "rate_limit": {"requests_per_minute": 10, "mode": "hard"},
    }
    gateway = ModelGateway(ModelProviderRegistry([_provider(lambda request: httpx.Response(500))]))

    with pytest.raises(ModelRateLimitUnavailable):
        await gateway.complete(_request(endpoint=endpoint))


@pytest.mark.asyncio
async def test_soft_rate_limit_can_degrade_when_limiter_is_unavailable() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                b'data: {"choices":[{"delta":{"content":"ok"},'
                b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            ),
        )

    endpoint = {
        **_request().endpoint,
        "rate_limit": {"requests_per_minute": 10, "mode": "soft"},
    }
    gateway = ModelGateway(ModelProviderRegistry([_provider(handler)]))

    response = await gateway.complete(_request(endpoint=endpoint))

    assert response.text == "ok"


@pytest.mark.asyncio
async def test_gateway_retries_disconnect_after_start_but_before_content() -> None:
    class DisconnectBeforeContent:
        name = "openai_compatible"

        def __init__(self) -> None:
            self.attempts = 0

        def describe_capabilities(self):
            return frozenset({ModelCapability.STREAMING})

        async def stream(self, request):
            self.attempts += 1
            yield ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED)
            if self.attempts == 1:
                raise ModelProviderError(
                    "MODEL_PROVIDER_NETWORK_ERROR",
                    "model provider request failed",
                    retryable=True,
                )
            yield ModelStreamEvent(
                type=ModelStreamEventType.TEXT_DELTA,
                text_delta="ok",
            )

    provider = DisconnectBeforeContent()
    gateway = ModelGateway(
        ModelProviderRegistry([provider]),
        max_attempts=2,
        retry_base_seconds=0,
    )

    response = await gateway.complete(_request())

    assert provider.attempts == 2
    assert response.text == "ok"


@pytest.mark.asyncio
async def test_provider_rejects_invalid_usage_with_a_stable_protocol_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                b'data: {"choices":[],"usage":{"prompt_tokens":true,'
                b'"completion_tokens":-1,"total_tokens":0}}\n\ndata: [DONE]\n\n'
            ),
        )

    gateway = ModelGateway(ModelProviderRegistry([_provider(handler)]), max_attempts=1)

    with pytest.raises(ModelProtocolError) as captured:
        await gateway.complete(_request())

    assert captured.value.code == "MODEL_PROTOCOL_ERROR"
