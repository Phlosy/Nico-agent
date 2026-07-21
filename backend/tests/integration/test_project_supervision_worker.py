from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import Settings
from nico_agent.database import Database, TenantContext
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    Project,
    ProjectMember,
    ProjectSession,
    ProjectSupervisionCycle,
    Run,
    Task,
    Tenant,
)
from nico_agent.projects.orchestration import ProjectOrchestrationService
from nico_agent.projects.worker import ProjectSupervisionWorker

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with the Compose dependencies running",
)


async def _seed(database: Database) -> tuple[TenantContext, dict[str, object]]:
    suffix = uuid4().hex[:10]
    coordination_policy = {
        "enabled": True,
        "allowed_target_scopes": ["project_members"],
        "allowed_agent_version_ids": [],
        "allowed_secret_refs": [],
        "max_depth": 3,
        "max_children": 8,
        "max_parallelism": 4,
    }
    async with database.admin_transaction() as session:
        tenant = Tenant(
            name=f"Supervision {suffix}",
            slug=f"supervision-{suffix}",
            settings={"coordination_policy": coordination_policy},
        )
        session.add(tenant)
        await session.flush()
        agents: list[Agent] = []
        versions: list[AgentVersion] = []
        for index in range(3):
            agent = Agent(
                tenant_id=tenant.id,
                name=f"supervision-agent-{index}-{suffix}",
                display_name=f"Supervision Agent {index}",
                status="ready",
            )
            session.add(agent)
            await session.flush()
            version = AgentVersion(
                tenant_id=tenant.id,
                agent_id=agent.id,
                version=1,
                status="published",
                role="project lead" if index != 1 else "project member",
                mandate="Coordinate bounded project work",
                runtime_provider="nico_native",
                execution_mode="plan_and_execute" if index != 1 else "direct",
                coordination_policy=coordination_policy if index != 1 else {},
                budgets={"max_steps": 16, "token_budget": 8000, "timeout_seconds": 300},
                content_hash=uuid4().hex * 2,
            )
            session.add(version)
            await session.flush()
            agent.current_version_id = version.id
            agents.append(agent)
            versions.append(version)
        project = Project(
            tenant_id=tenant.id,
            name=f"supervision-project-{suffix}",
            kind="shared",
            supervision_cadence_seconds=3600,
            next_supervision_at=datetime.now(UTC) + timedelta(hours=1),
            metadata_json={"_nico_collaboration": {"managed": True}},
        )
        session.add(project)
        await session.flush()
        members: list[ProjectMember] = []
        sessions: list[ProjectSession] = []
        for index, agent in enumerate(agents):
            member = ProjectMember(
                tenant_id=tenant.id,
                project_id=project.id,
                agent_id=agent.id,
                role="lead" if index == 0 else "member",
                status="active",
                created_by="supervision-test",
            )
            session.add(member)
            await session.flush()
            project_session = ProjectSession(
                tenant_id=tenant.id,
                project_id=project.id,
                project_member_id=member.id,
                agent_id=agent.id,
                status="active",
            )
            session.add(project_session)
            await session.flush()
            members.append(member)
            sessions.append(project_session)
        context = TenantContext(tenant.id, "supervision-test", uuid4())
        return context, {
            "project_id": project.id,
            "agents": [agent.id for agent in agents],
            "versions": [version.id for version in versions],
            "members": [member.id for member in members],
            "sessions": [value.id for value in sessions],
        }


