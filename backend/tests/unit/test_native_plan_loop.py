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
from nico_agent.models.gateway import ModelGateway
from nico_agent.models.registry import ModelProviderRegistry
from nico_agent.runtime.contracts import (
    ContextSeed,
    RuntimeEventType,
    RuntimeExecutionMode,
    RuntimeIntervention,
    RuntimeServices,
    RuntimeSessionRequest,
    RuntimeSessionStatus,
    RuntimeToolIntent,
    RuntimeToolOutcome,
    RuntimeToolSpec,
)
from nico_agent.runtime.native.planner import parse_plan, plan_content_hash
from nico_agent.runtime.native.provider import NicoNativeRuntimeProvider


class SequencedStructuredProvider:
    name = "openai_compatible"

    def __init__(self, payloads: list[dict | list[ModelStreamEvent]]) -> None:
        self.payloads = payloads
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
        payload = self.payloads[len(self.requests) - 1]
        if isinstance(payload, list):
            for event in payload:
                yield event
            return
        yield ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED)
        yield ModelStreamEvent(
            type=ModelStreamEventType.TEXT_DELTA,
            text_delta=json.dumps(payload, separators=(",", ":")),
        )
        yield ModelStreamEvent(
            type=ModelStreamEventType.RESPONSE_COMPLETED,
            finish_reason="stop",
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15, status="exact"),
            provider_request_id=f"plan-{len(self.requests)}",
        )


class RecordingPlanToolHandler:
    def __init__(self, *, cached: bool = False) -> None:
        self.intents: list[RuntimeToolIntent] = []
        self.cached = cached

    async def list_tools(self) -> tuple[RuntimeToolSpec, ...]:
        return (
            RuntimeToolSpec(
                name="file.write",
                version="1.0.0",
                description="Write a Run workspace file",
                input_schema={"type": "object"},
            ),
        )

    async def execute_tool(self, intent: RuntimeToolIntent) -> RuntimeToolOutcome:
        self.intents.append(intent)
        return RuntimeToolOutcome(
            call_id=intent.call_id,
            tool_call_id="00000000-0000-0000-0000-000000000321",
            run_step_id="00000000-0000-0000-0000-000000000322",
            status="succeeded",
            output={"path": "plan/report.txt"},
            cached=self.cached,
        )


class OneShotInterventionHandler:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.intervention_id = uuid4()

    async def freeze(self, boundary_key: str) -> tuple[RuntimeIntervention, ...]:
        self.calls.append(boundary_key)
        if len(self.calls) > 1:
            return ()
        return (
            RuntimeIntervention(
                intervention_id=self.intervention_id,
                content="Add an explicit rollback step.",
                content_hash="e" * 64,
                boundary_key=boundary_key,
            ),
        )

def _request(**updates) -> RuntimeSessionRequest:
    request = RuntimeSessionRequest(
        tenant_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        task_title="Create a verified report",
        task_input={"topic": "Nico"},
        acceptance={"non_empty": True, "required": ["content"]},
        role="researcher",
        mandate="Plan, execute, verify, and report",
        boundaries=["Do not invent evidence"],
        execution_mode=RuntimeExecutionMode.PLAN_AND_EXECUTE,
        context_seed=ContextSeed(source_refs=("task:input",)),
        model_endpoint_snapshot={
            "id": str(uuid4()),
            "protocol": "openai_compatible",
            "base_url": "https://models.example/v1",
            "credential_ref": "env:NICO_MODEL_SECRET_TEST",
            "allowed_models": ["test-model"],
            "capabilities": {"streaming": True, "structured_output": True, "tools": True},
            "model": "test-model",
        },
        model_config_data={"temperature": 0},
        budgets={"max_plan_steps": 6, "max_reflections": 2, "max_replans": 1},
        execution_manifest={
            "schema_version": 1,
            "runtime_provider": "nico_native",
            "execution_mode": "plan_and_execute",
        },
    )
    return request.model_copy(update=updates)


