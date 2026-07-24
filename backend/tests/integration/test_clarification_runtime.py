from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import Settings
from nico_agent.database import Database
from nico_agent.domain.models import (
    Agent,
    AgentActionBatch,
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
from nico_agent.runtime.contracts import RuntimeEvent, RuntimeEventType
from nico_agent.runtime.native.action_parser import parse_agent_actions
from nico_agent.runtime.service import RuntimeExecutionService

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


def _ask_batch():
    return parse_agent_actions(
        ModelResponse(
            text=json.dumps(
                {
                    "type": "ask_user",
                    "question": "Did you mean the platform timestamp?",
                    "reason": "The input contains a typo.",
                    "intent": {
                        "interpreted_intent": "Explain how the platform supplies the timestamp",
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
                }
            ),
            structured_output=True,
        )
    )


def _final_batch():
    return parse_agent_actions(
        ModelResponse(
            text=json.dumps(
                {
                    "type": "final",
                    "content": "The platform supplies the timestamp in the Run context.",
                    "intent": {
                        "interpreted_intent": "Explain how the platform supplies the timestamp",
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
                        "answered_user_intent": True,
                        "requires_user_response": False,
                    },
                }
            ),
            structured_output=True,
        )
    )


async def _seed_direct_run(database: Database) -> dict[str, object]:
    suffix = uuid4().hex[:10]
    async with database.admin_transaction() as session:
        tenant = Tenant(name=f"Clarification {suffix}", slug=f"clarification-{suffix}")
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Clarification Agent",
        )
        endpoint = ModelEndpoint(
            tenant_id=tenant.id,
            stable_key=f"clarification-{suffix}",
            revision=1,
            display_name="Clarification fake model",
            base_url="https://models.example/v1",
            credential_ref="env:NICO_MODEL_SECRET_CLARIFICATION_TEST",
            allowed_models=["clarification-model"],
            capabilities={"streaming": True, "structured_output": True},
        )
        session.add_all([project, agent, endpoint])
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="assistant",
            mandate="Ask only when indispensable",
            runtime_provider="nico_native",
            execution_mode="direct",
            model_endpoint_id=endpoint.id,
            model_name="clarification-model",
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
            title="Clarification persistence",
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
            status="completed",
            result={"content": "persistence fixture"},
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()
        return {"run": run.id}


@pytest.mark.asyncio
async def test_clarification_correction_relation_is_source_linked_and_idempotent() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        ids = await _seed_direct_run(database)
        source = _ask_batch()
        result = _final_batch()
        async with database.admin_transaction() as session:
            run = await session.scalar(select(Run).where(Run.id == ids["run"]))
            assert run is not None
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
                reason="clarification-test",
                content_hash="a" * 64,
            )
            session.add(context)
            await session.flush()
            runtime.current_context_snapshot_id = context.id
            for sequence, call_key in enumerate(
                ("model:1", "clarification-correction:direct:1"),
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
                            kind="clarification.test",
                            status="completed",
                            started_at=datetime.now(UTC),
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

        service = RuntimeExecutionService(database)
        events = (
            RuntimeEvent(
                sequence=1,
                type=RuntimeEventType.ACTION_BATCH_CREATED,
                payload={
                    "call_key": "model:1",
                    "run_step_key": "model:1",
                    "parse_revision": 1,
                    "batch": source.model_dump(mode="json"),
                },
            ),
            RuntimeEvent(
                sequence=2,
                type=RuntimeEventType.ACTION_BATCH_CREATED,
                payload={
                    "call_key": "clarification-correction:direct:1",
                    "run_step_key": "clarification-correction:direct:1",
                    "parse_revision": 1,
                    "batch": result.model_dump(mode="json"),
                    "repair": {
                        "kind": "clarification_correction",
                        "ordinal": 1,
                        "reason_code": "SAFE_PARTIAL_ANSWER_AVAILABLE",
                        "observation_ref": f"agent_action_batch:{source.content_hash}",
                        "source_batch_key": source.content_hash,
                    },
                },
            ),
        )
        for event in (events[0], events[1], events[1]):
            async with database.admin_transaction() as session:
                run = await session.scalar(select(Run).where(Run.id == ids["run"]))
                runtime = await session.scalar(
                    select(RuntimeSession).where(RuntimeSession.run_id == ids["run"])
                )
                assert run is not None and runtime is not None
                await service._project_agent_action_batch(session, run, runtime, event)

        async with database.admin_transaction() as session:
            batches = list(
                await session.scalars(
                    select(AgentActionBatch)
                    .where(AgentActionBatch.run_id == ids["run"])
                    .order_by(AgentActionBatch.created_at)
                )
            )
            repair = await session.scalar(
                select(AgentActionRepair).where(AgentActionRepair.run_id == ids["run"])
            )
            repair_count = await session.scalar(
                select(func.count())
                .select_from(AgentActionRepair)
                .where(AgentActionRepair.run_id == ids["run"])
            )
            assert len(batches) == 2
            assert repair_count == 1
            assert repair is not None
            assert repair.kind == "clarification_correction"
            assert repair.source_batch_id == batches[0].id
            assert repair.result_batch_id == batches[1].id
    finally:
        await engine.dispose()
