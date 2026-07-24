from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import Settings
from nico_agent.database import Database, RunClaim
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
from nico_agent.models.contracts import ModelResponse
from nico_agent.runtime.completion_gate import CompletionGateFacts, evaluate_final_action
from nico_agent.runtime.contracts import (
    RuntimeActionOutcome,
    RuntimeEvent,
    RuntimeEventType,
)
from nico_agent.runtime.native.action_dispatcher import AgentActionDispatcher
from nico_agent.runtime.native.action_parser import parse_agent_actions
from nico_agent.runtime.service import RuntimeExecutionService
from nico_agent.runtime.tools import RunActionHandler

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


def _final_batch(*, answered: bool, requires_user: bool, content: str):
    return parse_agent_actions(
        ModelResponse(
            text=json.dumps(
                {
                    "type": "final",
                    "content": content,
                    "intent": {
                        "interpreted_intent": "Explain the platform timestamp",
                        "confidence": 0.95,
                        "candidates": [
                            {
                                "candidate_id": "timestamp",
                                "intent": "Explain the platform timestamp",
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
                        "answered_user_intent": answered,
                        "requires_user_response": requires_user,
                    },
                }
            ),
            response_format_type="json_schema",
        )
    )


async def _seed_leased_direct_run(database: Database) -> tuple[RunClaim, str]:
    suffix = uuid4().hex[:10]
    worker_id = f"completion-worker-{suffix}"
    lease_token = uuid4()
    async with database.admin_transaction() as session:
        tenant = Tenant(name=f"Completion {suffix}", slug=f"completion-{suffix}")
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Completion Agent",
        )
        endpoint = ModelEndpoint(
            tenant_id=tenant.id,
            stable_key=f"completion-{suffix}",
            revision=1,
            display_name="Completion fake model",
            base_url="https://models.example/v1",
            credential_ref="env:NICO_MODEL_SECRET_COMPLETION_TEST",
            allowed_models=["completion-model"],
            capabilities={"streaming": True, "json_schema": True},
        )
        session.add_all([project, agent, endpoint])
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="assistant",
            mandate="Complete only after answering the resolved intent",
            runtime_provider="nico_native",
            execution_mode="direct",
            model_endpoint_id=endpoint.id,
            model_name="completion-model",
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
            title="Completion persistence",
            input={"prompt": "你平台是怎么提供de"},
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
            status="running",
            lease_owner=worker_id,
            lease_token=lease_token,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
            started_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()
        runtime = RuntimeSession(
            tenant_id=run.tenant_id,
            run_id=run.id,
            provider_name="nico_native",
            provider_version="0.2.0",
            protocol_version="2.0",
            status="running",
            execution_mode="direct",
        )
        session.add(runtime)
        await session.flush()
        context = ContextSnapshot(
            tenant_id=run.tenant_id,
            run_id=run.id,
            runtime_session_id=runtime.id,
            version=1,
            reason="completion-test",
            content_hash="a" * 64,
        )
        session.add(context)
        await session.flush()
        runtime.current_context_snapshot_id = context.id
        for sequence, call_key in enumerate(
            ("model:1", "completion-gate-correction:direct:1"),
            start=1,
        ):
            session.add_all(
                [
                    RunStep(
                        tenant_id=run.tenant_id,
                        run_id=run.id,
                        sequence=sequence,
                        step_key=call_key,
                        step_type="reasoning",
                        iteration=sequence,
                        kind="completion.test",
                        status="completed",
                        started_at=datetime.now(UTC),
                        ended_at=datetime.now(UTC),
                    ),
                    ModelCall(
                        tenant_id=run.tenant_id,
                        run_id=run.id,
                        runtime_session_id=runtime.id,
                        context_snapshot_id=context.id,
                        call_key=call_key,
                        provider="openai_compatible",
                        model="test",
                        status="completed",
                        request_hash=str(sequence) * 64,
                        response_hash=str(sequence + 2) * 64,
                        ended_at=datetime.now(UTC),
                    ),
                ]
            )
        return (
            RunClaim(
                run_id=run.id,
                tenant_id=run.tenant_id,
                lease_token=lease_token,
                previous_status="pending",
            ),
            worker_id,
        )


@pytest.mark.asyncio
async def test_completion_gate_verdict_and_correction_are_durable_and_idempotent() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        claim, worker_id = await _seed_leased_direct_run(database)
        source = _final_batch(
            answered=False,
            requires_user=True,
            content="I still need more information.",
        )
        corrected = _final_batch(
            answered=True,
            requires_user=False,
            content="The platform supplies the timestamp in the Run context.",
        )
        source_verdict = evaluate_final_action(
            source.actions[0],
            CompletionGateFacts(pending_user_input_count=1),
        )
        corrected_verdict = evaluate_final_action(
            corrected.actions[0],
            CompletionGateFacts(),
        )
        assert source_verdict.accepted is False
        assert source_verdict.reason_code == "PENDING_USER_INPUT"
        assert corrected_verdict.accepted is True

        service = RuntimeExecutionService(database)
        source_event = RuntimeEvent(
            sequence=1,
            type=RuntimeEventType.ACTION_BATCH_CREATED,
            payload={
                "call_key": "model:1",
                "run_step_key": "model:1",
                "parse_revision": 1,
                "batch": source.model_dump(mode="json"),
            },
        )
        corrected_event = RuntimeEvent(
            sequence=2,
            type=RuntimeEventType.ACTION_BATCH_CREATED,
            payload={
                "call_key": "completion-gate-correction:direct:1",
                "run_step_key": "completion-gate-correction:direct:1",
                "parse_revision": 1,
                "batch": corrected.model_dump(mode="json"),
                "repair": {
                    "kind": "completion_gate_correction",
                    "ordinal": 1,
                    "reason_code": source_verdict.reason_code,
                    "observation_ref": f"agent_action_batch:{source.content_hash}",
                    "source_batch_key": source.content_hash,
                },
            },
        )
        for event in (source_event, corrected_event, corrected_event):
            async with database.admin_transaction() as session:
                run = await session.scalar(select(Run).where(Run.id == claim.run_id))
                runtime = await session.scalar(
                    select(RuntimeSession).where(RuntimeSession.run_id == claim.run_id)
                )
                assert run is not None and runtime is not None
                await service._project_agent_action_batch(session, run, runtime, event)

        handler = RunActionHandler(service, claim, worker_id=worker_id)

        async def block_invalid(action, _ordinal):
            return RuntimeActionOutcome(
                status="blocked",
                outcome_ref=f"completion_gate:{source_verdict.reason_code}",
                observation_ref=f"completion_gate:{action.action_id}",
                value=source_verdict.corrective_observation(),
            )

        async def accept_final(action, _ordinal):
            return RuntimeActionOutcome(
                status="succeeded",
                outcome_ref=f"final:{action.action_id}",
                observation_ref="model_call:completion-gate-correction:direct:1",
                value={"content": action.content},
            )

        blocked = await AgentActionDispatcher(handler).dispatch(source, block_invalid)
        accepted = await AgentActionDispatcher(handler).dispatch(corrected, accept_final)
        assert blocked.completed is False
        assert blocked.terminal_outcome is not None
        assert blocked.terminal_outcome.status == "blocked"
        assert accepted.completed is True

        async with database.admin_transaction() as session:
            batches = list(
                await session.scalars(
                    select(AgentActionBatch)
                    .where(AgentActionBatch.run_id == claim.run_id)
                    .order_by(AgentActionBatch.created_at)
                )
            )
            actions = list(
                await session.scalars(
                    select(AgentActionRecord)
                    .where(AgentActionRecord.run_id == claim.run_id)
                    .order_by(AgentActionRecord.created_at)
                )
            )
            repair = await session.scalar(
                select(AgentActionRepair).where(AgentActionRepair.run_id == claim.run_id)
            )
            repair_count = await session.scalar(
                select(func.count())
                .select_from(AgentActionRepair)
                .where(AgentActionRepair.run_id == claim.run_id)
            )
            assert [batch.status for batch in batches] == ["failed", "completed"]
            assert [action.status for action in actions] == ["blocked", "succeeded"]
            assert actions[0].completion == {
                "answered_user_intent": False,
                "requires_user_response": True,
            }
            assert actions[0].outcome_ref == "completion_gate:PENDING_USER_INPUT"
            assert repair_count == 1
            assert repair is not None
            assert repair.kind == "completion_gate_correction"
            assert repair.source_batch_id == batches[0].id
            assert repair.result_batch_id == batches[1].id
    finally:
        await engine.dispose()