def _plan(keys: list[str]) -> dict:
    steps = []
    for index, key in enumerate(keys):
        steps.append(
            {
                "key": key,
                "title": key.title(),
                "instruction": f"Execute {key}",
                "acceptance": {"non_empty": True},
                "depends_on": [] if index == 0 else [keys[index - 1]],
            }
        )
    return {"objective": "Produce a verified report", "steps": steps}


@pytest.mark.asyncio
async def test_plan_and_execute_persists_three_step_trace_and_summary() -> None:
    model = SequencedStructuredProvider(
        [
            _plan(["collect", "analyze", "report"]),
            {"output": {"content": "evidence"}},
            {"output": {"content": "analysis"}},
            {"output": {"content": "verified report"}},
        ]
    )
    provider = NicoNativeRuntimeProvider(ModelGateway(ModelProviderRegistry([model])))
    request = _request()
    session = await provider.create_session(request)

    outcome = await provider.execute(session.external_session_id, request, RuntimeServices())
    events = [event async for event in provider.stream_events(session.external_session_id)]

    assert outcome.status is RuntimeSessionStatus.COMPLETED
    assert outcome.output == {
        "result": {"content": "verified report"},
        "execution_summary": {
            "plan_revision": 1,
            "completed_steps": ["collect", "analyze", "report"],
            "reflections": 0,
        },
    }
    assert outcome.checkpoint["schema_version"] == 3
    assert outcome.usage["model_calls"] == 4
    types = [event.type for event in events]
    assert types.count(RuntimeEventType.PLAN_CREATED) == 1
    assert types.count(RuntimeEventType.PLAN_STEP_COMPLETED) == 3
    assert types.count(RuntimeEventType.EVALUATION_COMPLETED) == 4
    assert [request.metadata["call_key"] for request in model.requests] == [
        "planner:1",
        "plan:1:step:collect:attempt:1:round:1",
        "plan:1:step:analyze:attempt:1:round:1",
        "plan:1:step:report:attempt:1:round:1",
    ]


@pytest.mark.asyncio
async def test_plan_injects_guidance_at_the_next_model_boundary() -> None:
    model = SequencedStructuredProvider(
        [_plan(["report"]), {"output": {"content": "verified report"}}]
    )
    provider = NicoNativeRuntimeProvider(ModelGateway(ModelProviderRegistry([model])))
    request = _request()
    session = await provider.create_session(request)
    handler = OneShotInterventionHandler()

    outcome = await provider.execute(
        session.external_session_id,
        request,
        RuntimeServices(intervention_handler=handler),
    )

    assert outcome.status is RuntimeSessionStatus.COMPLETED
    assert handler.calls[0] == "planner:1"
    assert "Add an explicit rollback step" in (
        model.requests[0].messages[-1].content or ""
    )
    assert "UNTRUSTED DATA" in (model.requests[0].messages[-1].content or "")


@pytest.mark.asyncio
async def test_failed_step_reflects_and_creates_new_readable_plan_revision() -> None:
    model = SequencedStructuredProvider(
        [
            _plan(["draft"]),
            {"output": {"content": ""}},
            {
                "decision": "replan",
                "reason": "Draft failed validation",
                "recovery_instruction": "Create a corrected report",
            },
            _plan(["correct"]),
            {"output": {"content": "corrected"}},
        ]
    )
    provider = NicoNativeRuntimeProvider(ModelGateway(ModelProviderRegistry([model])))
    request = _request()
    session = await provider.create_session(request)

    outcome = await provider.execute(session.external_session_id, request, RuntimeServices())
    events = [event async for event in provider.stream_events(session.external_session_id)]

    assert outcome.status is RuntimeSessionStatus.COMPLETED
    assert outcome.output["execution_summary"]["plan_revision"] == 2
    assert outcome.output["execution_summary"]["reflections"] == 1
    plans = [event.payload for event in events if event.type is RuntimeEventType.PLAN_CREATED]
    assert [plan["revision"] for plan in plans] == [1, 2]
    assert plans[1]["supersedes_revision"] == 1
    assert any(
        event.type is RuntimeEventType.PLAN_STATUS_CHANGED
        and event.payload == {"revision": 1, "status": "superseded"}
        for event in events
    )
    assert RuntimeEventType.REFLECTION_COMPLETED in [event.type for event in events]


