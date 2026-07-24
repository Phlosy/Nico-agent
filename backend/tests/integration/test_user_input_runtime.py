from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import Settings
from nico_agent.database import Database, RunClaim, TenantContext
from nico_agent.domain.errors import DomainConflict, ResourceNotFound
from nico_agent.domain.models import (
    Agent,
    AgentActionBatch,
    AgentActionRecord,
    AgentVersion,
    AuditRecord,
    ContextSnapshot,
    Event,
    ModelCall,
    Project,
    Run,
    RunStep,
    RuntimeSession,
    Task,
    Tenant,
    ToolCall,
    UserInputRequest,
)
from nico_agent.domain.states import RunStatus
from nico_agent.models.contracts import ModelResponse
from nico_agent.runtime.contracts import RuntimeLoopState, RuntimeSessionStatus, RuntimeTrajectory
from nico_agent.runtime.lifecycle import RunLifecycleAuthority
from nico_agent.runtime.native.action_parser import parse_agent_actions
from nico_agent.runtime.service import RuntimeExecutionService
from nico_agent.runtime.tools import RunActionHandler
from nico_agent.user_inputs.contracts import RuntimeUserInputIntent, UserInputAnswer
from nico_agent.user_inputs.service import RunUserInputHandler, UserInputService

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def _ask_batch():
    return parse_agent_actions(
        ModelResponse(
            text=json.dumps(
                {
                    "type": "ask_user",
                    "question": "Which exact object should be deleted?",
                    "reason": "File, database, and Run targets are equally plausible.",
                    "intent": {
                        "interpreted_intent": "Delete the previously referenced object",
                        "confidence": 0.55,
                        "candidates": [
                            {
                                "candidate_id": "file",
                                "intent": "Delete the file",
                                "confidence": 0.55,
                            },
                            {
                                "candidate_id": "database",
                                "intent": "Delete the database",
                                "confidence": 0.54,
                            },
                        ],
                        "ambiguity": 0.9,
                        "risk": "high",
                        "risk_reasons": ["Deletion is irreversible."],
                        "missing_information": ["Exact target"],
                        "safe_partial_answer_possible": False,
                    },
                }
            ),
            response_format_type="json_schema",
        )
    )


async def _seed_ask(
    database: Database,
    *,
    worker_id: str,
) -> tuple[RunClaim, object]:
    batch_dto = _ask_batch()
    action_dto = batch_dto.actions[0]
    suffix = uuid4().hex[:10]
    lease_token = uuid4()
    now = datetime.now(UTC)
    async with database.admin_transaction() as session:
        tenant = Tenant(name=f"User input {suffix}", slug=f"user-input-{suffix}")
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="User Input Agent",
        )
        session.add_all([project, agent])
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="assistant",
            mandate="Ask only when indispensable",
            runtime_provider="nico_native",
            execution_mode="react",
            content_hash="a" * 64,
        )
        session.add(version)
        await session.flush()
        agent.current_version_id = version.id
        agent.status = "ready"
        task = Task(
            tenant_id=tenant.id,
            project_id=project.id,
            assignee_agent_id=agent.id,
            title="High-risk deletion",
            input={"prompt": "delete that"},
            status="running",
        )
        session.add(task)
        await session.flush()
        run = Run(
            tenant_id=tenant.id,
            task_id=task.id,
            agent_id=agent.id,
            agent_version_id=version.id,
            attempt=1,
            status=RunStatus.RUNNING.value,
            lease_owner=worker_id,
            lease_token=lease_token,
            lease_expires_at=now + timedelta(minutes=5),
            heartbeat_at=now,
        )
        session.add(run)
        await session.flush()
        runtime = RuntimeSession(
            tenant_id=tenant.id,
            run_id=run.id,
            provider_name="nico_native",
            provider_version="0.2.0",
            protocol_version="2.0",
            status="running",
            execution_mode="react",
        )
        session.add(runtime)
        await session.flush()
        context = ContextSnapshot(
            tenant_id=tenant.id,
            run_id=run.id,
            runtime_session_id=runtime.id,
            version=1,
            reason="user-input-test",
            content_hash="b" * 64,
        )
        session.add(context)
        await session.flush()
        call = ModelCall(
            tenant_id=tenant.id,
            run_id=run.id,
            runtime_session_id=runtime.id,
            context_snapshot_id=context.id,
            call_key="model:1",
            provider="openai_compatible",
            model="test",
            status="completed",
            request_hash="c" * 64,
            response_hash="d" * 64,
        )
        step = RunStep(
            tenant_id=tenant.id,
            run_id=run.id,
            sequence=1,
            step_key="reasoning:1",
            step_type="reasoning",
            iteration=1,
            kind="react.reasoning",
            status="completed",
        )
        session.add_all([call, step])
        await session.flush()
        runtime.current_context_snapshot_id = context.id
        runtime.last_model_call_id = call.id
        batch = AgentActionBatch(
            tenant_id=tenant.id,
            run_id=run.id,
            runtime_session_id=runtime.id,
            context_snapshot_id=context.id,
            model_call_id=call.id,
            run_step_id=step.id,
            batch_key=batch_dto.content_hash,
            source_format=batch_dto.source_format.value,
            response_hash=call.response_hash,
            action_count=1,
        )
        session.add(batch)
        await session.flush()
        action = AgentActionRecord(
            tenant_id=tenant.id,
            run_id=run.id,
            batch_id=batch.id,
            ordinal=0,
            action_id=action_dto.action_id,
            kind=action_dto.kind,
            intent_redacted=action_dto.intent.model_dump(mode="json"),
            question_hash=_hash(action_dto.question),
            reason_hash=_hash(action_dto.reason),
        )
        session.add(action)
        await session.flush()
        claim = RunClaim(
            run_id=run.id,
            tenant_id=tenant.id,
            lease_token=lease_token,
            previous_status=RunStatus.RUNNING.value,
        )
    return claim, batch_dto


