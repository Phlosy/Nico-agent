from __future__ import annotations

import json
import re

import httpx
import pytest

from nico_agent.models import (
    ModelDiscoveryRequest,
    ModelGateway,
    ModelMessage,
    ModelRequest,
    ModelStreamEventType,
    ModelToolDefinition,
)
from nico_agent.models.providers import (
    AnthropicMessagesProvider,
    GoogleGeminiProvider,
    OpenAICompatibleProvider,
)
from nico_agent.models.registry import ModelProviderRegistry
from nico_agent.runtime.actions import FinalAction
from nico_agent.runtime.native.action_parser import parse_agent_actions


class FakeSecrets:
    def resolve(self, name: str, reference: str) -> str:
        assert name == "model_api_key"
        assert reference == "env:NICO_MODEL_SECRET_TEST"
        return "canary-provider-key"


def _endpoint(protocol: str, base_url: str) -> dict:
    return {
        "id": "00000000-0000-0000-0000-000000000010",
        "protocol": protocol,
        "base_url": base_url,
        "credential_ref": "env:NICO_MODEL_SECRET_TEST",
        "allowed_models": [],
        "capabilities": {"streaming": True, "tools": True},
    }


def _request(protocol: str, base_url: str, model: str) -> ModelRequest:
    return ModelRequest(
        model=model,
        messages=(
            ModelMessage(role="system", content="Be concise"),
            ModelMessage(role="user", content="Hello"),
        ),
        endpoint=_endpoint(protocol, base_url),
        max_output_tokens=16,
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _tool_request(protocol: str, base_url: str, model: str) -> ModelRequest:
    return ModelRequest(
        model=model,
        messages=(
            ModelMessage(role="user", content="Search for Nico"),
            ModelMessage(
                role="assistant",
                tool_calls=(
                    {
                        "id": "previous-call",
                        "type": "function",
                        "function": {"name": "web.search", "arguments": "{}"},
                    },
                ),
            ),
            ModelMessage(role="tool", content="{}", tool_call_id="previous-call"),
            ModelMessage(role="user", content="Search again"),
        ),
        endpoint=_endpoint(protocol, base_url),
        tools=(
            ModelToolDefinition(
                name="web.search",
                description="Search the public Web",
                input_schema={"type": "object"},
            ),
        ),
        max_output_tokens=16,
    )


def _assert_provider_safe_tool_name(value: str) -> None:
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value)
    assert value != "web.search"


def _final_action_text() -> str:
    return json.dumps(
        {
            "type": "final",
            "content": "Provider-neutral answer",
            "intent": {
                "interpreted_intent": "Answer the current request",
                "confidence": 0.9,
                "candidates": [
                    {
                        "candidate_id": "answer",
                        "intent": "Answer the current request",
                        "confidence": 0.9,
                    }
                ],
                "ambiguity": 0.1,
                "risk": "low",
                "risk_reasons": [],
                "missing_information": [],
                "safe_partial_answer_possible": True,
            },
            "completion": {
                "answered_user_intent": True,
                "requires_user_response": False,
            },
        },
        separators=(",", ":"),
    )