@pytest.mark.asyncio
async def test_plan_recovery_honors_persisted_replan_without_rebilling_old_calls() -> None:
    initial_model = SequencedStructuredProvider(
        [
            _plan(["draft"]),
            {"output": {"content": ""}},
            {
                "decision": "replan",
                "reason": "Draft failed validation",
                "recovery_instruction": "Create a corrected report",
            },
            _plan(["correct"]),
            {"output": {"content": "corrected"}},
        ]
    )
    initial_provider = NicoNativeRuntimeProvider(
        ModelGateway(ModelProviderRegistry([initial_model]))
    )
    initial_request = _request()
    initial_session = await initial_provider.create_session(initial_request)
    await initial_provider.execute(
        initial_session.external_session_id, initial_request, RuntimeServices()
    )
    initial_events = [
        event async for event in initial_provider.stream_events(initial_session.external_session_id)
    ]
    reflection_checkpoint = next(
        event.payload
        for event in initial_events
        if event.type is RuntimeEventType.CHECKPOINT_SAVED
        and event.payload.get("loop_state") == "reflecting"
        and event.payload.get("recovery_decision") == "replan"
    )
    created_plan = next(
        event.payload
        for event in initial_events
        if event.type is RuntimeEventType.PLAN_CREATED and event.payload["revision"] == 1
    )

    recovered_model = SequencedStructuredProvider(
        [_plan(["correct"]), {"output": {"content": "corrected"}}]
    )
    recovered_provider = NicoNativeRuntimeProvider(
        ModelGateway(ModelProviderRegistry([recovered_model]))
    )
    recovered_request = initial_request.model_copy(
        update={
            "checkpoint": reflection_checkpoint,
            "event_sequence": 40,
            "resume_session_id": initial_session.external_session_id,
            "recovery_state": {
                "model_call_keys": [
                    "planner:1",
                    "plan:1:step:draft:attempt:1:round:1",
                    "reflection:1",
                ],
                "planning": {
                    "active_plan": {
                        "revision": 1,
                        "status": "active",
                        "objective": created_plan["objective"],
                        "content_hash": created_plan["content_hash"],
                        "steps": created_plan["steps"],
                    }
                },
            },
        }
    )
    recovered_session = await recovered_provider.create_session(recovered_request)

    outcome = await recovered_provider.execute(
        recovered_session.external_session_id, recovered_request, RuntimeServices()
    )

    assert outcome.status is RuntimeSessionStatus.COMPLETED
    assert [request.metadata["call_key"] for request in recovered_model.requests] == [
        "planner:2",
        "plan:2:step:correct:attempt:2:round:1",
    ]
    assert outcome.usage["model_calls"] == 5


@pytest.mark.asyncio
async def test_completion_rejection_creates_a_corrective_plan_revision() -> None:
    model = SequencedStructuredProvider(
        [
            _plan(["report"]),
            {"output": {"wrong": "draft"}},
            {
                "decision": "retry",
                "reason": "Final acceptance requires content",
                "recovery_instruction": "Return the required content field",
            },
            _plan(["correct"]),
            {"output": {"content": "accepted"}},
        ]
    )
    provider = NicoNativeRuntimeProvider(ModelGateway(ModelProviderRegistry([model])))
    request = _request()
    session = await provider.create_session(request)

    outcome = await provider.execute(session.external_session_id, request, RuntimeServices())

    assert outcome.status is RuntimeSessionStatus.COMPLETED
    assert outcome.output["result"] == {"content": "accepted"}
    assert outcome.output["execution_summary"]["plan_revision"] == 2
    assert outcome.output["execution_summary"]["reflections"] == 1


