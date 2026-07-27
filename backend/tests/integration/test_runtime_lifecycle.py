from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import Settings
from nico_agent.database import Database, RunClaim, TenantContext
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    Event,
    Project,
    Run,
    RunStep,
    RuntimeSession,
    Task,
    Tenant,
    ToolApprovalRequest,
    ToolCall,
    ToolDefinition,
)
from nico_agent.domain.states import RUN_TRANSITIONS, RunStatus
from nico_agent.runtime.contracts import (
    RuntimeLoopState,
    RuntimeSessionStatus,
    RuntimeTrajectory,
)
from nico_agent.runtime.lifecycle import RunLifecycleAuthority
from nico_agent.runtime.service import RuntimeExecutionService

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


async def _seed_run(database: Database, status: RunStatus = RunStatus.PENDING) -> Run:
    suffix = uuid4().hex[:10]
    async with database.admin_transaction() as session:
        tenant = Tenant(name=f"Lifecycle {suffix}", slug=f"lifecycle-{suffix}")
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Lifecycle Agent",
        )
        session.add_all([project, agent])
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="lifecycle-test",
            mandate="Prove lifecycle behavior",
            run_config={},
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
            title="Lifecycle acceptance",
            status="running",
            priority=0,
        )
        session.add(task)
        await session.flush()
        run = Run(
            tenant_id=tenant.id,
            task_id=task.id,
            agent_id=agent.id,
            agent_version_id=version.id,
            attempt=1,
            status=status.value,
        )
        session.add(run)
        await session.flush()
        run_id = run.id
        tenant_id = tenant.id

    async with database.admin_transaction() as session:
        return await session.scalar(select(Run).where(Run.tenant_id == tenant_id, Run.id == run_id))


async def _seed_decided_approval_handshake(
    database: Database,
) -> tuple[RunClaim, ToolApprovalRequest, str]:
    suffix = uuid4().hex[:10]
    worker_id = f"approval-worker-{suffix}"
    lease_token = uuid4()
    now = datetime.now(UTC)
    async with database.admin_transaction() as session:
        tenant = Tenant(name=f"Approval {suffix}", slug=f"approval-{suffix}")
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Approval Agent",
        )
        tool = ToolDefinition(
            tenant_id=tenant.id,
            name=f"test.approval.{suffix}",
            version="1.0.0",
            status="enabled",
            description="Lifecycle approval handshake fixture",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            permission="test.execute",
            timeout_seconds=5,
            retry_policy={"max_attempts": 1},
            isolation_policy={"mode": "in_process"},
            risk="high",
            max_output_bytes=1024,
            implementation_hash="a" * 64,
            content_hash="b" * 64,
            created_by="test",
            enabled_at=now,
        )
        session.add_all([project, agent, tool])
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="lifecycle-test",
            mandate="Prove approval suspension ordering",
            run_config={},
            content_hash="c" * 64,
        )
        session.add(version)
        await session.flush()
        agent.current_version_id = version.id
        agent.status = "ready"
        task = Task(
            tenant_id=tenant.id,
            project_id=project.id,
            assignee_agent_id=agent.id,
            title="Approval lifecycle acceptance",
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
            status=RunStatus.WAITING_FOR_APPROVAL.value,
            lease_owner=worker_id,
            lease_token=lease_token,
            lease_expires_at=now + timedelta(minutes=5),
            heartbeat_at=now,
        )
        session.add(run)
        await session.flush()
        runtime_session = RuntimeSession(
            tenant_id=tenant.id,
            run_id=run.id,
            provider_name="mock",
            provider_version="1.0",
            protocol_version="2.0",
            status="running",
        )
        step = RunStep(
            tenant_id=tenant.id,
            run_id=run.id,
            sequence=1,
            kind="tool",
            status="waiting",
        )
        session.add_all([runtime_session, step])
        await session.flush()
        call = ToolCall(
            tenant_id=tenant.id,
            run_id=run.id,
            run_step_id=step.id,
            tool_definition_id=tool.id,
            tool_name=tool.name,
            tool_version=tool.version,
            idempotency_key=f"call-{suffix}",
            arguments_hash="d" * 64,
            caller="runtime:test",
            arguments={},
            status="pending",
        )
        session.add(call)
        await session.flush()
        approval = ToolApprovalRequest(
            tenant_id=tenant.id,
            run_id=run.id,
            run_step_id=step.id,
            tool_call_id=call.id,
            tool_definition_id=tool.id,
            risk_level="high",
            status="requested",
            requester=f"worker:{worker_id}",
            arguments_redacted={},
            arguments_hash=call.arguments_hash,
            expires_at=now + timedelta(minutes=10),
        )
        session.add(approval)
        await session.flush()
        approval.status = "approved"
        approval.allowed_scope = "once"
        approval.decided_by = "operator:test"
        approval.decision = {"status": "approved", "scope": "once"}
        approval.decision_idempotency_key = f"decision-{suffix}"
        approval.decided_at = now
        approval.revision += 1
        await session.flush()
        claim = RunClaim(
            run_id=run.id,
            tenant_id=tenant.id,
            lease_token=lease_token,
            previous_status=RunStatus.WAITING_FOR_APPROVAL.value,
        )
        return claim, approval, worker_id


