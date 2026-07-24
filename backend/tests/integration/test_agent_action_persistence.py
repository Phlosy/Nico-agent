from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.api import create_app
from nico_agent.config import Settings
from nico_agent.database import Database, TenantContext
from nico_agent.domain.models import (
    Agent,
    AgentActionBatch,
    AgentActionRecord,
    AgentActionRepair,
    AgentVersion,
    ContextSnapshot,
    ModelCall,
    ModelEndpoint,
    Project,
    Run,
    RunStep,
    RuntimeSession,
    Task,
    Tenant,
)
from nico_agent.models.contracts import (
    ModelCapability,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelToolCall,
    ModelUsage,
)
from nico_agent.models.errors import ModelProviderError
from nico_agent.models.gateway import ModelGateway
from nico_agent.models.registry import ModelProviderRegistry
from nico_agent.runtime import NicoNativeRuntimeProvider, RuntimeProviderRegistry
from nico_agent.runtime.contracts import RuntimeEvent, RuntimeEventType, RuntimeServices
from nico_agent.runtime.executor import RuntimeWorker
from nico_agent.runtime.native.action_parser import parse_agent_actions
from nico_agent.runtime.service import RuntimeExecutionService

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


class FinalModelProvider:
    name = "openai_compatible"

    def __init__(self, content: str = "durable final") -> None:
        self.content = content
        self.calls = 0

    def describe_capabilities(self):
        return frozenset({ModelCapability.STREAMING, ModelCapability.JSON_OBJECT})

    async def stream(self, request):
        self.calls += 1
        yield ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED)
        yield ModelStreamEvent(
            type=ModelStreamEventType.TEXT_DELTA,
            text_delta=json.dumps(
                {
                    "type": "final",
                    "content": self.content,
                    "intent": {
                        "interpreted_intent": "Persist the requested decision",
                        "confidence": 0.95,
                        "candidates": [
                            {
                                "candidate_id": "persist",
                                "intent": "Persist the requested decision",
                                "confidence": 0.95,
                            }
                        ],
                        "ambiguity": 0.05,
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
            ),
        )
        yield ModelStreamEvent(
            type=ModelStreamEventType.RESPONSE_COMPLETED,
            finish_reason="stop",
            usage=ModelUsage(
                input_tokens=6,
                output_tokens=2,
                total_tokens=8,
                status="exact",
            ),
            provider_request_id="action-provider-request",
        )


class RejectingModelProvider(FinalModelProvider):
    async def stream(self, request):
        self.calls += 1
        raise ModelProviderError(
            "DUPLICATE_BILLING_ATTEMPT",
            "recovery must not call the model provider",
            retryable=False,
        )
        yield  # pragma: no cover


async def _seed_direct_run(database: Database) -> dict[str, UUID]:
    suffix = uuid4().hex[:10]
    async with database.admin_transaction() as session:
        tenant = Tenant(name=f"Action {suffix}", slug=f"action-{suffix}")
        other = Tenant(name=f"Action Other {suffix}", slug=f"action-other-{suffix}")
        session.add_all([tenant, other])
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Action Agent",
        )
        endpoint = ModelEndpoint(
            tenant_id=tenant.id,
            stable_key=f"action-{suffix}",
            revision=1,
            display_name="Action fake model",
            base_url="https://models.example/v1",
            credential_ref="env:NICO_MODEL_SECRET_ACTION_TEST",
            allowed_models=["action-model"],
            capabilities={"streaming": True, "json_object": True},
        )
        session.add_all([project, agent, endpoint])
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="assistant",
            mandate="Persist provider-neutral actions",
            runtime_provider="nico_native",
            execution_mode="direct",
            model_endpoint_id=endpoint.id,
            model_name="action-model",
            content_hash="d" * 64,
        )
        session.add(version)
        await session.flush()
        agent.current_version_id = version.id
        agent.status = "ready"
        task = Task(
            tenant_id=tenant.id,
            project_id=project.id,
            assignee_agent_id=agent.id,
            title="AgentAction persistence",
            input={"prompt": "persist this decision"},
            status="running",
            priority=2_147_483_647,
        )
        session.add(task)
        await session.flush()
        run = Run(
            tenant_id=tenant.id,
            task_id=task.id,
            agent_id=agent.id,
            agent_version_id=version.id,
            attempt=1,
        )
        session.add(run)
        await session.flush()
        return {
            "tenant": tenant.id,
            "other": other.id,
            "run": run.id,
        }