@pytest.mark.asyncio
async def test_supervision_claim_is_exactly_once_recovers_and_uses_current_lead() -> None:
    engine = create_async_engine(Settings(environment="test", _env_file=None).resolved_database_url)
    database = Database(engine)
    try:
        context, seeded = await _seed(database)
        project_id = seeded["project_id"]
        service = ProjectOrchestrationService(database)
        first = await service.create_manual(context, project_id, idempotency_key="manual-one")
        replay = await service.create_manual(context, project_id, idempotency_key="manual-one")
        assert replay.id == first.id

        claimed = await asyncio.gather(
            ProjectSupervisionWorker(database, worker_id="supervisor-one").execute_once(),
            ProjectSupervisionWorker(database, worker_id="supervisor-two").execute_once(),
        )
        assert claimed.count(True) == 1
        async with database.admin_transaction() as session:
            materialized = await session.get(ProjectSupervisionCycle, first.id)
            assert materialized is not None
            assert materialized.status == "running"
            assert materialized.task_id is not None
            assert materialized.run_id is not None
            assert (
                await session.scalar(
                    select(func.count(Task.id)).where(Task.id == materialized.task_id)
                )
            ) == 1
            assert (
                await session.scalar(
                    select(func.count(Run.id)).where(Run.id == materialized.run_id)
                )
            ) == 1
            materialized_run_id = materialized.run_id

        async with database.tenant_transaction(context) as session:
            completed_run = await session.get(Run, materialized_run_id)
            assert completed_run is not None
            completed_run.status = "completed"
            completed_run.result = {"content": "n" * 5000}
            completed_run.ended_at = datetime.now(UTC)
            completed_run.revision += 1
            await ProjectOrchestrationService.finalize_in_session(
                session,
                context,
                completed_run,
                narrative_output=completed_run.result,
            )
        completed = await service.get_cycle(context, project_id, first.id)
        assert completed.status == "completed"
        assert completed.metrics["project_id"] == str(project_id)
        assert len(completed.narrative_summary or "") == 4000

        crashed = await service.create_manual(
            context, project_id, idempotency_key="manual-recovery"
        )
        abandoned = await database.claim_next_project_supervision("crashed-worker", 30)
        assert abandoned is not None and abandoned.cycle_id == crashed.id
        async with database.admin_transaction() as session:
            claimed_cycle = await session.get(ProjectSupervisionCycle, crashed.id)
            assert claimed_cycle is not None
            claimed_cycle.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        assert await ProjectSupervisionWorker(database, worker_id="recovery-worker").execute_once()
        async with database.admin_transaction() as session:
            recovered = await session.get(ProjectSupervisionCycle, crashed.id)
            assert recovered is not None and recovered.status == "running"
            assert recovered.task_id is not None and recovered.run_id is not None

            old_lead = await session.get(ProjectMember, seeded["members"][0])
            new_lead = await session.get(ProjectMember, seeded["members"][2])
            assert old_lead is not None and new_lead is not None
            old_lead.role = "member"
            await session.flush()
            new_lead.role = "lead"
            await session.flush()

        future = await service.create_manual(context, project_id, idempotency_key="manual-new-lead")
        assert await ProjectSupervisionWorker(database, worker_id="new-lead-worker").execute_once()
        async with database.admin_transaction() as session:
            current = await session.get(ProjectSupervisionCycle, future.id)
            assert current is not None
            run = await session.get(Run, current.run_id)
            assert run is not None
            assert run.agent_id == seeded["agents"][2]
            assert run.agent_version_id == seeded["versions"][2]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_supervision_claim_fails_cleanly_after_project_archive() -> None:
    engine = create_async_engine(Settings(environment="test", _env_file=None).resolved_database_url)
    database = Database(engine)
    try:
        context, seeded = await _seed(database)
        service = ProjectOrchestrationService(database)
        cycle = await service.create_manual(
            context,
            seeded["project_id"],
            idempotency_key="archive-before-materialize",
        )
        claim = await database.claim_next_project_supervision("stale-claim-worker", 30)
        assert claim is not None and claim.cycle_id == cycle.id
        async with database.admin_transaction() as session:
            project = await session.get(Project, seeded["project_id"])
            assert project is not None
            project.status = "archived"

        materialized = await service.materialize_claim(claim, worker_id="stale-claim-worker")
        assert materialized.status == "failed"
        assert materialized.error["code"] == "PROJECT_ARCHIVED"
        assert materialized.task_id is None
        assert materialized.run_id is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_due_cadence_creates_one_slot_and_advances_without_backlog() -> None:
    engine = create_async_engine(Settings(environment="test", _env_file=None).resolved_database_url)
    database = Database(engine)
    try:
        context, seeded = await _seed(database)
        async with database.admin_transaction() as session:
            project = await session.get(Project, seeded["project_id"])
            assert project is not None
            project.next_supervision_at = datetime.now(UTC) - timedelta(days=3)
        assert await ProjectSupervisionWorker(database, worker_id="scheduled-worker").execute_once()
        cycles = await ProjectOrchestrationService(database).list_cycles(
            context, seeded["project_id"]
        )
        scheduled = [cycle for cycle in cycles if cycle.trigger == "scheduled"]
        assert len(scheduled) == 1
        assert scheduled[0].status == "running"
        async with database.admin_transaction() as session:
            project = await session.get(Project, seeded["project_id"])
            assert project is not None and project.next_supervision_at is not None
            assert project.next_supervision_at > datetime.now(UTC)
        assert not await ProjectSupervisionWorker(
            database, worker_id="scheduled-worker-second"
        ).execute_once()
    finally:
        await engine.dispose()