@pytest.mark.asyncio
async def test_provider_final_fixtures_normalize_to_equivalent_actions() -> None:
    action_text = _final_action_text()

    async def openai_handler(request: httpx.Request) -> httpx.Response:
        chunk = {
            "choices": [
                {
                    "delta": {"content": action_text},
                    "finish_reason": "stop",
                }
            ]
        }
        return httpx.Response(
            200,
            content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
        )

    async def anthropic_handler(request: httpx.Request) -> httpx.Response:
        payloads = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": action_text},
            },
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
        ]
        return httpx.Response(
            200,
            content="".join(f"data: {json.dumps(item)}\n\n" for item in payloads),
        )

    async def gemini_handler(request: httpx.Request) -> httpx.Response:
        chunk = {
            "candidates": [
                {
                    "content": {"parts": [{"text": action_text}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 2,
                "candidatesTokenCount": 1,
                "totalTokenCount": 3,
            },
        }
        return httpx.Response(200, content=f"data: {json.dumps(chunk)}\n\n")

    fixtures = (
        (
            OpenAICompatibleProvider(
                client=_client(openai_handler),
                secret_resolver=FakeSecrets(),
                resolver=lambda host, port: ["93.184.216.34"],
            ),
            _request("openai_compatible", "https://models.example/v1", "openai-test"),
        ),
        (
            AnthropicMessagesProvider(
                client=_client(anthropic_handler),
                secret_resolver=FakeSecrets(),
                resolver=lambda host, port: ["93.184.216.34"],
            ),
            _request("anthropic_messages", "https://api.anthropic.com", "claude-test"),
        ),
        (
            GoogleGeminiProvider(
                client=_client(gemini_handler),
                secret_resolver=FakeSecrets(),
                resolver=lambda host, port: ["93.184.216.34"],
            ),
            _request(
                "google_gemini",
                "https://generativelanguage.googleapis.com/v1beta",
                "gemini-test",
            ),
        ),
    )
    actions = []
    for provider, request in fixtures:
        response = await ModelGateway(
            ModelProviderRegistry([provider]),
            max_attempts=1,
        ).complete(request)
        actions.append(parse_agent_actions(response).actions[0])

    assert all(isinstance(action, FinalAction) for action in actions)
    assert actions[0] == actions[1] == actions[2]


@pytest.mark.asyncio
async def test_anthropic_messages_normalizes_stream_and_discovery_pages() -> None:
    discovery_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal discovery_calls
        assert request.headers["x-api-key"] == "canary-provider-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        if request.method == "GET":
            discovery_calls += 1
            if discovery_calls == 1:
                return httpx.Response(
                    200,
                    json={
                        "data": [{"id": "claude-sonnet-5", "display_name": "Sonnet"}],
                        "has_more": True,
                        "last_id": "claude-sonnet-5",
                    },
                )
            assert request.url.params["after_id"] == "claude-sonnet-5"
            return httpx.Response(
                200,
                json={"data": [{"id": "claude-opus-5"}], "has_more": False},
            )

        body = json.loads(request.content)
        assert body["system"] == "Be concise"
        assert body["messages"] == [{"role": "user", "content": "Hello"}]
        assert body["max_tokens"] == 16
        payloads = [
            (
                "message_start",
                {"type": "message_start", "message": {"id": "msg-1", "usage": {"input_tokens": 3}}},
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hello"},
                },
            ),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 1},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        content = "".join(
            f"event: {event}\ndata: {json.dumps(payload)}\n\n" for event, payload in payloads
        )
        return httpx.Response(200, headers={"request-id": "req-a"}, content=content)

    provider = AnthropicMessagesProvider(
        client=_client(handler),
        secret_resolver=FakeSecrets(),
        resolver=lambda host, port: ["93.184.216.34"],
    )
    gateway = ModelGateway(ModelProviderRegistry([provider]), max_attempts=1)

    response = await gateway.complete(
        _request("anthropic_messages", "https://api.anthropic.com", "claude-sonnet-5")
    )
    discovered = await gateway.discover(
        ModelDiscoveryRequest(
            endpoint=_endpoint("anthropic_messages", "https://api.anthropic.com"),
            limit=10,
        )
    )

    assert response.text == "Hello"
    assert response.finish_reason == "end_turn"
    assert response.usage.total_tokens == 4
    assert response.provider_request_id == "req-a"
    assert [model.id for model in discovered.models] == ["claude-sonnet-5", "claude-opus-5"]
    assert discovered.truncated is False


@pytest.mark.asyncio
async def test_gemini_normalizes_stream_and_filters_non_generation_models() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "canary-provider-key"
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-3.5-flash",
                            "displayName": "Gemini Flash",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {
                            "name": "models/embedding-001",
                            "supportedGenerationMethods": ["embedContent"],
                        },
                    ]
                },
            )

        assert request.url.path.endswith("/v1beta/models/gemini-3.5-flash:streamGenerateContent")
        assert request.url.params["alt"] == "sse"
        body = json.loads(request.content)
        assert body["systemInstruction"]["parts"] == [{"text": "Be concise"}]
        assert body["contents"] == [{"role": "user", "parts": [{"text": "Hello"}]}]
        chunks = [
            {
                "candidates": [
                    {
                        "content": {"parts": [{"text": "Hi"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 2,
                    "candidatesTokenCount": 1,
                    "totalTokenCount": 3,
                },
            }
        ]
        return httpx.Response(
            200,
            headers={"x-request-id": "req-g"},
            content="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks),
        )

    provider = GoogleGeminiProvider(
        client=_client(handler),
        secret_resolver=FakeSecrets(),
        resolver=lambda host, port: ["93.184.216.34"],
    )
    gateway = ModelGateway(ModelProviderRegistry([provider]), max_attempts=1)

    events = [
        event
        async for event in gateway.stream(
            _request(
                "google_gemini",
                "https://generativelanguage.googleapis.com/v1beta",
                "gemini-3.5-flash",
            )
        )
    ]
    discovered = await gateway.discover(
        ModelDiscoveryRequest(
            endpoint=_endpoint("google_gemini", "https://generativelanguage.googleapis.com/v1beta"),
            limit=10,
        )
    )

    assert "".join(event.text_delta or "" for event in events) == "Hi"
    assert events[-1].type is ModelStreamEventType.RESPONSE_COMPLETED
    assert events[-1].usage is not None and events[-1].usage.total_tokens == 3
    assert [model.id for model in discovered.models] == ["gemini-3.5-flash"]


@pytest.mark.asyncio
async def test_openai_discovery_deduplicates_and_truncates() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={"data": [{"id": "model-a"}, {"id": "model-a"}, {"id": "model-b"}]},
        )

    gateway = ModelGateway(
        ModelProviderRegistry(
            [
                OpenAICompatibleProvider(
                    client=_client(handler),
                    secret_resolver=FakeSecrets(),
                    resolver=lambda host, port: ["93.184.216.34"],
                )
            ]
        )
    )

    result = await gateway.discover(
        ModelDiscoveryRequest(
            endpoint=_endpoint("openai_compatible", "https://models.example/v1"),
            limit=1,
        )
    )

    assert [model.id for model in result.models] == ["model-a"]
    assert result.truncated is True


@pytest.mark.asyncio
async def test_openai_compatible_maps_dotted_tool_names_at_the_wire_boundary() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        wire_name = body["tools"][0]["function"]["name"]
        _assert_provider_safe_tool_name(wire_name)
        assert body["messages"][1]["tool_calls"][0]["function"]["name"] == wire_name
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
                                    "function": {"name": wire_name, "arguments": "{}"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        ]
        payload = "".join(f"data: {json.dumps(item)}\n\n" for item in chunks)
        return httpx.Response(200, content=(payload + "data: [DONE]\n\n").encode())

    provider = OpenAICompatibleProvider(
        client=_client(handler),
        secret_resolver=FakeSecrets(),
        resolver=lambda host, port: ["93.184.216.34"],
    )
    response = await ModelGateway(ModelProviderRegistry([provider]), max_attempts=1).complete(
        _tool_request("openai_compatible", "https://models.example/v1", "deepseek-test")
    )

    assert response.tool_calls[0].name == "web.search"


@pytest.mark.asyncio
async def test_anthropic_maps_dotted_tool_names_at_the_wire_boundary() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        wire_name = body["tools"][0]["name"]
        _assert_provider_safe_tool_name(wire_name)
        assert body["messages"][1]["content"][0]["name"] == wire_name
        payloads = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": wire_name,
                    "input": {},
                },
            },
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
        ]
        content = "".join(f"data: {json.dumps(item)}\n\n" for item in payloads)
        return httpx.Response(200, content=content)

    provider = AnthropicMessagesProvider(
        client=_client(handler),
        secret_resolver=FakeSecrets(),
        resolver=lambda host, port: ["93.184.216.34"],
    )
    response = await ModelGateway(ModelProviderRegistry([provider]), max_attempts=1).complete(
        _tool_request("anthropic_messages", "https://api.anthropic.com", "claude-test")
    )

    assert response.tool_calls[0].name == "web.search"


@pytest.mark.asyncio
async def test_gemini_maps_dotted_tool_names_at_the_wire_boundary() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        declaration = body["tools"][0]["functionDeclarations"][0]
        wire_name = declaration["name"]
        _assert_provider_safe_tool_name(wire_name)
        assert body["contents"][1]["parts"][0]["functionCall"]["name"] == wire_name
        chunk = {
            "candidates": [
                {
                    "content": {"parts": [{"functionCall": {"name": wire_name, "args": {}}}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 2,
                "candidatesTokenCount": 1,
                "totalTokenCount": 3,
            },
        }
        return httpx.Response(200, content=f"data: {json.dumps(chunk)}\n\n")

    provider = GoogleGeminiProvider(
        client=_client(handler),
        secret_resolver=FakeSecrets(),
        resolver=lambda host, port: ["93.184.216.34"],
    )
    response = await ModelGateway(ModelProviderRegistry([provider]), max_attempts=1).complete(
        _tool_request(
            "google_gemini",
            "https://generativelanguage.googleapis.com/v1beta",
            "gemini-test",
        )
    )

    assert response.tool_calls[0].name == "web.search"