@pytest.mark.asyncio
async def test_optional_completion_judge_is_a_separate_billed_model_call() -> None:
    model = SequencedStructuredProvider(
        [
            _plan(["report"]),
            {"output": {"content": "verified"}},
            {"verdict": "passed", "reason": "The result satisfies acceptance."},
        ]
    )
    provider = NicoNativeRuntimeProvider(ModelGateway(ModelProviderRegistry([model])))
    request = _request(run_config={"completion_model_judge": True})
    session = await provider.create_session(request)

    outcome = await provider.execute(session.external_session_id, request, RuntimeServices())
    events = [event async for event in provider.stream_events(session.external_session_id)]

    assert outcome.status is RuntimeSessionStatus.COMPLETED
    assert outcome.usage["model_calls"] == 3
    assert model.requests[-1].metadata["call_key"] == "completion-judge:1:1"
    completion_evaluations = [
        event.payload
        for event in events
        if event.type is RuntimeEventType.EVALUATION_COMPLETED
        and event.payload["evaluation_type"] == "completion"
    ]
    assert [item["method"] for item in completion_evaluations] == ["deterministic", "model"]
    assert completion_evaluations[-1]["model_call_key"] == "completion-judge:1:1"


@pytest.mark.asyncio
async def test_recovery_advances_checkpoint_from_committed_plan_step_fact() -> None:
    plan_payload = _plan(["report"])
    plan_hash = plan_content_hash(parse_plan(plan_payload))
    model = SequencedStructuredProvider([])
    provider = NicoNativeRuntimeProvider(ModelGateway(ModelProviderRegistry([model])))
    request = _request(
        recovery_state={
            "model_call_keys": ["planner:1", "plan:1:step:report:attempt:1:round:1"],
            "last_context_version": 2,
            "usage": {
                "model_calls": 2,
                "input_tokens": 20,
                "output_tokens": 10,
                "total_tokens": 30,
            },
            "planning": {
                "active_plan": {
                    "revision": 1,
                    "status": "active",
                    "objective": plan_payload["objective"],
                    "content_hash": plan_hash,
                    "steps": plan_payload["steps"],
                    "completed_step_keys": ["report"],
                    "latest_output": {"content": "committed before checkpoint"},
                    "reflection_count": 0,
                    "latest_reflection": None,
                }
            },
        },
        checkpoint=None,
        event_sequence=20,
        resume_session_id="nico:previous-attempt",
    )
    session = await provider.create_session(request)

    outcome = await provider.execute(session.external_session_id, request, RuntimeServices())

    assert outcome.status is RuntimeSessionStatus.COMPLETED
    assert outcome.output["result"] == {"content": "committed before checkpoint"}
    assert outcome.usage["model_calls"] == 2
    assert model.requests == []


@pytest.mark.asyncio
async def test_plan_step_uses_tool_gateway_with_pre_action_checkpoint() -> None:
    tool_response = [
        ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED),
        ModelStreamEvent(
            type=ModelStreamEventType.TOOL_CALL_DELTA,
            tool_index=0,
            tool_call_id="write-report",
            tool_name="file.write",
            tool_arguments_delta='{"path":"plan/report.txt","content":"verified"}',
        ),
        ModelStreamEvent(
            type=ModelStreamEventType.RESPONSE_COMPLETED,
            finish_reason="tool_calls",
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15, status="exact"),
        ),
    ]
    model = SequencedStructuredProvider(
        [
            _plan(["report"]),
            tool_response,
            {"output": {"content": "verified"}},
        ]
    )
    handler = RecordingPlanToolHandler()
    provider = NicoNativeRuntimeProvider(ModelGateway(ModelProviderRegistry([model])))
    request = _request(budgets={"max_plan_steps": 4, "max_tool_calls": 1})
    session = await provider.create_session(request)

    outcome = await provider.execute(
        session.external_session_id,
        request,
        RuntimeServices(tool_handler=handler),
    )
    events = [event async for event in provider.stream_events(session.external_session_id)]

    assert outcome.status is RuntimeSessionStatus.COMPLETED
    assert outcome.output["result"] == {"content": "verified"}
    assert outcome.usage["tool_calls"] == 1
    assert len(handler.intents) == 1
    assert handler.intents[0].version == "1.0.0"
    assert handler.intents[0].idempotency_key.endswith(":1:report:1:write-report")
    assert (
        handler.intents[0].checkpoint["step_state"]["pending_actions"][0]["call_id"]
        == "write-report"
    )
    assert [request.metadata["call_key"] for request in model.requests] == [
        "planner:1",
        "plan:1:step:report:attempt:1:round:1",
        "plan:1:step:report:attempt:1:round:2",
    ]
    assert RuntimeEventType.TOOL_CALL_STARTED in [event.type for event in events]
    assert RuntimeEventType.TOOL_CALL_COMPLETED in [event.type for event in events]


