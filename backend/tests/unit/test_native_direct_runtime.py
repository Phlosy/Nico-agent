from __future__ import annotations

import json
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
from nico_agent.runtime.native.prompts import NATIVE_CONTINUITY_POLICY
from nico_agent.runtime.native.provider import NicoNativeRuntimeProvider
from nico_agent.user_inputs.contracts import RuntimeUserInputRequest


class FakeModelProvider:
    name = "openai_compatible"

    def __init__(self, events: list[ModelStreamEvent]) -> None:
        self.events = events
        self.requests = []

    def describe_capabilities(self):
        return frozenset(
            {
                ModelCapability.STREAMING,
                ModelCapability.TOOLS,
                ModelCapability.STRUCTURED_OUTPUT,
            }
        )

    async def stream(self, request):
        self.requests.append(request)
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


class SequencedModelProvider(FakeModelProvider):
    def __init__(self, responses: list[list[ModelStreamEvent]]) -> None:
        super().__init__([])
        self.responses = responses

    async def stream(self, request):
        self.requests.append(request)
        for event in self.responses[len(self.requests) - 1]:
            yield event


class RecordingUserInputHandler:
    def __init__(self) -> None:
        self.intents = []

    async def request_user_input(self, intent):
        self.intents.append(intent)
        return RuntimeUserInputRequest(
            id=uuid4(),
            status="requested",
            revision=1,
            wake_key="a" * 64,
            expires_at="2099-01-01T00:00:00Z",
        )


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


def _sequenced_native(responses: list[str]) -> tuple[NicoNativeRuntimeProvider, object]:
    provider = SequencedModelProvider(
        [
            [
                ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED),
                ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta=response),
                ModelStreamEvent(
                    type=ModelStreamEventType.RESPONSE_COMPLETED,
                    finish_reason="stop",
                    usage=ModelUsage(total_tokens=5, status="exact"),
                ),
            ]
            for response in responses
        ]
    )
    return NicoNativeRuntimeProvider(ModelGateway(ModelProviderRegistry([provider]))), provider


def _action_envelope(action_type: str, **fields) -> str:
    return json.dumps(
        {
            "type": action_type,
            **fields,
            "intent": {
                "interpreted_intent": "Provide the requested answer",
                "confidence": 0.95,
                "candidates": [
                    {
                        "candidate_id": "answer",
                        "intent": "Provide the requested answer",
                        "confidence": 0.95,
                    }
                ],
                "ambiguity": 0.05,
                "risk": "low",
                "risk_reasons": [],
                "missing_information": [],
                "safe_partial_answer_possible": True,
            },
        }
    )


def test_direct_context_is_deterministic_and_marks_untrusted_data() -> None:
    first = build_direct_context(_request())
    second = build_direct_context(_request(run_id=_request().run_id))

    assert first.content_hash == second.content_hash
    assert first.messages[0].role == "system"
    assert "UNTRUSTED DATA" in first.messages[-1].content
    assert "Do not expose secrets" in first.messages[0].content
    assert (first.messages[0].content or "").count(NATIVE_CONTINUITY_POLICY) == 1


