from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
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
    RunIntervention,
    Task,
    Tenant,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with the Compose dependencies running",
)


async def _seed_project_scope(database: Database, label: str) -> dict:
    suffix = uuid4().hex[:10]
    async with database.admin_transaction() as session:
        tenant = Tenant(name=f"Collaboration {label} {suffix}", slug=f"collab-{label}-{suffix}")
        session.add(tenant)
        await session.flush()
        project = Project(
            tenant_id=tenant.id,
            name=f"project-{suffix}",
            kind="shared",
            supervision_cadence_seconds=3600,
        )
        other_project = Project(
            tenant_id=tenant.id,
            name=f"other-{suffix}",
            kind="shared",
        )
        lead = Agent(
            tenant_id=tenant.id,
            name=f"lead-{suffix}",
            display_name="Lead",
            status="ready",
        )
        member = Agent(
            tenant_id=tenant.id,
            name=f"member-{suffix}",
            display_name="Member",
            status="ready",
        )
        session.add_all([project, other_project, lead, member])
        await session.flush()
        versions = []
        for agent in (lead, member):
            version = AgentVersion(
                tenant_id=tenant.id,
                agent_id=agent.id,
                version=1,
                status="published",
                role="project-test",
                mandate="Exercise project collaboration persistence",
                content_hash=uuid4().hex * 2,
            )
            session.add(version)
            versions.append(version)
        await session.flush()
        lead.current_version_id = versions[0].id
        member.current_version_id = versions[1].id
        lead_member = ProjectMember(
            tenant_id=tenant.id,
            project_id=project.id,
            agent_id=lead.id,
            role="lead",
            status="active",
            created_by="persistence-test",
        )
        regular_member = ProjectMember(
            tenant_id=tenant.id,
            project_id=project.id,
            agent_id=member.id,
            role="member",
            status="active",
            created_by="persistence-test",
        )
        session.add_all([lead_member, regular_member])
        await session.flush()
        lead_session = ProjectSession(
            tenant_id=tenant.id,
            project_id=project.id,
            project_member_id=lead_member.id,
            agent_id=lead.id,
            status="active",
        )
        member_session = ProjectSession(
            tenant_id=tenant.id,
            project_id=project.id,
            project_member_id=regular_member.id,
            agent_id=member.id,
            status="active",
        )
        session.add_all([lead_session, member_session])
        await session.flush()
        task = Task(
            tenant_id=tenant.id,
            project_id=project.id,
            project_session_id=member_session.id,
            assignee_agent_id=member.id,
            title="Member work",
            status="running",
        )
        session.add(task)
        await session.flush()
        run = Run(
            tenant_id=tenant.id,
            task_id=task.id,
            agent_id=member.id,
            agent_version_id=versions[1].id,
            attempt=1,
            status="running",
        )
        session.add(run)
        await session.flush()
        return {
            "tenant_id": tenant.id,
            "project_id": project.id,
            "other_project_id": other_project.id,
            "lead_agent_id": lead.id,
            "member_agent_id": member.id,
            "lead_member_id": lead_member.id,
            "member_id": regular_member.id,
            "lead_session_id": lead_session.id,
            "member_session_id": member_session.id,
            "task_id": task.id,
            "run_id": run.id,
        }