@pytest.mark.asyncio
async def test_plan_tool_recovery_reuses_pre_action_idempotency_key() -> None:
    tool_response = [
        ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED),
        ModelStreamEvent(
            type=ModelStreamEventType.TOOL_CALL_DELTA,
            tool_index=0,
            tool_call_id="write-report",
            tool_name="file.write",
            tool_arguments_delta='{"path":"plan/report.txt","content":"verified"}',
        ),
        ModelStreamEvent(
            type=ModelStreamEventType.RESPONSE_COMPLETED,
            finish_reason="tool_calls",
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15, status="exact"),
        ),
    ]
    initial_model = SequencedStructuredProvider([_plan(["report"]), tool_response])
    initial_handler = RecordingPlanToolHandler()
    initial_provider = NicoNativeRuntimeProvider(
        ModelGateway(ModelProviderRegistry([initial_model]))
    )
    initial_request = _request(
        budgets={"max_plan_steps": 4, "max_tool_calls": 1, "max_step_iterations": 1}
    )
    initial_session = await initial_provider.create_session(initial_request)

    initial_outcome = await initial_provider.execute(
        initial_session.external_session_id,
        initial_request,
        RuntimeServices(tool_handler=initial_handler),
    )

    assert initial_outcome.error["code"] == "PLAN_STEP_ITERATIONS_EXHAUSTED"
    pre_action = initial_handler.intents[0].checkpoint
    plan_payload = _plan(["report"])
    recovered_model = SequencedStructuredProvider([{"output": {"content": "verified"}}])
    recovered_handler = RecordingPlanToolHandler(cached=True)
    recovered_provider = NicoNativeRuntimeProvider(
        ModelGateway(ModelProviderRegistry([recovered_model]))
    )
    recovered_request = initial_request.model_copy(
        update={
            "checkpoint": pre_action,
            "event_sequence": 30,
            "resume_session_id": initial_session.external_session_id,
            "recovery_state": {
                "model_call_keys": [
                    "planner:1",
                    "plan:1:step:report:attempt:1:round:1",
                ],
                "last_context_version": 2,
                "usage": pre_action["usage"],
                "planning": {
                    "active_plan": {
                        "revision": 1,
                        "status": "active",
                        "objective": plan_payload["objective"],
                        "content_hash": pre_action["plan_content_hash"],
                        "steps": plan_payload["steps"],
                        "completed_step_keys": [],
                        "latest_output": None,
                        "reflection_count": 0,
                        "latest_reflection": None,
                    }
                },
            },
            "budgets": {
                "max_plan_steps": 4,
                "max_tool_calls": 1,
                "max_step_iterations": 2,
            },
        }
    )
    recovered_session = await recovered_provider.create_session(recovered_request)

    outcome = await recovered_provider.execute(
        recovered_session.external_session_id,
        recovered_request,
        RuntimeServices(tool_handler=recovered_handler),
    )

    assert outcome.status is RuntimeSessionStatus.COMPLETED
    assert len(recovered_handler.intents) == 1
    assert recovered_handler.cached is True
    assert (
        recovered_handler.intents[0].idempotency_key == initial_handler.intents[0].idempotency_key
    )
    assert recovered_model.requests[0].metadata["call_key"] == (
        "plan:1:step:report:attempt:1:round:2"
    )