def _registry(provider: FinalModelProvider) -> RuntimeProviderRegistry:
    gateway = ModelGateway(ModelProviderRegistry([provider]), max_attempts=1)
    return RuntimeProviderRegistry([NicoNativeRuntimeProvider(gateway)])


def _hash(value) -> str:
    rendered = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(rendered.encode()).hexdigest()


@pytest.mark.asyncio
async def test_action_batch_is_tenant_scoped_immutable_and_publicly_redacted() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        ids = await _seed_direct_run(database)
        provider = FinalModelProvider("protected answer content")
        worker = RuntimeWorker(
            database,
            _registry(provider),
            worker_id=f"action-{uuid4().hex[:8]}",
            lease_seconds=10,
            heartbeat_seconds=1,
        )
        assert await worker.execute_once() is True
        assert provider.calls == 1

        async with database.admin_transaction() as session:
            batch = await session.scalar(
                select(AgentActionBatch).where(AgentActionBatch.run_id == ids["run"])
            )
            action = await session.scalar(
                select(AgentActionRecord).where(AgentActionRecord.run_id == ids["run"])
            )
            assert batch is not None and batch.action_count == 1
            assert batch.dispatch_cursor == 1 and batch.status == "completed"
            assert action is not None and action.kind == "final"
            assert action.status == "succeeded"
            assert action.outcome_ref == f"final:{action.action_id}"
            assert action.content_hash == _hash("protected answer content")
            assert action.intent_redacted["candidate_count"] == 1
            assert "interpreted_intent" not in action.intent_redacted
            action_id = action.id

        other_context = TenantContext(ids["other"], "other-action-reader", uuid4())
        async with database.tenant_transaction(other_context) as session:
            assert (
                await session.scalar(
                    select(AgentActionRecord).where(AgentActionRecord.id == action_id)
                )
                is None
            )

        with pytest.raises(DBAPIError):
            async with database.admin_transaction() as session:
                await session.execute(
                    text(
                        "UPDATE agent_actions SET intent_redacted = "
                        """'{"risk":"high"}'::jsonb WHERE id = :id"""
                    ),
                    {"id": action_id},
                )

        app = create_app(settings=settings, health_service=object(), database=database)
        headers = {
            "X-Tenant-ID": str(ids["tenant"]),
            "X-Actor-ID": "action-reader",
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                f"/api/v1/runs/{ids['run']}/agent-actions",
                headers=headers,
            )
        assert response.status_code == 200
        payload = response.json()
        assert len(payload) == 1
        encoded = json.dumps(payload)
        for protected in (
            "protected answer content",
            "request_redacted",
            "response_redacted",
            "credential_ref",
        ):
            assert protected not in encoded
        public_action = payload[0]["actions"][0]
        assert not {"content", "arguments", "question", "reason"}.intersection(public_action)
        assert payload[0]["actions"][0]["content_hash"] == _hash("protected answer content")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_multi_action_projection_is_atomic_and_retry_is_idempotent() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        ids = await _seed_direct_run(database)
        worker = RuntimeWorker(
            database,
            _registry(FinalModelProvider()),
            worker_id=f"action-base-{uuid4().hex[:8]}",
            lease_seconds=10,
            heartbeat_seconds=1,
        )
        assert await worker.execute_once() is True
        service = RuntimeExecutionService(database)

        async with database.admin_transaction() as session:
            run = await session.scalar(select(Run).where(Run.id == ids["run"]))
            runtime = await session.scalar(
                select(RuntimeSession).where(RuntimeSession.run_id == ids["run"])
            )
            context = await session.scalar(
                select(ContextSnapshot).where(ContextSnapshot.run_id == ids["run"])
            )
            assert run is not None and runtime is not None and context is not None
            step = RunStep(
                tenant_id=run.tenant_id,
                run_id=run.id,
                sequence=100,
                step_key="atomic-action-test",
                step_type="reasoning",
                kind="action.atomicity",
                status="running",
                started_at=datetime.now(UTC),
            )
            call = ModelCall(
                tenant_id=run.tenant_id,
                run_id=run.id,
                runtime_session_id=runtime.id,
                context_snapshot_id=context.id,
                call_key="model:atomic",
                provider="openai_compatible",
                model="action-model",
                status="completed",
                request_redacted={},
                request_hash="a" * 64,
                response_redacted={"tool_calls": []},
                response_hash="b" * 64,
                ended_at=datetime.now(UTC),
            )
            session.add_all([step, call])
            await session.flush()
            atomic_call_id = call.id

        batch = parse_agent_actions(
            ModelResponse(
                tool_calls=(
                    ModelToolCall(id="tool-a", name="file.read", arguments={"path": "a"}),
                    ModelToolCall(id="tool-b", name="file.read", arguments={"path": "b"}),
                )
            )
        )
        invalid_payload = batch.model_dump(mode="json")
        invalid_payload["actions"][1]["position"] = 0
        event = RuntimeEvent(
            sequence=100,
            type=RuntimeEventType.ACTION_BATCH_CREATED,
            payload={
                "call_key": "model:atomic",
                "run_step_key": "atomic-action-test",
                "parse_revision": 1,
                "batch": invalid_payload,
            },
        )
        with pytest.raises(ValueError, match="unique"):
            async with database.admin_transaction() as session:
                run = await session.scalar(select(Run).where(Run.id == ids["run"]))
                runtime = await session.scalar(
                    select(RuntimeSession).where(RuntimeSession.run_id == ids["run"])
                )
                assert run is not None and runtime is not None
                await service._project_agent_action_batch(session, run, runtime, event)

        async with database.admin_transaction() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AgentActionBatch)
                    .where(AgentActionBatch.model_call_id == atomic_call_id)
                )
                == 0
            )

        valid_event = event.model_copy(
            update={
                "payload": {
                    **event.payload,
                    "batch": batch.model_dump(mode="json"),
                }
            }
        )
        for _ in range(2):
            async with database.admin_transaction() as session:
                run = await session.scalar(select(Run).where(Run.id == ids["run"]))
                runtime = await session.scalar(
                    select(RuntimeSession).where(RuntimeSession.run_id == ids["run"])
                )
                assert run is not None and runtime is not None
                await service._project_agent_action_batch(
                    session,
                    run,
                    runtime,
                    valid_event,
                )

        async with database.admin_transaction() as session:
            persisted_batch = await session.scalar(
                select(AgentActionBatch).where(AgentActionBatch.model_call_id == atomic_call_id)
            )
            assert persisted_batch is not None
            actions = list(
                await session.scalars(
                    select(AgentActionRecord)
                    .where(AgentActionRecord.batch_id == persisted_batch.id)
                    .order_by(AgentActionRecord.ordinal)
                )
            )
            assert [action.action_id for action in actions] == [
                action.action_id for action in batch.actions
            ]
            assert [action.ordinal for action in actions] == [0, 1]

        ask_payload = {
            "type": "ask_user",
            "question": "Which exact target should be changed?",
            "reason": "Two equally plausible high-risk targets remain.",
            "intent": {
                "interpreted_intent": "Change one of two protected targets",
                "confidence": 0.5,
                "candidates": [
                    {
                        "candidate_id": "target-a",
                        "intent": "Change target A",
                        "confidence": 0.5,
                    },
                    {
                        "candidate_id": "target-b",
                        "intent": "Change target B",
                        "confidence": 0.5,
                    },
                ],
                "ambiguity": 1.0,
                "risk": "high",
                "risk_reasons": ["The change is difficult to reverse."],
                "missing_information": ["Exact target identifier"],
                "safe_partial_answer_possible": False,
            },
        }
        ask_batch = parse_agent_actions(
            ModelResponse(
                text=json.dumps(ask_payload),
                response_format_type="json_schema",
            )
        )
        async with database.admin_transaction() as session:
            run = await session.scalar(select(Run).where(Run.id == ids["run"]))
            runtime = await session.scalar(
                select(RuntimeSession).where(RuntimeSession.run_id == ids["run"])
            )
            context = await session.scalar(
                select(ContextSnapshot).where(ContextSnapshot.run_id == ids["run"])
            )
            assert run is not None and runtime is not None and context is not None
            ask_step = RunStep(
                tenant_id=run.tenant_id,
                run_id=run.id,
                sequence=101,
                step_key="ask-action-test",
                step_type="reasoning",
                kind="action.ask",
                status="running",
                started_at=datetime.now(UTC),
            )
            ask_call = ModelCall(
                tenant_id=run.tenant_id,
                run_id=run.id,
                runtime_session_id=runtime.id,
                context_snapshot_id=context.id,
                call_key="model:ask",
                provider="openai_compatible",
                model="action-model",
                status="completed",
                request_redacted={},
                request_hash="c" * 64,
                response_redacted={"content": json.dumps(ask_payload)},
                response_hash="d" * 64,
                ended_at=datetime.now(UTC),
            )
            session.add_all([ask_step, ask_call])
        ask_event = RuntimeEvent(
            sequence=101,
            type=RuntimeEventType.ACTION_BATCH_CREATED,
            payload={
                "call_key": "model:ask",
                "run_step_key": "ask-action-test",
                "parse_revision": 1,
                "batch": ask_batch.model_dump(mode="json"),
            },
        )
        for _ in range(2):
            async with database.admin_transaction() as session:
                run = await session.scalar(select(Run).where(Run.id == ids["run"]))
                runtime = await session.scalar(
                    select(RuntimeSession).where(RuntimeSession.run_id == ids["run"])
                )
                assert run is not None and runtime is not None
                await service._project_agent_action_batch(
                    session,
                    run,
                    runtime,
                    ask_event,
                )
        async with database.admin_transaction() as session:
            ask_record = await session.scalar(
                select(AgentActionRecord).where(
                    AgentActionRecord.run_id == ids["run"],
                    AgentActionRecord.kind == "ask_user",
                )
            )
            assert ask_record is not None
            assert ask_record.action_id == ask_batch.actions[0].action_id
            assert ask_record.question_hash == _hash(ask_payload["question"])
            assert ask_record.reason_hash == _hash(ask_payload["reason"])
            assert ask_record.intent_redacted["missing_information_count"] == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_model_call_gap_repairs_once_without_duplicate_billing() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        ids = await _seed_direct_run(database)
        first_provider = FinalModelProvider("recover from committed response")
        first_registry = _registry(first_provider)
        service = RuntimeExecutionService(database)
        old_claim = await database.claim_next_run("crashed-action-worker", 5)
        assert old_claim is not None and old_claim.run_id == ids["run"]
        prepared = await service.prepare_claim(
            old_claim,
            worker_id="crashed-action-worker",
            registry=first_registry,
        )
        native = first_registry.get("nico_native")
        handle = await native.create_session(prepared.request)
        await service.bind_session(
            prepared,
            worker_id="crashed-action-worker",
            handle=handle,
        )
        await native.execute(
            handle.external_session_id,
            prepared.request,
            RuntimeServices(),
        )
        events = [event async for event in native.stream_events(handle.external_session_id)]
        terminal_index = next(
            index
            for index, event in enumerate(events)
            if event.type is RuntimeEventType.MODEL_CALL_COMPLETED
        )
        assert events[terminal_index + 1].type is RuntimeEventType.ACTION_BATCH_CREATED
        for event in events[: terminal_index + 1]:
            await service.record_event(
                prepared,
                worker_id="crashed-action-worker",
                event=event,
            )
        assert first_provider.calls == 1

        async with database.admin_transaction() as session:
            await session.execute(
                text("UPDATE runs SET lease_expires_at = :expired WHERE id = :run_id"),
                {
                    "expired": datetime.now(UTC) - timedelta(seconds=1),
                    "run_id": ids["run"],
                },
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AgentActionBatch)
                    .where(AgentActionBatch.run_id == ids["run"])
                )
                == 0
            )

        rejecting = RejectingModelProvider()
        replacement = RuntimeWorker(
            database,
            _registry(rejecting),
            worker_id=f"action-recovery-{uuid4().hex[:8]}",
            lease_seconds=5,
            heartbeat_seconds=0.05,
        )
        assert await replacement.execute_once() is True
        assert rejecting.calls == 0

        async with database.admin_transaction() as session:
            calls = list(
                await session.scalars(select(ModelCall).where(ModelCall.run_id == ids["run"]))
            )
            repairs = list(
                await session.scalars(
                    select(AgentActionRepair).where(AgentActionRepair.run_id == ids["run"])
                )
            )
            batches = list(
                await session.scalars(
                    select(AgentActionBatch).where(AgentActionBatch.run_id == ids["run"])
                )
            )
            run = await session.scalar(select(Run).where(Run.id == ids["run"]))
            assert len(calls) == 1
            assert calls[0].provider_request_id == "action-provider-request"
            assert len(batches) == 1
            assert len(repairs) == 1
            assert repairs[0].kind == "post_model_call_commit"
            assert repairs[0].result_batch_id == batches[0].id
            assert run is not None and run.status == "completed"
            assert run.result == {"content": "recover from committed response"}
    finally:
        await engine.dispose()