@pytest.mark.asyncio
async def test_personal_project_identity_and_active_lead_are_database_enforced() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        seeded = await _seed_project_scope(database, "identity")
        with pytest.raises(IntegrityError):
            async with database.admin_transaction() as session:
                session.add(
                    ProjectMember(
                        tenant_id=seeded["tenant_id"],
                        project_id=seeded["project_id"],
                        agent_id=seeded["member_agent_id"],
                        role="lead",
                        status="active",
                        created_by="persistence-test",
                    )
                )
                await session.flush()

        owner = f"actor-{uuid4()}"
        async with database.admin_transaction() as session:
            session.add(
                Project(
                    tenant_id=seeded["tenant_id"],
                    name=f"personal-{uuid4()}",
                    kind="personal",
                    owner_actor_id=owner,
                )
            )
            await session.flush()
        with pytest.raises(IntegrityError):
            async with database.admin_transaction() as session:
                session.add(
                    Project(
                        tenant_id=seeded["tenant_id"],
                        name=f"personal-{uuid4()}",
                        kind="personal",
                        owner_actor_id=owner,
                    )
                )
                await session.flush()
        async with database.admin_transaction() as session:
            session.add(
                Project(
                    tenant_id=seeded["tenant_id"],
                    name=f"personal-{uuid4()}",
                    kind="personal",
                    owner_actor_id=f"other-{owner}",
                )
            )
            await session.flush()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_session_scope_cycle_idempotency_and_terminal_guards_are_enforced() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        seeded = await _seed_project_scope(database, "guards")
        with pytest.raises(IntegrityError):
            async with database.admin_transaction() as session:
                session.add(
                    ProjectSession(
                        tenant_id=seeded["tenant_id"],
                        project_id=seeded["other_project_id"],
                        project_member_id=seeded["member_id"],
                        agent_id=seeded["member_agent_id"],
                        status="active",
                    )
                )
                await session.flush()

        slot = datetime.now(UTC).replace(microsecond=0)
        async with database.admin_transaction() as session:
            session.add(
                ProjectSupervisionCycle(
                    tenant_id=seeded["tenant_id"],
                    project_id=seeded["project_id"],
                    lead_project_member_id=seeded["lead_member_id"],
                    lead_project_session_id=seeded["lead_session_id"],
                    trigger="scheduled",
                    cadence_slot=slot,
                    scheduled_for=slot,
                    status="completed",
                    idempotency_key="scheduled-slot",
                )
            )
            await session.flush()
        with pytest.raises(IntegrityError):
            async with database.admin_transaction() as session:
                session.add(
                    ProjectSupervisionCycle(
                        tenant_id=seeded["tenant_id"],
                        project_id=seeded["project_id"],
                        lead_project_member_id=seeded["lead_member_id"],
                        lead_project_session_id=seeded["lead_session_id"],
                        trigger="scheduled",
                        cadence_slot=slot,
                        scheduled_for=slot,
                        idempotency_key="other-key",
                    )
                )
                await session.flush()
        with pytest.raises(DBAPIError):
            async with database.admin_transaction() as session:
                await session.execute(
                    text(
                        "UPDATE project_supervision_cycles SET status = 'running' "
                        "WHERE tenant_id = :tenant_id AND idempotency_key = 'scheduled-slot'"
                    ),
                    {"tenant_id": seeded["tenant_id"]},
                )

        intervention_id = uuid4()
        async with database.admin_transaction() as session:
            session.add(
                RunIntervention(
                    id=intervention_id,
                    tenant_id=seeded["tenant_id"],
                    project_id=seeded["project_id"],
                    project_session_id=seeded["member_session_id"],
                    task_id=seeded["task_id"],
                    run_id=seeded["run_id"],
                    kind="local_guidance",
                    content="Check the failing assertion first",
                    content_hash="a" * 64,
                    expected_run_revision=1,
                    status="consumed",
                    consumed_at=datetime.now(UTC),
                    idempotency_key="guidance-1",
                    created_by="persistence-test",
                )
            )
            await session.flush()
        with pytest.raises(DBAPIError):
            async with database.admin_transaction() as session:
                await session.execute(
                    text("UPDATE run_interventions SET status = 'pending' WHERE id = :id"),
                    {"id": intervention_id},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_collaboration_tables_are_force_rls_isolated() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        first = await _seed_project_scope(database, "rls-first")
        second = await _seed_project_scope(database, "rls-second")
        context = TenantContext(first["tenant_id"], "persistence-test", uuid4())
        async with database.tenant_transaction(context) as session:
            visible = set((await session.scalars(select(ProjectSession.id))).all())
        assert first["member_session_id"] in visible
        assert second["member_session_id"] not in visible
    finally:
        await engine.dispose()
