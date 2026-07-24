from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import Settings
from nico_agent.database import Database, RunClaim
from nico_agent.domain.models import (
    Agent,
    AgentActionBatch,
    AgentActionRecord,
    AgentVersion,
    ContextSnapshot,
    ModelCall,
    Project,
    Run,
    RunStep,
    RuntimeSession,
    Task,
    Tenant,
)
from nico_agent.models.contracts import ModelResponse, ModelToolCall
from nico_agent.runtime.contracts import RuntimeActionOutcome
from nico_agent.runtime.native.action_dispatcher import AgentActionDispatcher
from nico_agent.runtime.native.action_parser import parse_agent_actions
from nico_agent.runtime.service import RuntimeExecutionService
from nico_agent.runtime.tools import RunActionHandler

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


def _hash(value) -> str:
    rendered = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(rendered.encode()).hexdigest()


async def _seed_dispatched_batch(
    database: Database,
    *,
    worker_id: str,
) -> tuple[RunClaim, object]:
    batch_dto = parse_agent_actions(
        ModelResponse(
            tool_calls=(
                ModelToolCall(id="call-0", name="search", arguments={"q": "zero"}),
                ModelToolCall(id="call-1", name="search", arguments={"q": "one"}),
                ModelToolCall(id="call-2", name="search", arguments={"q": "two"}),
            )
        )
    )
    lease_token = uuid4()
    suffix = uuid4().hex[:10]
    async with database.admin_transaction() as session:
        tenant = Tenant(name=f"Dispatch {suffix}", slug=f"dispatch-{suffix}")
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Dispatch Agent",
        )
        session.add_all([project, agent])
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="assistant",
            mandate="Dispatch committed Actions",
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
            title="Dispatch recovery",
            input={"prompt": "dispatch"},
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
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
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
            reason="dispatch-test",
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
            action_count=len(batch_dto.actions),
        )
        session.add(batch)
        await session.flush()
        for ordinal, action in enumerate(batch_dto.actions):
            session.add(
                AgentActionRecord(
                    tenant_id=tenant.id,
                    run_id=run.id,
                    batch_id=batch.id,
                    ordinal=ordinal,
                    action_id=action.action_id,
                    kind=action.kind,
                    provider_call_id=action.provider_call_id,
                    tool_name=action.name,
                    arguments_hash=_hash(action.arguments),
                )
            )
        await session.flush()
        claim = RunClaim(
            run_id=run.id,
            tenant_id=tenant.id,
            lease_token=lease_token,
            previous_status="running",
        )
    return claim, batch_dto


@pytest.mark.asyncio
async def test_postgres_cursor_resume_stops_at_uncertain_ordinal_without_repeating() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    worker_id = f"dispatch-{uuid4().hex[:8]}"
    try:
        claim, batch = await _seed_dispatched_batch(database, worker_id=worker_id)
        service = RuntimeExecutionService(database)
        handler = RunActionHandler(service, claim, worker_id=worker_id)

        await handler.begin_action(
            batch.content_hash,
            ordinal=0,
            action_id=batch.actions[0].action_id,
        )
        state = await handler.complete_action(
            batch.content_hash,
            ordinal=0,
            action_id=batch.actions[0].action_id,
            outcome=RuntimeActionOutcome(
                status="succeeded",
                outcome_ref="tool_action:first",
            ),
        )
        assert state.dispatch_cursor == 1
        await handler.begin_action(
            batch.content_hash,
            ordinal=1,
            action_id=batch.actions[1].action_id,
        )

        executed: list[int] = []

        async def execute(_action, ordinal):
            executed.append(ordinal)
            return RuntimeActionOutcome(status="succeeded")

        result = await AgentActionDispatcher(handler).dispatch(batch, execute)

        assert executed == []
        assert result.completed is False
        assert result.terminal_outcome is not None
        assert result.terminal_outcome.status == "unknown"
        persisted = await handler.wait_for_batch(batch.content_hash)
        assert persisted.dispatch_cursor == 1
        assert [item.status for item in persisted.actions] == [
            "succeeded",
            "dispatched",
            "pending",
        ]
        async with database.admin_transaction() as session:
            row = await session.scalar(
                select(AgentActionBatch).where(AgentActionBatch.run_id == claim.run_id)
            )
            assert row is not None and row.status == "dispatching"
    finally:
        await engine.dispose()