@pytest.mark.asyncio
async def test_native_direct_streams_and_completes_without_external_runtime() -> None:
    envelope = _action_envelope(
        "final",
        content="Hello Nico",
        completion={"answered_user_intent": True, "requires_user_response": False},
    )
    provider = _native(
        [
            ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED),
            ModelStreamEvent(
                type=ModelStreamEventType.TEXT_DELTA,
                text_delta=envelope[: len(envelope) // 2],
            ),
            ModelStreamEvent(
                type=ModelStreamEventType.TEXT_DELTA,
                text_delta=envelope[len(envelope) // 2 :],
            ),
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
    event_types = [event.type for event in events]
    completed_index = event_types.index(RuntimeEventType.MODEL_CALL_COMPLETED)
    action_index = event_types.index(RuntimeEventType.ACTION_BATCH_CREATED)
    step_index = event_types.index(RuntimeEventType.STEP_COMPLETED)
    assert completed_index < action_index < step_index
    action_event = events[action_index]
    assert action_event.payload["batch"]["source_format"] == "structured_json"
    assert (
        outcome.checkpoint["last_action_batch_key"] == action_event.payload["batch"]["content_hash"]
    )
    output_delta = next(
        event for event in events if event.type is RuntimeEventType.MODEL_OUTPUT_DELTA
    )
    assert output_delta.payload["visibility"] == "internal"
    assert events[-1].type is RuntimeEventType.RUN_COMPLETED


@pytest.mark.asyncio
async def test_direct_rejects_unnecessary_question_once_and_model_answers_interpreted_intent() -> (
    None
):
    ask = _action_envelope(
        "ask_user",
        question="Do you mean the platform timestamp?",
        reason="I want to confirm the typo.",
    )
    final = _action_envelope(
        "final",
        content="The platform supplies the timestamp in the Run context.",
        completion={"answered_user_intent": True, "requires_user_response": False},
    )
    native, model = _sequenced_native([ask, final])
    handler = RecordingUserInputHandler()
    request = _request(task_input={"message": "你平台是怎么提供de"})
    handle = await native.create_session(request)

    outcome = await native.execute(
        handle.external_session_id,
        request,
        RuntimeServices(user_input_handler=handler),
    )

    assert outcome.status is RuntimeSessionStatus.COMPLETED
    assert outcome.output == {"content": "The platform supplies the timestamp in the Run context."}
    assert len(model.requests) == 2
    assert handler.intents == []
    correction = model.requests[1].messages[-1].content
    assert "clarification_policy_observation" in correction
    assert "Provide the requested answer" in correction


@pytest.mark.asyncio
async def test_direct_repeated_rejected_question_exhausts_one_correction() -> None:
    ask = _action_envelope(
        "ask_user",
        question="Should I answer?",
        reason="I prefer to ask.",
    )
    native, model = _sequenced_native([ask, ask])
    request = _request()
    handle = await native.create_session(request)

    outcome = await native.execute(
        handle.external_session_id,
        request,
        RuntimeServices(user_input_handler=RecordingUserInputHandler()),
    )

    assert outcome.status is RuntimeSessionStatus.FAILED
    assert len(model.requests) == 2
    assert outcome.error["code"] == "CLARIFICATION_CORRECTION_EXHAUSTED"


@pytest.mark.asyncio
async def test_direct_corrects_pseudo_final_once_before_completion() -> None:
    invalid = _action_envelope(
        "final",
        content="Could you clarify?",
        completion={"answered_user_intent": False, "requires_user_response": True},
    )
    valid = _action_envelope(
        "final",
        content="The corrected authoritative answer.",
        completion={"answered_user_intent": True, "requires_user_response": False},
    )
    native, model = _sequenced_native([invalid, valid])
    request = _request()
    handle = await native.create_session(request)

    outcome = await native.execute(handle.external_session_id, request, RuntimeServices())

    assert outcome.status is RuntimeSessionStatus.COMPLETED
    assert outcome.output == {"content": "The corrected authoritative answer."}
    assert len(model.requests) == 2
    assert model.requests[1].tools == ()
    assert "semantic_completion_observation" in model.requests[1].messages[-1].content


@pytest.mark.asyncio
async def test_direct_repeated_invalid_final_exhausts_semantic_correction() -> None:
    invalid = _action_envelope(
        "final",
        content="I still need more information.",
        completion={"answered_user_intent": False, "requires_user_response": True},
    )
    native, model = _sequenced_native([invalid, invalid])
    request = _request()
    handle = await native.create_session(request)

    outcome = await native.execute(handle.external_session_id, request, RuntimeServices())

    assert outcome.status is RuntimeSessionStatus.FAILED
    assert outcome.error["code"] == "SEMANTIC_FINAL_CORRECTION_EXHAUSTED"
    assert len(model.requests) == 2


@pytest.mark.asyncio
async def test_direct_corrected_ask_user_returns_to_clarification_gate() -> None:
    invalid = _action_envelope(
        "final",
        content="I cannot choose the target.",
        completion={"answered_user_intent": False, "requires_user_response": True},
    )
    ask = json.loads(
        _action_envelope(
            "ask_user",
            question="Which target: file or database?",
            reason="Both targets are equally plausible.",
        )
    )
    ask["intent"].update({"confidence": 0.55, "ambiguity": 0.9})
    ask["intent"]["candidates"] = [
        {"candidate_id": "file", "intent": "Use the file", "confidence": 0.55},
        {
            "candidate_id": "database",
            "intent": "Use the database",
            "confidence": 0.54,
        },
    ]
    native, _model = _sequenced_native([invalid, json.dumps(ask)])
    handler = RecordingUserInputHandler()
    request = _request()
    handle = await native.create_session(request)

    outcome = await native.execute(
        handle.external_session_id,
        request,
        RuntimeServices(user_input_handler=handler),
    )

    assert outcome.status is RuntimeSessionStatus.SUSPENDED
    assert outcome.wake_condition["type"] == "user_input"
    assert len(handler.intents) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("structured_output", [True, False])
@pytest.mark.parametrize("pseudo_final", ["legacy pseudo final", "{not-json"])
async def test_direct_legacy_final_requires_structured_correction(
    structured_output: bool,
    pseudo_final: str,
) -> None:
    valid = _action_envelope(
        "final",
        content="Structured correction.",
        completion={"answered_user_intent": True, "requires_user_response": False},
    )
    native, model = _sequenced_native([pseudo_final, valid])
    request = _request(
        model_endpoint_snapshot={
            **_request().model_endpoint_snapshot,
            "capabilities": {
                "streaming": True,
                "tools": True,
                "structured_output": structured_output,
            },
        }
    )
    handle = await native.create_session(request)

    outcome = await native.execute(handle.external_session_id, request, RuntimeServices())

    assert outcome.status is RuntimeSessionStatus.COMPLETED
    assert outcome.output == {"content": "Structured correction."}
    assert len(model.requests) == 2
    assert (model.requests[1].response_format is not None) is structured_output


@pytest.mark.asyncio
async def test_direct_pending_user_input_blocks_claimed_final_completion() -> None:
    valid = _action_envelope(
        "final",
        content="Claimed answer.",
        completion={"answered_user_intent": True, "requires_user_response": False},
    )
    native, model = _sequenced_native([valid, valid])
    request = _request(
        execution_manifest={"completion_pending_user_input_count": 1},
    )
    handle = await native.create_session(request)

    outcome = await native.execute(handle.external_session_id, request, RuntimeServices())

    assert outcome.status is RuntimeSessionStatus.FAILED
    assert outcome.error["code"] == "SEMANTIC_FINAL_CORRECTION_EXHAUSTED"
    assert len(model.requests) == 2


@pytest.mark.asyncio
async def test_direct_recovers_committed_clarification_calls_without_provider_rebilling() -> None:
    ask = _action_envelope(
        "ask_user",
        question="Did you mean the platform timestamp?",
        reason="The input contains a typo.",
    )
    final = _action_envelope(
        "final",
        content="Recovered correction answer.",
        completion={"answered_user_intent": True, "requires_user_response": False},
    )
    initial, _model = _sequenced_native([ask, final])
    request = _request(task_input={"message": "你平台是怎么提供de"})
    handle = await initial.create_session(request)
    first = await initial.execute(
        handle.external_session_id,
        request,
        RuntimeServices(user_input_handler=RecordingUserInputHandler()),
    )
    assert first.status is RuntimeSessionStatus.COMPLETED
    events = [event async for event in initial.stream_events(handle.external_session_id)]
    started = {
        event.payload["call_key"]: event
        for event in events
        if event.type is RuntimeEventType.MODEL_CALL_STARTED
    }
    completed = {
        event.payload["call_key"]: event
        for event in events
        if event.type is RuntimeEventType.MODEL_CALL_COMPLETED
    }
    recovery_state = {
        "model_call_keys": list(completed),
        "recoverable_action_calls": {
            call_key: {
                "request_hash": started[call_key].payload["request_hash"],
                "response_redacted": event.payload["response_redacted"],
                "response_hash": event.payload["response_hash"],
                "provider_request_id": event.payload["provider_request_id"],
                "usage": event.payload["usage"],
                "usage_status": event.payload["usage_status"],
                "cost": event.payload["cost"],
                "cost_status": event.payload["cost_status"],
                "action_batch_exists": True,
            }
            for call_key, event in completed.items()
        },
    }
    recovered_request = request.model_copy(update={"recovery_state": recovery_state})
    recovered = _failing_native()
    recovered_handle = await recovered.create_session(recovered_request)

    outcome = await recovered.execute(
        recovered_handle.external_session_id,
        recovered_request,
        RuntimeServices(user_input_handler=RecordingUserInputHandler()),
    )

    assert outcome.status is RuntimeSessionStatus.COMPLETED
    assert outcome.output == {"content": "Recovered correction answer."}


@pytest.mark.asyncio
async def test_direct_recovers_committed_semantic_final_correction_without_rebilling() -> None:
    invalid = _action_envelope(
        "final",
        content="I still need more information.",
        completion={"answered_user_intent": False, "requires_user_response": True},
    )
    final = _action_envelope(
        "final",
        content="Recovered semantic correction answer.",
        completion={"answered_user_intent": True, "requires_user_response": False},
    )
    initial, _model = _sequenced_native([invalid, final])
    request = _request()
    handle = await initial.create_session(request)
    first = await initial.execute(handle.external_session_id, request, RuntimeServices())
    assert first.status is RuntimeSessionStatus.COMPLETED
    events = [event async for event in initial.stream_events(handle.external_session_id)]
    started = {
        event.payload["call_key"]: event
        for event in events
        if event.type is RuntimeEventType.MODEL_CALL_STARTED
    }
    completed = {
        event.payload["call_key"]: event
        for event in events
        if event.type is RuntimeEventType.MODEL_CALL_COMPLETED
    }
    assert set(completed) == {"model:1", "semantic-final-correction:direct:1"}
    recovery_state = {
        "model_call_keys": list(completed),
        "recoverable_action_calls": {
            call_key: {
                "request_hash": started[call_key].payload["request_hash"],
                "response_redacted": event.payload["response_redacted"],
                "response_hash": event.payload["response_hash"],
                "provider_request_id": event.payload["provider_request_id"],
                "usage": event.payload["usage"],
                "usage_status": event.payload["usage_status"],
                "cost": event.payload["cost"],
                "cost_status": event.payload["cost_status"],
                "action_batch_exists": True,
            }
            for call_key, event in completed.items()
        },
    }
    recovered_request = request.model_copy(update={"recovery_state": recovery_state})
    recovered = _failing_native()
    recovered_handle = await recovered.create_session(recovered_request)

    outcome = await recovered.execute(
        recovered_handle.external_session_id,
        recovered_request,
        RuntimeServices(),
    )

    assert outcome.status is RuntimeSessionStatus.COMPLETED
    assert outcome.output == {"content": "Recovered semantic correction answer."}


@pytest.mark.asyncio
async def test_direct_allows_materially_ambiguous_question_and_suspends() -> None:
    ask = json.loads(
        _action_envelope(
            "ask_user",
            question="Which target: file or database?",
            reason="Both targets are equally plausible.",
        )
    )
    ask["intent"]["candidates"] = [
        {"candidate_id": "file", "intent": "Use the file", "confidence": 0.55},
        {"candidate_id": "database", "intent": "Use the database", "confidence": 0.54},
    ]
    ask["intent"]["confidence"] = 0.55
    ask["intent"]["ambiguity"] = 0.9
    native, _model = _sequenced_native([json.dumps(ask)])
    handler = RecordingUserInputHandler()
    request = _request()
    handle = await native.create_session(request)

    outcome = await native.execute(
        handle.external_session_id,
        request,
        RuntimeServices(user_input_handler=handler),
    )

    assert outcome.status is RuntimeSessionStatus.SUSPENDED
    assert outcome.wake_condition["type"] == "user_input"
    assert len(handler.intents) == 1
    assert handler.intents[0].question == "Which target: file or database?"


@pytest.mark.asyncio
async def test_direct_repairs_terminal_model_call_gap_without_calling_provider_again() -> None:
    envelope = _action_envelope(
        "final",
        content="Recovered answer",
        completion={"answered_user_intent": True, "requires_user_response": False},
    )
    first = _native(
        [
            ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED),
            ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta=envelope),
            ModelStreamEvent(
                type=ModelStreamEventType.RESPONSE_COMPLETED,
                finish_reason="stop",
                usage=ModelUsage(
                    input_tokens=7,
                    output_tokens=2,
                    total_tokens=9,
                    status="exact",
                ),
                provider_request_id="charged-once",
            ),
        ]
    )
    request = _request()
    first_handle = await first.create_session(request)
    await first.execute(first_handle.external_session_id, request, RuntimeServices())
    first_events = [event async for event in first.stream_events(first_handle.external_session_id)]
    started = next(
        event for event in first_events if event.type is RuntimeEventType.MODEL_CALL_STARTED
    )
    completed = next(
        event for event in first_events if event.type is RuntimeEventType.MODEL_CALL_COMPLETED
    )
    recovery_state = {
        "model_call_keys": ["model:1"],
        "recoverable_action_calls": {
            "model:1": {
                "request_hash": started.payload["request_hash"],
                "response_redacted": completed.payload["response_redacted"],
                "response_hash": completed.payload["response_hash"],
                "provider_request_id": completed.payload["provider_request_id"],
                "usage": completed.payload["usage"],
                "usage_status": completed.payload["usage_status"],
                "cost": completed.payload["cost"],
                "cost_status": completed.payload["cost_status"],
            }
        },
    }
    recovered_request = request.model_copy(update={"recovery_state": recovery_state})
    recovered = _failing_native()
    recovered_handle = await recovered.create_session(recovered_request)

    outcome = await recovered.execute(
        recovered_handle.external_session_id,
        recovered_request,
        RuntimeServices(),
    )
    events = [
        event async for event in recovered.stream_events(recovered_handle.external_session_id)
    ]

    assert outcome.status is RuntimeSessionStatus.COMPLETED
    assert outcome.output == {"content": "Recovered answer"}
    event_types = [event.type for event in events]
    assert RuntimeEventType.MODEL_CALL_STARTED not in event_types
    assert RuntimeEventType.MODEL_CALL_COMPLETED not in event_types
    action = next(event for event in events if event.type is RuntimeEventType.ACTION_BATCH_CREATED)
    assert action.payload["repair"] == {
        "kind": "post_model_call_commit",
        "ordinal": 1,
        "reason_code": "ACTION_BATCH_COMMIT_INTERRUPTED",
    }


@pytest.mark.asyncio
async def test_direct_dispatches_structured_final_content_in_shadow_mode() -> None:
    envelope = _action_envelope(
        "final",
        content="shadow only",
        completion={
            "answered_user_intent": True,
            "requires_user_response": False,
        },
    )
    provider = _native(
        [
            ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED),
            ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta=envelope),
            ModelStreamEvent(
                type=ModelStreamEventType.RESPONSE_COMPLETED,
                finish_reason="stop",
                usage=ModelUsage(
                    input_tokens=5,
                    output_tokens=5,
                    total_tokens=10,
                    status="exact",
                ),
            ),
        ]
    )
    request = _request()
    handle = await provider.create_session(request)

    outcome = await provider.execute(handle.external_session_id, request, RuntimeServices())

    assert outcome.status is RuntimeSessionStatus.COMPLETED
    assert outcome.output == {"content": "shadow only"}
    schema = provider.loop.gateway.registry.get("openai_compatible")
    assert schema is not None


@pytest.mark.asyncio
async def test_direct_live_schema_exposes_final_but_rejects_unadvertised_ask_user() -> None:
    envelope = _action_envelope(
        "ask_user",
        question="Which target?",
        reason="A target is required",
    )
    provider = _native(
        [
            ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta=envelope),
            ModelStreamEvent(type=ModelStreamEventType.RESPONSE_COMPLETED, finish_reason="stop"),
        ]
    )
    request = _request()
    handle = await provider.create_session(request)

    outcome = await provider.execute(handle.external_session_id, request, RuntimeServices())

    assert outcome.status is RuntimeSessionStatus.FAILED
    assert outcome.error["code"] == "MODEL_PROTOCOL_ERROR"
    assert "ACTION_KIND_UNSUPPORTED" in outcome.error["message"]
    model_request = provider.loop.gateway.registry.get("openai_compatible").requests[0]
    schema_text = json.dumps(model_request.response_format, sort_keys=True)
    assert '"final"' in schema_text
    assert "ask_user" not in schema_text


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
