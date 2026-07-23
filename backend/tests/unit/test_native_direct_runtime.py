from __future__ import annotations

from uuid import uuid4

import pytest

from nico_agent.models.contracts import (
    ModelCapability,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelUsage,
)
from nico_agent.models.errors import ModelProviderError
from nico_agent.models.gateway import ModelGateway
from nico_agent.models.registry import ModelProviderRegistry
from nico_agent.runtime.contracts import (
    ContextSeed,
    RuntimeEventType,
    RuntimeExecutionMode,
    RuntimeServices,
    RuntimeSessionRequest,
    RuntimeSessionStatus,
)
from nico_agent.runtime.native.context import build_direct_context
from nico_agent.runtime.native.provider import NicoNativeRuntimeProvider


class FakeModelProvider:
    name = "openai_compatible"

    def __init__(self, events: list[ModelStreamEvent]) -> None:
        self.events = events

    def describe_capabilities(self):
        return frozenset(
            {
                ModelCapability.STREAMING,
                ModelCapability.TOOLS,
                ModelCapability.STRUCTURED_OUTPUT,
            }
        )

    async def stream(self, request):
        for event in self.events:
            yield event


class FailingModelProvider(FakeModelProvider):
    async def stream(self, request):
        raise ModelProviderError(
            "MODEL_PROVIDER_UNAVAILABLE",
            "model provider connection failed",
            retryable=False,
        )
        yield  # pragma: no cover - keeps this method an async generator


def _request(**updates) -> RuntimeSessionRequest:
    request = RuntimeSessionRequest(
        tenant_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        task_title="Say hello",
        task_input={"name": "Nico"},
        acceptance={"non_empty": True},
        role="assistant",
        mandate="Provide a concise answer",
        boundaries=["Do not expose secrets"],
        execution_mode=RuntimeExecutionMode.DIRECT,
        context_seed=ContextSeed(
            source_refs=("agent:version", "task:input"),
            untrusted_context=({"source": "task:input", "content": {"name": "Nico"}},),
        ),
        model_endpoint_snapshot={
            "id": str(uuid4()),
            "protocol": "openai_compatible",
            "base_url": "https://models.example/v1",
            "credential_ref": "env:NICO_MODEL_SECRET_TEST",
            "allowed_models": ["test-model"],
            "capabilities": {
                "streaming": True,
                "tools": True,
                "structured_output": True,
            },
            "model": "test-model",
        },
        model_config_data={"temperature": 0},
    )
    return request.model_copy(update=updates)


def _native(events: list[ModelStreamEvent]) -> NicoNativeRuntimeProvider:
    gateway = ModelGateway(ModelProviderRegistry([FakeModelProvider(events)]))
    return NicoNativeRuntimeProvider(gateway)


def _failing_native() -> NicoNativeRuntimeProvider:
    gateway = ModelGateway(ModelProviderRegistry([FailingModelProvider([])]), max_attempts=1)
    return NicoNativeRuntimeProvider(gateway)


def test_direct_context_is_deterministic_and_marks_untrusted_data() -> None:
    first = build_direct_context(_request())
    second = build_direct_context(_request(run_id=_request().run_id))

    assert first.content_hash == second.content_hash
    assert first.messages[0].role == "system"
    assert "UNTRUSTED DATA" in first.messages[-1].content
    assert "Do not expose secrets" in first.messages[0].content


@pytest.mark.asyncio
async def test_native_direct_streams_and_completes_without_external_runtime() -> None:
    provider = _native(
        [
            ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED),
            ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta="Hello "),
            ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta="Nico"),
            ModelStreamEvent(
                type=ModelStreamEventType.RESPONSE_COMPLETED,
                finish_reason="stop",
                usage=ModelUsage(
                    input_tokens=5,
                    output_tokens=2,
                    total_tokens=7,
                    status="exact",
                ),
                provider_request_id="req-native-1",
            ),
        ]
    )
    request = _request()
    handle = await provider.create_session(request)
    outcome = await provider.execute(handle.external_session_id, request, RuntimeServices())
    events = [event async for event in provider.stream_events(handle.external_session_id)]

    assert outcome.status is RuntimeSessionStatus.COMPLETED
    assert outcome.output == {"content": "Hello Nico"}
    assert outcome.usage["total_tokens"] == 7
    assert RuntimeEventType.CONTEXT_SNAPSHOT_CREATED in [event.type for event in events]
    assert RuntimeEventType.MODEL_CALL_COMPLETED in [event.type for event in events]
    output_delta = next(
        event for event in events if event.type is RuntimeEventType.MODEL_OUTPUT_DELTA
    )
    assert output_delta.payload["visibility"] == "assistant"
    assert events[-1].type is RuntimeEventType.RUN_COMPLETED


@pytest.mark.asyncio
async def test_direct_mode_rejects_tool_actions_without_executing_them() -> None:
    provider = _native(
        [
            ModelStreamEvent(
                type=ModelStreamEventType.TOOL_CALL_DELTA,
                tool_index=0,
                tool_call_id="call-1",
                tool_name="file.read",
                tool_arguments_delta='{"path":"secret"}',
            ),
            ModelStreamEvent(
                type=ModelStreamEventType.RESPONSE_COMPLETED,
                finish_reason="tool_calls",
            ),
        ]
    )
    request = _request()
    handle = await provider.create_session(request)

    outcome = await provider.execute(handle.external_session_id, request, RuntimeServices())

    assert outcome.status is RuntimeSessionStatus.FAILED
    assert outcome.error["code"] == "MODE_CAPABILITY_VIOLATION"


@pytest.mark.asyncio
async def test_native_direct_fails_stably_without_model_endpoint() -> None:
    provider = _native([])
    request = _request(model_endpoint_snapshot=None)
    handle = await provider.create_session(request)

    outcome = await provider.execute(handle.external_session_id, request, RuntimeServices())

    assert outcome.status is RuntimeSessionStatus.FAILED
    assert outcome.error["code"] == "MODEL_ENDPOINT_REQUIRED"


@pytest.mark.asyncio
async def test_native_direct_maps_model_failure_to_terminal_outcome() -> None:
    provider = _failing_native()
    request = _request()
    handle = await provider.create_session(request)

    outcome = await provider.execute(handle.external_session_id, request, RuntimeServices())

    assert outcome.status is RuntimeSessionStatus.FAILED
    assert outcome.error["code"] == "MODEL_PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_native_direct_honors_cancellation_before_model_call() -> None:
    provider = _native([])
    request = _request()
    handle = await provider.create_session(request)
    await provider.cancel(handle.external_session_id)

    outcome = await provider.execute(handle.external_session_id, request, RuntimeServices())

    assert outcome.status is RuntimeSessionStatus.CANCELLED
    assert outcome.error["code"] == "CANCELLED"


@pytest.mark.asyncio
async def test_native_plan_mode_rejects_invalid_planner_output() -> None:
    provider = _native([])
    request = _request(execution_mode=RuntimeExecutionMode.PLAN_AND_EXECUTE)
    handle = await provider.create_session(request)

    outcome = await provider.execute(handle.external_session_id, request, RuntimeServices())

    assert outcome.status is RuntimeSessionStatus.FAILED
    assert outcome.error["code"] == "PLANNER_OUTPUT_INVALID"
