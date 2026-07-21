from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.artifacts.contracts import RuntimeArtifactIntent
from nico_agent.artifacts.minio import MinioArtifactStore
from nico_agent.artifacts.service import ArtifactService
from nico_agent.config import Settings
from nico_agent.coordination import BudgetGrant, DelegationIntent
from nico_agent.coordination.policy import build_coordination_policy_snapshot
from nico_agent.coordination.service import CoordinationService
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import AccessDenied, DomainConflict
from nico_agent.domain.models import (
    Agent,
    AgentMessage,
    AgentRunRelation,
    AgentVersion,
    Artifact,
    Delegation,
    Project,
    ProjectMember,
    ProjectSession,
    Run,
    RunBudgetLedger,
    RuntimeSession,
    SharedArtifactLink,
    Task,
    Tenant,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


@dataclass(frozen=True)
class SeededTree:
    context: TenantContext
    parent_run_id: UUID
    worker_id: str
    lease_token: UUID
    parent_version_id: UUID
    target_version_ids: tuple[UUID, UUID]


async def _seed(database: Database, *, token_limit: int = 1000) -> SeededTree:
    suffix = uuid4().hex[:10]
    lease_token = uuid4()
    worker_id = f"coord-{suffix}"
    correlation_id = uuid4()
    async with database.admin_transaction() as session:
        tenant = Tenant(name=f"Coord {suffix}", slug=f"coord-{suffix}")
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        parent_agent = Agent(
            tenant_id=tenant.id, name=f"parent-{suffix}", display_name="Parent Agent"
        )
        child_a = Agent(tenant_id=tenant.id, name=f"child-a-{suffix}", display_name="Child A")
        child_b = Agent(tenant_id=tenant.id, name=f"child-b-{suffix}", display_name="Child B")
        session.add_all([project, parent_agent, child_a, child_b])
        await session.flush()
        common_tool_policy = {
            "allow": ["http.read@1.0.0"],
            "permissions": ["network.read"],
            "secrets": ["authorization"],
        }
        versions = [
            AgentVersion(
                tenant_id=tenant.id,
                agent_id=agent.id,
                version=1,
                status="published",
                role=role,
                mandate=f"Act as {role}",
                tool_policy=common_tool_policy,
                content_hash=character * 64,
            )
            for agent, role, character in (
                (parent_agent, "parent", "p"),
                (child_a, "child-a", "a"),
                (child_b, "child-b", "b"),
            )
        ]
        session.add_all(versions)
        await session.flush()
        allowed = [str(versions[1].id), str(versions[2].id)]
        coordination_policy = {
            "enabled": True,
            "max_depth": 3,
            "max_children": 4,
            "max_parallelism": 4,
            "allowed_agent_version_ids": allowed,
            "allowed_secret_refs": ["authorization"],
        }
        tenant.settings = {
            "coordination_policy": coordination_policy,
            "tool_policy": {
                "allow": ["http.read@1.0.0"],
                "permissions": ["network.read"],
                "secret_refs": {
                    "authorization": "env:NICO_TOOL_SECRET_HTTP_AUTH",
                    "not-delegated": "env:NICO_TOOL_SECRET_OTHER",
                },
            },
        }
        versions[0].coordination_policy = coordination_policy
        for agent, version in zip((parent_agent, child_a, child_b), versions, strict=True):
            agent.current_version_id = version.id
            agent.status = "ready"
        task = Task(
            tenant_id=tenant.id,
            project_id=project.id,
            assignee_agent_id=parent_agent.id,
            title="Coordinate bounded research",
            status="running",
            priority=100,
        )
        session.add(task)
        await session.flush()
        now = datetime.now(UTC)
        run = Run(
            tenant_id=tenant.id,
            task_id=task.id,
            agent_id=parent_agent.id,
            agent_version_id=versions[0].id,
            attempt=1,
            status="running",
            token_budget=token_limit,
            budgets={"cost_limit_microunits": 10000, "tool_call_limit": 10},
            lease_owner=worker_id,
            lease_token=lease_token,
            lease_expires_at=now + timedelta(minutes=5),
            heartbeat_at=now,
            started_at=now,
        )
        session.add(run)
        await session.flush()
        snapshot = build_coordination_policy_snapshot(tenant.settings, coordination_policy)
        session.add(
            RuntimeSession(
                tenant_id=tenant.id,
                run_id=run.id,
                provider_name="nico_native",
                provider_version="test",
                protocol_version="2.0",
                status="running",
                coordination_policy_snapshot=snapshot,
                tool_policy_snapshot={
                    "version": 1,
                    "allow": ["http.read@1.0.0"],
                    "permissions": ["network.read"],
                    "secret_refs": {
                        "authorization": "env:NICO_TOOL_SECRET_HTTP_AUTH",
                        "not-delegated": "env:NICO_TOOL_SECRET_OTHER",
                    },
                    "tools": {},
                    "errors": [],
                },
            )
        )
        tenant_id, run_id = tenant.id, run.id
        target_ids = (versions[1].id, versions[2].id)
    return SeededTree(
        context=TenantContext(tenant_id, "coordination-test", correlation_id),
        parent_run_id=run_id,
        worker_id=worker_id,
        lease_token=lease_token,
        parent_version_id=versions[0].id,
        target_version_ids=target_ids,
    )


def _intent(target: UUID, key: str, *, tokens: int, objective: str | None = None):
    return DelegationIntent(
        target_agent_version_id=target,
        objective=objective or f"Research independently for {key}",
        acceptance={"required": ["summary"]},
        context_refs=("memory:approved",),
        budget=BudgetGrant(token_limit=tokens, cost_limit_microunits=1000, tool_call_limit=1),
        idempotency_key=key,
    )


async def _enable_managed_scope(database: Database, seeded: SeededTree) -> dict[UUID, UUID]:
    async with database.admin_transaction() as session:
        parent_run = await session.get(Run, seeded.parent_run_id)
        assert parent_run is not None
        parent_task = await session.get(Task, parent_run.task_id)
        assert parent_task is not None
        project = await session.get(Project, parent_task.project_id)
        assert project is not None
        project.metadata_json = {"_nico_collaboration": {"managed": True}}
        agent_ids = [parent_run.agent_id]
        for version_id in seeded.target_version_ids:
            version = await session.get(AgentVersion, version_id)
            assert version is not None
            agent_ids.append(version.agent_id)
        sessions: dict[UUID, UUID] = {}
        for index, agent_id in enumerate(agent_ids):
            member = ProjectMember(
                tenant_id=seeded.context.tenant_id,
                project_id=project.id,
                agent_id=agent_id,
                role="lead" if index == 0 else "member",
                status="active",
                created_by="coordination-test",
            )
            session.add(member)
            await session.flush()
            project_session = ProjectSession(
                tenant_id=seeded.context.tenant_id,
                project_id=project.id,
                project_member_id=member.id,
                agent_id=agent_id,
                status="active",
            )
            session.add(project_session)
            await session.flush()
            sessions[agent_id] = project_session.id
        parent_task.project_session_id = sessions[parent_run.agent_id]
        runtime = await session.scalar(
            select(RuntimeSession).where(RuntimeSession.run_id == parent_run.id)
        )
        assert runtime is not None
        runtime.coordination_policy_snapshot = {
            **runtime.coordination_policy_snapshot,
            "target_scope": "project_members",
            "allowed_agent_version_ids": [str(item) for item in seeded.target_version_ids],
        }
        return sessions


@pytest.mark.asyncio
async def test_delegate_atomically_creates_child_closure_assignment_and_idempotent_replay() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        seeded = await _seed(database)
        service = CoordinationService(database)
        intent = _intent(seeded.target_version_ids[0], "first", tokens=400)

        created = await service.delegate(
            seeded.context,
            seeded.parent_run_id,
            intent,
            worker_id=seeded.worker_id,
            lease_token=seeded.lease_token,
        )
        replay = await service.delegate(
            seeded.context,
            seeded.parent_run_id,
            intent,
            worker_id=seeded.worker_id,
            lease_token=seeded.lease_token,
        )

        assert replay.delegation_id == created.delegation_id
        assert replay.idempotent_replay is True
        async with database.tenant_transaction(seeded.context) as session:
            delegation = await session.get(Delegation, created.delegation_id)
            child = await session.get(Run, created.child_run_id)
            relation = await session.scalar(
                select(AgentRunRelation).where(
                    AgentRunRelation.descendant_run_id == created.child_run_id,
                    AgentRunRelation.is_direct.is_(True),
                )
            )
            message = await session.scalar(
                select(AgentMessage).where(AgentMessage.delegation_id == created.delegation_id)
            )
            ledger = await session.scalar(
                select(RunBudgetLedger).where(RunBudgetLedger.run_id == seeded.parent_run_id)
            )
        assert delegation is not None and delegation.status == "accepted"
        assert child is not None and child.status == "pending"
        assert relation is not None and relation.ancestor_run_id == seeded.parent_run_id
        assert message is not None and message.message_type == "task_assignment"
        assert delegation.permission_snapshot["secret_refs"] == {
            "authorization": "env:NICO_TOOL_SECRET_HTTP_AUTH"
        }
        assert ledger is not None and ledger.token_child_reserved == 400

        with pytest.raises(DomainConflict, match="equivalent delegation"):
            await service.delegate(
                seeded.context,
                seeded.parent_run_id,
                intent.model_copy(update={"idempotency_key": "same-work-new-key"}),
                worker_id=seeded.worker_id,
                lease_token=seeded.lease_token,
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_member_delegation_links_session_and_checks_live_state_before_budget() -> (
    None
):
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        seeded = await _seed(database)
        sessions = await _enable_managed_scope(database, seeded)
        service = CoordinationService(database)
        first_target = await service.delegate(
            seeded.context,
            seeded.parent_run_id,
            _intent(seeded.target_version_ids[0], "managed-member", tokens=200),
            worker_id=seeded.worker_id,
            lease_token=seeded.lease_token,
        )
        async with database.admin_transaction() as session:
            child_task = await session.get(Task, first_target.child_task_id)
            first_version = await session.get(AgentVersion, seeded.target_version_ids[0])
            second_version = await session.get(AgentVersion, seeded.target_version_ids[1])
            assert child_task is not None and first_version is not None
            assert second_version is not None
            assert child_task.project_session_id == sessions[first_version.agent_id]
            second_member = await session.scalar(
                select(ProjectMember).where(
                    ProjectMember.tenant_id == seeded.context.tenant_id,
                    ProjectMember.agent_id == second_version.agent_id,
                    ProjectMember.status == "active",
                )
            )
            second_session = await session.scalar(
                select(ProjectSession).where(
                    ProjectSession.tenant_id == seeded.context.tenant_id,
                    ProjectSession.agent_id == second_version.agent_id,
                )
            )
            assert second_member is not None and second_session is not None
            second_member.status = "paused"
            second_session.status = "paused"
            ledger = await session.scalar(
                select(RunBudgetLedger).where(RunBudgetLedger.run_id == seeded.parent_run_id)
            )
            assert ledger is not None
            reserved_before = ledger.token_child_reserved

        with pytest.raises(DomainConflict) as inactive:
            await service.delegate(
                seeded.context,
                seeded.parent_run_id,
                _intent(seeded.target_version_ids[1], "inactive-member", tokens=100),
                worker_id=seeded.worker_id,
                lease_token=seeded.lease_token,
            )
        assert inactive.value.code == "PROJECT_MEMBER_INACTIVE"
        async with database.admin_transaction() as session:
            ledger = await session.scalar(
                select(RunBudgetLedger).where(RunBudgetLedger.run_id == seeded.parent_run_id)
            )
            assert ledger is not None and ledger.token_child_reserved == reserved_before
            parent = await session.get(Run, seeded.parent_run_id)
            assert parent is not None
            task = await session.get(Task, parent.task_id)
            assert task is not None
            project = await session.get(Project, task.project_id)
            assert project is not None
            project.status = "archived"

        with pytest.raises(DomainConflict) as archived:
            await service.delegate(
                seeded.context,
                seeded.parent_run_id,
                _intent(seeded.target_version_ids[1], "archived-project", tokens=100),
                worker_id=seeded.worker_id,
                lease_token=seeded.lease_token,
            )
        assert archived.value.code == "PROJECT_ARCHIVED"
        async with database.admin_transaction() as session:
            ledger = await session.scalar(
                select(RunBudgetLedger).where(RunBudgetLedger.run_id == seeded.parent_run_id)
            )
            assert ledger is not None and ledger.token_child_reserved == reserved_before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_budget_reservations_never_oversell_parent() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        seeded = await _seed(database, token_limit=1000)
        service = CoordinationService(database)

        results = await asyncio.gather(
            service.delegate(
                seeded.context,
                seeded.parent_run_id,
                _intent(seeded.target_version_ids[0], "concurrent-a", tokens=600),
                worker_id=seeded.worker_id,
                lease_token=seeded.lease_token,
            ),
            service.delegate(
                seeded.context,
                seeded.parent_run_id,
                _intent(seeded.target_version_ids[1], "concurrent-b", tokens=600),
                worker_id=seeded.worker_id,
                lease_token=seeded.lease_token,
            ),
            return_exceptions=True,
        )

        assert sum(not isinstance(result, Exception) for result in results) == 1
        failure = next(result for result in results if isinstance(result, Exception))
        assert isinstance(failure, DomainConflict)
        assert failure.code == "DELEGATION_BUDGET_EXCEEDED"
        async with database.tenant_transaction(seeded.context) as session:
            ledger = await session.scalar(
                select(RunBudgetLedger).where(RunBudgetLedger.run_id == seeded.parent_run_id)
            )
            child_count = await session.scalar(
                select(func.count())
                .select_from(Delegation)
                .where(Delegation.parent_run_id == seeded.parent_run_id)
            )
        assert ledger is not None and ledger.token_child_reserved == 600
        assert child_count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_count_depth_and_ancestry_cycle_guards_are_fail_closed() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        service = CoordinationService(database)

        count_seed = await _seed(database)
        async with database.admin_transaction() as session:
            runtime = await session.scalar(
                select(RuntimeSession).where(RuntimeSession.run_id == count_seed.parent_run_id)
            )
            assert runtime is not None
            runtime.coordination_policy_snapshot = {
                **runtime.coordination_policy_snapshot,
                "max_children": 1,
            }
        await service.delegate(
            count_seed.context,
            count_seed.parent_run_id,
            _intent(count_seed.target_version_ids[0], "only-child", tokens=100),
            worker_id=count_seed.worker_id,
            lease_token=count_seed.lease_token,
        )
        with pytest.raises(DomainConflict) as count_error:
            await service.delegate(
                count_seed.context,
                count_seed.parent_run_id,
                _intent(count_seed.target_version_ids[1], "second-child", tokens=100),
                worker_id=count_seed.worker_id,
                lease_token=count_seed.lease_token,
            )
        assert count_error.value.code == "DELEGATION_COUNT_EXCEEDED"

        cycle_seed = await _seed(database)
        async with database.admin_transaction() as session:
            runtime = await session.scalar(
                select(RuntimeSession).where(RuntimeSession.run_id == cycle_seed.parent_run_id)
            )
            assert runtime is not None
            runtime.coordination_policy_snapshot = {
                **runtime.coordination_policy_snapshot,
                "allowed_agent_version_ids": [str(cycle_seed.parent_version_id)],
            }
        with pytest.raises(DomainConflict) as cycle_error:
            await service.delegate(
                cycle_seed.context,
                cycle_seed.parent_run_id,
                _intent(cycle_seed.parent_version_id, "cycle", tokens=100),
                worker_id=cycle_seed.worker_id,
                lease_token=cycle_seed.lease_token,
            )
        assert cycle_error.value.code == "DELEGATION_CYCLE"

        depth_seed = await _seed(database)
        async with database.admin_transaction() as session:
            runtime = await session.scalar(
                select(RuntimeSession).where(RuntimeSession.run_id == depth_seed.parent_run_id)
            )
            assert runtime is not None
            runtime.coordination_policy_snapshot = {
                **runtime.coordination_policy_snapshot,
                "max_depth": 1,
            }
        first_child = await service.delegate(
            depth_seed.context,
            depth_seed.parent_run_id,
            _intent(depth_seed.target_version_ids[0], "depth-one", tokens=300),
            worker_id=depth_seed.worker_id,
            lease_token=depth_seed.lease_token,
        )
        child_worker, child_lease = "depth-child", uuid4()
        async with database.admin_transaction() as session:
            child = await session.get(Run, first_child.child_run_id)
            assert child is not None
            child.status = "running"
            child.lease_owner = child_worker
            child.lease_token = child_lease
            child.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            session.add(
                RuntimeSession(
                    tenant_id=depth_seed.context.tenant_id,
                    run_id=child.id,
                    provider_name="nico_native",
                    provider_version="test",
                    protocol_version="2.0",
                    status="running",
                    coordination_policy_snapshot={
                        **runtime.coordination_policy_snapshot,
                        "allowed_agent_version_ids": [str(depth_seed.target_version_ids[1])],
                    },
                    tool_policy_snapshot={
                        "version": 1,
                        "allow": ["http.read@1.0.0"],
                        "permissions": ["network.read"],
                        "secret_refs": {"authorization": "env:NICO_TOOL_SECRET_HTTP_AUTH"},
                    },
                )
            )
        with pytest.raises(DomainConflict) as depth_error:
            await service.delegate(
                depth_seed.context,
                first_child.child_run_id,
                _intent(depth_seed.target_version_ids[1], "too-deep", tokens=100),
                worker_id=child_worker,
                lease_token=child_lease,
            )
        assert depth_error.value.code == "DELEGATION_DEPTH_EXCEEDED"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_coordination_tables_force_rls_cross_tenant_fk_and_terminal_immutability() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        first = await _seed(database)
        second = await _seed(database)
        service = CoordinationService(database)
        created = await service.delegate(
            first.context,
            first.parent_run_id,
            _intent(first.target_version_ids[0], "terminal", tokens=100),
            worker_id=first.worker_id,
            lease_token=first.lease_token,
        )
        async with database.admin_transaction() as session:
            protected = (
                await session.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname IN ('delegations', 'agent_run_relations', "
                        "'agent_messages', 'run_budget_ledgers') ORDER BY relname"
                    )
                )
            ).all()
        assert len(protected) == 4
        assert all(row[1:] == (True, True) for row in protected)

        async with database.admin_transaction() as session:
            delegation = await session.get(Delegation, created.delegation_id)
            assert delegation is not None
            delegation.status = "completed"
            delegation.ended_at = datetime.now(UTC)
            delegation.revision += 1
        with pytest.raises(IntegrityError):
            async with database.admin_transaction() as session:
                delegation = await session.get(Delegation, created.delegation_id)
                assert delegation is not None
                delegation.result = {"mutated": True}

        with pytest.raises(IntegrityError):
            async with database.admin_transaction() as session:
                session.add(
                    AgentMessage(
                        tenant_id=first.context.tenant_id,
                        delegation_id=created.delegation_id,
                        sender_run_id=first.parent_run_id,
                        receiver_run_id=second.parent_run_id,
                        message_type="system_notice",
                        idempotency_key="cross-tenant",
                        correlation_id=uuid4(),
                    )
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_tree_cancel_releases_reservations_and_prevents_late_wake() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        seeded = await _seed(database)
        service = CoordinationService(database)
        first = await service.delegate(
            seeded.context,
            seeded.parent_run_id,
            _intent(seeded.target_version_ids[0], "cancel-a", tokens=200),
            worker_id=seeded.worker_id,
            lease_token=seeded.lease_token,
        )
        second = await service.delegate(
            seeded.context,
            seeded.parent_run_id,
            _intent(seeded.target_version_ids[1], "cancel-b", tokens=300),
            worker_id=seeded.worker_id,
            lease_token=seeded.lease_token,
        )
        async with database.admin_transaction() as session:
            parent = await session.get(Run, seeded.parent_run_id)
            assert parent is not None
            parent.status = "waiting_for_subagent"
            parent.lease_owner = None
            parent.lease_token = None
            parent.lease_expires_at = None
            revision = parent.revision

        cancelled = await service.cancel_tree(
            seeded.context,
            seeded.parent_run_id,
            expected_revision=revision,
        )
        replay = await service.cancel_tree(
            seeded.context,
            seeded.parent_run_id,
            expected_revision=revision,
        )

        assert cancelled.status == "cancelled"
        assert replay.status == "cancelled"
        assert await database.reconcile_coordination_waiters() == 0
        async with database.admin_transaction() as session:
            runs = list(
                await session.scalars(
                    select(Run).where(
                        Run.id.in_([seeded.parent_run_id, first.child_run_id, second.child_run_id])
                    )
                )
            )
            delegations = list(
                await session.scalars(
                    select(Delegation).where(Delegation.parent_run_id == seeded.parent_run_id)
                )
            )
            ledger = await session.scalar(
                select(RunBudgetLedger).where(RunBudgetLedger.run_id == seeded.parent_run_id)
            )
        assert {run.status for run in runs} == {"cancelled"}
        assert {delegation.status for delegation in delegations} == {"cancelled"}
        assert ledger is not None and ledger.token_child_reserved == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reconciler_wakes_terminal_tree_and_retry_request_is_idempotent() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        seeded = await _seed(database)
        service = CoordinationService(database)
        created = await service.delegate(
            seeded.context,
            seeded.parent_run_id,
            _intent(seeded.target_version_ids[0], "reconcile", tokens=100),
            worker_id=seeded.worker_id,
            lease_token=seeded.lease_token,
        )
        message = await service.request_retry(
            seeded.context,
            created.delegation_id,
            reason="child requests a bounded retry",
            idempotency_key="retry-request-1",
        )
        replay = await service.request_retry(
            seeded.context,
            created.delegation_id,
            reason="child requests a bounded retry",
            idempotency_key="retry-request-1",
        )
        assert replay.id == message.id

        async with database.admin_transaction() as session:
            parent = await session.get(Run, seeded.parent_run_id)
            runtime = await session.scalar(
                select(RuntimeSession).where(RuntimeSession.run_id == seeded.parent_run_id)
            )
            delegation = await session.get(Delegation, created.delegation_id)
            assert parent is not None and runtime is not None and delegation is not None
            parent.status = "waiting_for_subagent"
            parent.lease_owner = None
            parent.lease_token = None
            parent.lease_expires_at = None
            runtime.status = "suspended"
            delegation.status = "completed"
            delegation.ended_at = datetime.now(UTC)
            delegation.revision += 1

        assert await database.reconcile_coordination_waiters() == 1
        async with database.admin_transaction() as session:
            parent = await session.get(Run, seeded.parent_run_id)
            runtime = await session.scalar(
                select(RuntimeSession).where(RuntimeSession.run_id == seeded.parent_run_id)
            )
        assert parent is not None and parent.status == "running"
        assert parent.lease_owner is None
        assert runtime is not None and runtime.status == "paused"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_content_addressed_artifact_is_private_shared_and_has_no_temp_orphan() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    store = MinioArtifactStore(
        settings.minio_url,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        bucket=settings.minio_bucket,
    )
    artifacts = ArtifactService(database, store, max_bytes=settings.artifact_max_bytes)
    try:
        seeded = await _seed(database)
        coordination = CoordinationService(database)
        child = await coordination.delegate(
            seeded.context,
            seeded.parent_run_id,
            _intent(seeded.target_version_ids[0], "artifact-owner", tokens=100),
            worker_id=seeded.worker_id,
            lease_token=seeded.lease_token,
        )
        sibling = await coordination.delegate(
            seeded.context,
            seeded.parent_run_id,
            _intent(seeded.target_version_ids[1], "artifact-sibling", tokens=100),
            worker_id=seeded.worker_id,
            lease_token=seeded.lease_token,
        )
        intent = RuntimeArtifactIntent(
            name="finding.txt",
            content_type="text/plain",
            content_text="verified child finding",
            share_with_parent=True,
            idempotency_key="artifact-1",
        )
        created, concurrent_replay = await asyncio.gather(
            artifacts.store_runtime_artifact(seeded.context, child.child_run_id, intent),
            artifacts.store_runtime_artifact(seeded.context, child.child_run_id, intent),
        )
        replay = await artifacts.store_runtime_artifact(seeded.context, child.child_run_id, intent)

        assert concurrent_replay.artifact_id == created.artifact_id
        assert replay.artifact_id == created.artifact_id
        assert created.size_bytes == len(b"verified child finding")
        assert created.shared_with_run_ids == (seeded.parent_run_id,)
        visible = await artifacts.list_for_run(seeded.context, seeded.parent_run_id)
        assert [item.id for item in visible] == [created.artifact_id]
        metadata, data = await artifacts.read_for_run(
            seeded.context, seeded.parent_run_id, created.artifact_id
        )
        assert data == b"verified child finding"
        assert metadata.sha256 == created.sha256
        with pytest.raises(AccessDenied):
            await artifacts.read_for_run(seeded.context, sibling.child_run_id, created.artifact_id)

        async with database.admin_transaction() as session:
            artifact = await session.get(Artifact, created.artifact_id)
            link = await session.scalar(
                select(SharedArtifactLink).where(
                    SharedArtifactLink.artifact_id == created.artifact_id
                )
            )
        assert artifact is not None and artifact.status == "available"
        assert link is not None and link.grantee_run_id == seeded.parent_run_id
        assert await store.list_keys(f"tenants/{seeded.context.tenant_id}/tmp/") == []
        async with httpx.AsyncClient() as client:
            unsigned = await client.get(
                f"{settings.minio_url}/{settings.minio_bucket}/{artifact.object_key}"
            )
        assert unsigned.status_code in {401, 403}

        assert artifact.object_key is not None
        corrupt_key = f"tenants/{seeded.context.tenant_id}/tmp/corrupt-{uuid4()}"
        await store.put_temp(corrupt_key, b"tampered", "text/plain")
        await store.promote(corrupt_key, artifact.object_key)
        with pytest.raises(DomainConflict, match="hash or size"):
            await artifacts.read_for_run(seeded.context, seeded.parent_run_id, created.artifact_id)
        restore_key = f"tenants/{seeded.context.tenant_id}/tmp/restore-{uuid4()}"
        await store.put_temp(restore_key, b"verified child finding", "text/plain")
        await store.promote(restore_key, artifact.object_key)

        with pytest.raises(IntegrityError):
            async with database.admin_transaction() as session:
                terminal = await session.get(Artifact, created.artifact_id)
                assert terminal is not None
                terminal.name = "rewritten.txt"

        async with database.admin_transaction() as session:
            rls = (
                await session.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity "
                        "FROM pg_class WHERE relname IN "
                        "('artifacts', 'shared_artifact_links') ORDER BY relname"
                    )
                )
            ).all()
        assert rls == [(True, True), (True, True)]
        assert await store.list_keys(f"tenants/{seeded.context.tenant_id}/tmp/") == []
    finally:
        await engine.dispose()