@pytest.mark.asyncio
async def test_python_authority_commits_one_event_and_rejects_terminal_mutation() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        seeded = await _seed_run(database)
        context = TenantContext(seeded.tenant_id, "worker:lifecycle", uuid4())
        async with database.tenant_transaction(context) as session:
            run = await session.scalar(
                select(Run)
                .where(Run.tenant_id == seeded.tenant_id, Run.id == seeded.id)
                .with_for_update()
            )
            await RunLifecycleAuthority.transition(
                session,
                context,
                run,
                target=RunStatus.PLANNING,
                reason="integration_prepare",
                metadata={"worker_id": "lifecycle"},
                loop_state=RuntimeLoopState.INITIALIZING,
                event_type="RunPlanningStarted",
            )

        async with database.tenant_transaction(context) as session:
            run = await session.scalar(
                select(Run)
                .where(Run.tenant_id == seeded.tenant_id, Run.id == seeded.id)
                .with_for_update()
            )
            await RunLifecycleAuthority.transition(
                session,
                context,
                run,
                target=RunStatus.COMPLETED,
                reason="integration_direct_complete",
                loop_state=RuntimeLoopState.COMPLETED,
                event_type="RunCompleted",
            )
            terminal_revision = run.revision
            event_count = await session.scalar(
                select(func.count())
                .select_from(Event)
                .where(
                    Event.tenant_id == run.tenant_id,
                    Event.run_id == run.id,
                    Event.aggregate_type == "run",
                )
            )

        with pytest.raises(Exception) as captured:
            async with database.tenant_transaction(context) as session:
                run = await session.scalar(
                    select(Run)
                    .where(Run.tenant_id == seeded.tenant_id, Run.id == seeded.id)
                    .with_for_update()
                )
                await RunLifecycleAuthority.transition(
                    session,
                    context,
                    run,
                    target=RunStatus.RUNNING,
                    reason="illegal_terminal_wake",
                )
        assert getattr(captured.value, "code", None) == "INVALID_STATE_TRANSITION"

        async with database.tenant_transaction(context) as session:
            run = await session.scalar(
                select(Run).where(Run.tenant_id == seeded.tenant_id, Run.id == seeded.id)
            )
            after_count = await session.scalar(
                select(func.count())
                .select_from(Event)
                .where(
                    Event.tenant_id == run.tenant_id,
                    Event.run_id == run.id,
                    Event.aggregate_type == "run",
                )
            )
            assert run.revision == terminal_revision
            assert after_count == event_count == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_python_and_sql_transition_matrix_and_claimability_are_identical() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    now = datetime.now(UTC)
    try:
        async with database.admin_transaction() as session:
            for source in RunStatus:
                for target in RunStatus:
                    sql_allowed = await session.scalar(
                        text("SELECT runtime_lifecycle_transition_allowed(:source, :target)"),
                        {"source": source.value, "target": target.value},
                    )
                    assert bool(sql_allowed) is (target in RUN_TRANSITIONS[source])

            for status in RunStatus:
                sql_claimable = await session.scalar(
                    text("SELECT runtime_run_claimable(:status, NULL, :at)"),
                    {"status": status.value, "at": now},
                )
                expected = status in {
                    RunStatus.PENDING,
                    RunStatus.PLANNING,
                    RunStatus.RUNNING,
                }
                assert bool(sql_claimable) is expected
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_claimer_cannot_execute_raw_transition_but_can_execute_reconcilers() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    signature = (
        "public.transition_run_lifecycle(uuid,uuid,text,text,text,jsonb,text,uuid,text,text)"
    )
    try:
        async with database.admin_transaction() as session:
            raw_execute = await session.scalar(
                text("SELECT has_function_privilege('nico_worker_claimer', :signature, 'EXECUTE')"),
                {"signature": signature},
            )
            coordination_execute = await session.scalar(
                text(
                    "SELECT has_function_privilege("
                    "'nico_worker_claimer', "
                    "'public.reconcile_coordination_waiters()', 'EXECUTE')"
                )
            )
            approval_execute = await session.scalar(
                text(
                    "SELECT has_function_privilege("
                    "'nico_worker_claimer', "
                    "'public.reconcile_expired_tool_approvals()', 'EXECUTE')"
                )
            )
            user_input_execute = await session.scalar(
                text(
                    "SELECT has_function_privilege("
                    "'nico_worker_claimer', "
                    "'public.reconcile_user_input_requests()', 'EXECUTE')"
                )
            )
            assert raw_execute is False
            assert coordination_execute is True
            assert approval_execute is True
            assert user_input_execute is True

        async with database.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE nico_worker_claimer"))
            with pytest.raises(ProgrammingError):
                async with session.begin_nested():
                    await session.scalar(
                        text(
                            "SELECT transition_run_lifecycle("
                            ":tenant_id, :run_id, 'pending', 'planning', "
                            "'forbidden', '{}'::jsonb, 'forged', :correlation_id, "
                            "'RunPlanningStarted', 'forged.transition')"
                        ),
                        {
                            "tenant_id": uuid4(),
                            "run_id": uuid4(),
                            "correlation_id": uuid4(),
                        },
                    )

        assert await database.reconcile_coordination_waiters() >= 0
        assert await database.reconcile_expired_tool_approvals() >= 0
        assert await database.reconcile_user_input_requests() >= 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sql_approval_wake_preserves_live_handshake_lease_and_status_alias() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    seeded = await _seed_run(database, RunStatus.WAITING_FOR_APPROVAL)
    worker_id = f"sql-approval-{uuid4()}"
    lease_token = uuid4()
    lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    try:
        async with database.admin_transaction() as session:
            await session.execute(
                text(
                    "UPDATE runs SET lease_owner = :worker_id, lease_token = :lease_token, "
                    "lease_expires_at = :lease_expires_at, heartbeat_at = clock_timestamp() "
                    "WHERE tenant_id = :tenant_id AND id = :run_id"
                ),
                {
                    "worker_id": worker_id,
                    "lease_token": lease_token,
                    "lease_expires_at": lease_expires_at,
                    "tenant_id": seeded.tenant_id,
                    "run_id": seeded.id,
                },
            )
            transitioned = await session.scalar(
                text(
                    "SELECT transition_run_lifecycle("
                    ":tenant_id, :run_id, 'waiting_for_approval', 'running', "
                    "'approval_decided', '{}'::jsonb, 'operator:test', "
                    ":correlation_id, 'RunWoken', 'tool.approval.decide')"
                ),
                {
                    "tenant_id": seeded.tenant_id,
                    "run_id": seeded.id,
                    "correlation_id": uuid4(),
                },
            )
            assert transitioned is True

        async with database.admin_transaction() as session:
            run = await session.scalar(select(Run).where(Run.id == seeded.id))
            event = await session.scalar(
                select(Event)
                .where(Event.run_id == seeded.id, Event.event_type == "RunWoken")
                .order_by(Event.created_at.desc())
            )
            assert run is not None
            assert run.status == RunStatus.RUNNING.value
            assert run.lease_owner == worker_id
            assert run.lease_token == lease_token
            assert run.lease_expires_at == lease_expires_at
            assert event is not None
            assert event.payload["status"] == event.payload["target"] == "running"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_decision_before_suspend_persists_recovery_boundary_then_releases_lease() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    service = RuntimeExecutionService(database)
    trajectory = RuntimeTrajectory(
        provider="mock",
        provider_version="1.0",
        external_session_id="approval-handshake",
        status=RuntimeSessionStatus.SUSPENDED,
        events=[],
    )
    claim, approval, worker_id = await _seed_decided_approval_handshake(database)
    context = TenantContext(claim.tenant_id, "operator:test", uuid4())
    try:
        async with database.tenant_transaction(context) as session:
            run = await session.scalar(
                select(Run)
                .where(Run.tenant_id == claim.tenant_id, Run.id == claim.run_id)
                .with_for_update()
            )
            assert run is not None
            await RunLifecycleAuthority.transition(
                session,
                context,
                run,
                target=RunStatus.RUNNING,
                reason="approval_decided_before_suspend",
                event_type="RunWoken",
                action="tool.approval.decide",
            )
            assert run.lease_owner is not None
            assert run.lease_token == claim.lease_token

        checkpoint = {"schema_version": 2, "cursor": "after-approval-request"}
        usage = {"input_tokens": 13}
        suspended = await service.suspend_claim(
            SimpleNamespace(claim=claim),
            worker_id=worker_id,
            checkpoint=checkpoint,
            wake_condition={"type": "tool_approval", "approval_id": str(approval.id)},
            usage=usage,
            trajectory=trajectory,
        )
        assert suspended is True

        async with database.admin_transaction() as session:
            run = await session.scalar(select(Run).where(Run.id == claim.run_id))
            runtime_session = await session.scalar(
                select(RuntimeSession).where(RuntimeSession.run_id == claim.run_id)
            )
            assert run is not None and runtime_session is not None
            assert run.status == RunStatus.RUNNING.value
            assert run.checkpoint == checkpoint
            assert run.cost == usage
            assert run.lease_owner is None
            assert run.lease_token is None
            assert run.lease_expires_at is None
            assert run.heartbeat_at is None
            assert runtime_session.status == RuntimeSessionStatus.SUSPENDED.value
            assert runtime_session.checkpoint == checkpoint
            assert runtime_session.usage == usage
            assert runtime_session.trajectory == trajectory.model_dump(mode="json")

        # Do not leave a deliberately claimable recovery fixture behind for
        # later integration tests that exercise the global queue.
        async with database.tenant_transaction(context) as session:
            run = await session.scalar(
                select(Run)
                .where(Run.tenant_id == claim.tenant_id, Run.id == claim.run_id)
                .with_for_update()
            )
            assert run is not None
            await RunLifecycleAuthority.transition(
                session,
                context,
                run,
                target=RunStatus.CANCELLED,
                reason="approval_handshake_fixture_complete",
                loop_state=RuntimeLoopState.CANCELLED,
                event_type="RunCancelled",
                action="test.fixture.cleanup",
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sql_wake_is_once_and_cancellation_wins_a_wake_race() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    seeded = await _seed_run(database, RunStatus.WAITING_FOR_APPROVAL)

    async def wake() -> bool:
        async with database.admin_transaction() as session:
            return bool(
                await session.scalar(
                    text(
                        "SELECT transition_run_lifecycle("
                        ":tenant_id, :run_id, 'waiting_for_approval', 'running', "
                        "'approval_decided', '{}'::jsonb, 'system:test-wake', "
                        ":correlation_id, 'RunWoken', 'test.wake')"
                    ),
                    {
                        "tenant_id": seeded.tenant_id,
                        "run_id": seeded.id,
                        "correlation_id": uuid4(),
                    },
                )
            )

    async def cancel() -> bool:
        context = TenantContext(seeded.tenant_id, "operator:test-cancel", uuid4())
        async with database.admin_transaction() as session:
            run = await session.scalar(
                select(Run)
                .where(Run.tenant_id == seeded.tenant_id, Run.id == seeded.id)
                .with_for_update()
            )
            if RunStatus(run.status) is RunStatus.CANCELLED:
                return False
            await RunLifecycleAuthority.transition(
                session,
                context,
                run,
                target=RunStatus.CANCELLED,
                reason="operator_cancelled",
                loop_state=RuntimeLoopState.CANCELLED,
                event_type="RunCancelled",
            )
            return True

    try:
        wake_result, cancel_result = await asyncio.gather(wake(), cancel())
        assert cancel_result is True
        assert wake_result in {True, False}
        assert await wake() is False

        async with database.admin_transaction() as session:
            run = await session.scalar(
                select(Run).where(Run.tenant_id == seeded.tenant_id, Run.id == seeded.id)
            )
            lifecycle_events = await session.scalar(
                select(func.count())
                .select_from(Event)
                .where(
                    Event.tenant_id == seeded.tenant_id,
                    Event.run_id == seeded.id,
                    Event.aggregate_type == "run",
                )
            )
            assert run.status == RunStatus.CANCELLED.value
            assert run.lifecycle_revision == lifecycle_events
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_0027_expansion_defaults_preserve_representative_0026_run_shape() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        seeded = await _seed_run(database)
        async with database.admin_transaction() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT lifecycle_reason, lifecycle_metadata, "
                            "lifecycle_revision FROM runs "
                            "WHERE tenant_id = :tenant_id AND id = :run_id"
                        ),
                        {"tenant_id": seeded.tenant_id, "run_id": seeded.id},
                    )
                )
                .mappings()
                .one()
            )
            head = await session.scalar(text("SELECT version_num FROM alembic_version"))
            assert row == {
                "lifecycle_reason": None,
                "lifecycle_metadata": {},
                "lifecycle_revision": 0,
            }
            assert head == "20260727_0033"
            await session.execute(
                text(
                    "UPDATE runs SET status = 'completed', ended_at = clock_timestamp() "
                    "WHERE tenant_id = :tenant_id AND id = :run_id"
                ),
                {"tenant_id": seeded.tenant_id, "run_id": seeded.id},
            )
    finally:
        await engine.dispose()