async def _request_and_suspend(
    database: Database,
    *,
    worker_id: str,
    schema: dict,
    ttl_seconds: int = 60,
):
    claim, batch = await _seed_ask(database, worker_id=worker_id)
    runtime_service = RuntimeExecutionService(database)
    action_handler = RunActionHandler(runtime_service, claim, worker_id=worker_id)
    await action_handler.begin_action(
        batch.content_hash,
        ordinal=0,
        action_id=batch.actions[0].action_id,
    )
    checkpoint = {
        "schema_version": 2,
        "execution_mode": "react",
        "loop_state": "waiting_for_user_input",
        "pending_user_input": {"action_id": batch.actions[0].action_id},
    }
    handler = RunUserInputHandler(
        UserInputService(database),
        claim,
        worker_id=worker_id,
    )
    intent = RuntimeUserInputIntent(
        batch_key=batch.content_hash,
        action_id=batch.actions[0].action_id,
        ordinal=0,
        question=batch.actions[0].question,
        reason=batch.actions[0].reason,
        input_schema=schema,
        checkpoint=checkpoint,
        ttl_seconds=ttl_seconds,
        idempotency_key=f"ask:{batch.actions[0].action_id}",
    )
    request = await handler.request_user_input(intent)
    retry = await handler.request_user_input(intent)
    assert retry.id == request.id
    assert retry.revision == request.revision
    suspended = await runtime_service.suspend_claim(
        SimpleNamespace(claim=claim),
        worker_id=worker_id,
        checkpoint=checkpoint,
        wake_condition={"type": "user_input", "request_id": str(request.id)},
        usage={},
        trajectory=RuntimeTrajectory(
            provider="nico_native",
            provider_version="0.2.0",
            external_session_id="user-input-test",
            status=RuntimeSessionStatus.SUSPENDED,
            events=[],
        ),
    )
    assert suspended is True
    return claim, batch, request, checkpoint


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schema", "answer"),
    [
        ({"type": "string", "minLength": 1}, "SQLite"),
        ({"type": "string", "enum": ["file", "database", "run"]}, "database"),
        (
            {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "confirmed": {"const": True},
                },
                "required": ["target", "confirmed"],
                "additionalProperties": False,
            },
            {"target": "run-7", "confirmed": True},
        ),
    ],
)
async def test_answer_resumes_same_action_once_and_keeps_payload_protected(
    schema: dict,
    answer: object,
) -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    worker_id = f"user-input-{uuid4().hex[:8]}"
    secret = answer
    try:
        claim, batch, request, checkpoint = await _request_and_suspend(
            database,
            worker_id=worker_id,
            schema=schema,
        )
        context = TenantContext(claim.tenant_id, "operator:test", uuid4())
        service = UserInputService(database)
        result = await service.answer(
            context,
            request.id,
            UserInputAnswer(expected_revision=1, answer=answer),
            idempotency_key="answer-once",
        )
        replay = await service.answer(
            context,
            request.id,
            UserInputAnswer(expected_revision=1, answer=answer),
            idempotency_key="answer-once",
        )
        assert result == replay
        assert result.status == "answered"
        assert result.revision == 2
        assert not hasattr(result, "answer")

        async with database.admin_transaction() as session:
            run = await session.scalar(select(Run).where(Run.id == claim.run_id))
            persisted = await session.scalar(
                select(UserInputRequest).where(UserInputRequest.id == request.id)
            )
            action = await session.scalar(
                select(AgentActionRecord).where(
                    AgentActionRecord.run_id == claim.run_id,
                    AgentActionRecord.action_id == batch.actions[0].action_id,
                )
            )
            action_batch = await session.scalar(
                select(AgentActionBatch).where(
                    AgentActionBatch.run_id == claim.run_id,
                    AgentActionBatch.batch_key == batch.content_hash,
                )
            )
            tool_count = await session.scalar(
                select(func.count()).select_from(ToolCall).where(ToolCall.run_id == claim.run_id)
            )
            records = [
                *list(await session.scalars(select(Event).where(Event.run_id == claim.run_id))),
                *list(
                    await session.scalars(
                        select(AuditRecord).where(AuditRecord.resource_id == request.id)
                    )
                ),
            ]
            outcome = await RuntimeExecutionService._resolved_agent_action_outcome(
                session,
                run,
                action,
            )
            assert run.status == RunStatus.RUNNING.value
            assert run.lease_owner is None
            assert run.checkpoint == checkpoint
            assert persisted.answer_payload == answer
            assert action.status == "succeeded"
            assert action_batch.status == "completed"
            assert action_batch.dispatch_cursor == 1
            assert tool_count == 0
            assert outcome is not None
            assert outcome.value["trust"] == "untrusted"
            assert outcome.value["answer"] == answer
            public_records = json.dumps(
                [
                    getattr(item, "payload", None) or getattr(item, "details", None)
                    for item in records
                ],
                sort_keys=True,
                ensure_ascii=False,
            )
            assert json.dumps(secret, ensure_ascii=False) not in public_records
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_invalid_cross_tenant_and_conflicting_answers_do_not_wake() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        claim, _, request, _ = await _request_and_suspend(
            database,
            worker_id=f"user-input-{uuid4().hex[:8]}",
            schema={"type": "string", "enum": ["file", "database"]},
        )
        service = UserInputService(database)
        context = TenantContext(claim.tenant_id, "operator:test", uuid4())
        with pytest.raises(ValueError, match="does not match"):
            await service.answer(
                context,
                request.id,
                UserInputAnswer(expected_revision=1, answer="run"),
                idempotency_key="invalid",
            )
        with pytest.raises(ResourceNotFound):
            await service.answer(
                TenantContext(uuid4(), "operator:other", uuid4()),
                request.id,
                UserInputAnswer(expected_revision=1, answer="file"),
                idempotency_key="cross-tenant",
            )
        async with database.admin_transaction() as session:
            run = await session.scalar(select(Run).where(Run.id == claim.run_id))
            assert run.status == RunStatus.WAITING_FOR_USER_INPUT.value

        await service.answer(
            context,
            request.id,
            UserInputAnswer(expected_revision=1, answer="file"),
            idempotency_key="valid",
        )
        with pytest.raises(DomainConflict) as captured:
            await service.answer(
                context,
                request.id,
                UserInputAnswer(expected_revision=1, answer="database"),
                idempotency_key="conflict",
            )
        assert captured.value.code == "USER_INPUT_ALREADY_RESOLVED"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_expiry_reconciler_wakes_once_without_repeating_action() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        claim, _, request, _ = await _request_and_suspend(
            database,
            worker_id=f"user-input-{uuid4().hex[:8]}",
            schema={"type": "string"},
            ttl_seconds=1,
        )
        await asyncio.sleep(1.05)
        assert await database.reconcile_user_input_requests() == 1
        assert await database.reconcile_user_input_requests() == 0
        async with database.admin_transaction() as session:
            run = await session.scalar(select(Run).where(Run.id == claim.run_id))
            persisted = await session.scalar(
                select(UserInputRequest).where(UserInputRequest.id == request.id)
            )
            action = await session.scalar(
                select(AgentActionRecord).where(AgentActionRecord.id == persisted.agent_action_id)
            )
            assert run.status == RunStatus.RUNNING.value
            assert persisted.status == "expired"
            assert action.status == "blocked"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_run_cancellation_prevents_late_answer_resurrection() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        claim, _, request, _ = await _request_and_suspend(
            database,
            worker_id=f"user-input-{uuid4().hex[:8]}",
            schema={"type": "string"},
        )
        context = TenantContext(claim.tenant_id, "operator:cancel", uuid4())
        async with database.tenant_transaction(context) as session:
            run = await session.scalar(
                select(Run)
                .where(Run.tenant_id == claim.tenant_id, Run.id == claim.run_id)
                .with_for_update()
            )
            await RunLifecycleAuthority.transition(
                session,
                context,
                run,
                target=RunStatus.CANCELLED,
                reason="operator_cancelled",
                loop_state=RuntimeLoopState.CANCELLED,
                event_type="RunCancelled",
            )
        with pytest.raises(DomainConflict):
            await UserInputService(database).answer(
                context,
                request.id,
                UserInputAnswer(expected_revision=1, answer="late"),
                idempotency_key="late-answer",
            )
        async with database.admin_transaction() as session:
            run = await session.scalar(select(Run).where(Run.id == claim.run_id))
            persisted = await session.scalar(
                select(UserInputRequest).where(UserInputRequest.id == request.id)
            )
            assert run.status == RunStatus.CANCELLED.value
            assert persisted.status == "cancelled"
            assert persisted.answer_payload is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_answered_but_unwoken_request_is_reconciled_once() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        claim, _, runtime_request, _ = await _request_and_suspend(
            database,
            worker_id=f"user-input-{uuid4().hex[:8]}",
            schema={"type": "string"},
        )
        answer = "protected-after-crash"
        now = datetime.now(UTC)
        async with database.admin_transaction() as session:
            request = await session.scalar(
                select(UserInputRequest)
                .where(UserInputRequest.id == runtime_request.id)
                .with_for_update()
            )
            action = await session.scalar(
                select(AgentActionRecord)
                .where(AgentActionRecord.id == request.agent_action_id)
                .with_for_update()
            )
            batch = await session.scalar(
                select(AgentActionBatch)
                .where(AgentActionBatch.id == request.action_batch_id)
                .with_for_update()
            )
            request.status = "answered"
            request.answer_payload = answer
            request.answer_hash = _hash(answer)
            request.answer_ref = f"user_input:{request.id}:answer"
            request.answer_idempotency_key = "answer-before-crash"
            request.answered_by = "operator:test"
            request.answered_at = now
            request.resolved_at = now
            request.revision += 1
            action.status = "succeeded"
            action.outcome_ref = f"user_input:{request.id}"
            action.observation_ref = request.answer_ref
            action.completed_at = now
            action.revision += 1
            batch.status = "completed"
            batch.dispatch_cursor = 1
            batch.completed_at = now
            batch.revision += 1

        assert await database.reconcile_user_input_requests() == 1
        assert await database.reconcile_user_input_requests() == 0
        async with database.admin_transaction() as session:
            run = await session.scalar(select(Run).where(Run.id == claim.run_id))
            wake_count = await session.scalar(
                select(func.count())
                .select_from(Event)
                .where(
                    Event.run_id == claim.run_id,
                    Event.event_type == "RunWoken",
                )
            )
            runtime_session = await session.scalar(
                select(RuntimeSession).where(RuntimeSession.run_id == claim.run_id)
            )
            assert run.status == RunStatus.RUNNING.value
            assert wake_count == 1
            assert runtime_session.status == "paused"
            assert answer not in json.dumps(
                runtime_session.provider_state,
                ensure_ascii=False,
            )
    finally:
        await engine.dispose()
