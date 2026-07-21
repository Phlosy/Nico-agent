from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import Settings
from nico_agent.database import Database, RunClaim, TenantContext
from nico_agent.domain.errors import DomainConflict
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    Project,
    ProjectMember,
    ProjectSession,
    Run,
    RuntimeSession,
    Task,
    Tenant,
)
from nico_agent.projects.contracts import (
    ProjectChangeCreate,
    RunInterventionCreate,
    RunInterventionWithdraw,
)
from nico_agent.projects.interventions import ProjectInterventionService

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


async def _seed(database: Database) -> dict[str, object]:
    suffix = uuid4().hex[:10]
    lease_token = uuid4()
    worker_id = f"intervention-worker-{suffix}"
    async with database.admin_transaction() as session:
        tenant = Tenant(name=f"Intervention {suffix}", slug=f"intervention-{suffix}")
        session.add(tenant)
        await session.flush()
        project = Project(
            tenant_id=tenant.id,
            name=f"intervention-project-{suffix}",
            kind="shared",
            metadata_json={"_nico_collaboration": {"managed": True}},
        )
        lead = Agent(
            tenant_id=tenant.id,
            name=f"intervention-lead-{suffix}",
            display_name="Intervention Lead",
            status="ready",
        )
        member = Agent(
            tenant_id=tenant.id,
            name=f"intervention-member-{suffix}",
            display_name="Intervention Member",
            status="ready",
        )
        session.add_all([project, lead, member])
        await session.flush()
        versions: list[AgentVersion] = []
        for index, agent in enumerate((lead, member)):
            version = AgentVersion(
                tenant_id=tenant.id,
                agent_id=agent.id,
                version=1,
                status="published",
                role="lead" if index == 0 else "member",
                mandate="Complete bounded Project work",
                runtime_provider="nico_native",
                execution_mode="plan_and_execute" if index == 0 else "react",
                content_hash=("a" if index == 0 else "b") * 64,
            )
            session.add(version)
            await session.flush()
            agent.current_version_id = version.id
            versions.append(version)
        memberships: list[ProjectMember] = []
        project_sessions: list[ProjectSession] = []
        for index, agent in enumerate((lead, member)):
            membership = ProjectMember(
                tenant_id=tenant.id,
                project_id=project.id,
                agent_id=agent.id,
                role="lead" if index == 0 else "member",
                status="active",
                created_by="intervention-test",
            )
            session.add(membership)
            await session.flush()
            project_session = ProjectSession(
                tenant_id=tenant.id,
                project_id=project.id,
                project_member_id=membership.id,
                agent_id=agent.id,
                status="active",
            )
            session.add(project_session)
            await session.flush()
            memberships.append(membership)
            project_sessions.append(project_session)
        task = Task(
            tenant_id=tenant.id,
            project_id=project.id,
            project_session_id=project_sessions[1].id,
            assignee_agent_id=member.id,
            title="Active member task",
            status="running",
        )
        session.add(task)
        await session.flush()
        now = datetime.now(UTC)
        run = Run(
            tenant_id=tenant.id,
            task_id=task.id,
            agent_id=member.id,
            agent_version_id=versions[1].id,
            attempt=1,
            status="running",
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
            capabilities=["checkpoint", "interventions", "platform_tools"],
            provider_state={"safe": "unchanged"},
            tool_policy_snapshot={"allow": ["file.read@1.0.0"]},
            coordination_policy_snapshot={"allowed_agent_version_ids": []},
        )
        session.add(runtime)
        await session.flush()
        return {
            "context": TenantContext(tenant.id, "intervention-operator", uuid4()),
            "project_id": project.id,
            "member_session_id": project_sessions[1].id,
            "lead_session_id": project_sessions[0].id,
            "run_id": run.id,
            "run_revision": run.revision,
            "worker_id": worker_id,
            "lease_token": lease_token,
        }


@pytest.mark.asyncio
async def test_guidance_freeze_consume_recovery_and_withdraw_are_deterministic() -> None:
    engine = create_async_engine(Settings(environment="test", _env_file=None).resolved_database_url)
    database = Database(engine)
    try:
        seeded = await _seed(database)
        service = ProjectInterventionService(database)
        command = RunInterventionCreate(
            content="Check the failing edge case first.",
            expected_run_revision=seeded["run_revision"],
        )
        created = await service.create_local_guidance(
            seeded["context"],
            seeded["project_id"],
            seeded["member_session_id"],
            seeded["run_id"],
            command,
            idempotency_key="guidance-one",
        )
        replay = await service.create_local_guidance(
            seeded["context"],
            seeded["project_id"],
            seeded["member_session_id"],
            seeded["run_id"],
            command,
            idempotency_key="guidance-one",
        )
        assert replay.id == created.id

        withdrawn = await service.create_local_guidance(
            seeded["context"],
            seeded["project_id"],
            seeded["member_session_id"],
            seeded["run_id"],
            RunInterventionCreate(
                content="This instruction will be withdrawn.",
                expected_run_revision=seeded["run_revision"],
            ),
            idempotency_key="guidance-withdraw",
        )
        withdrawn = await service.withdraw(
            seeded["context"],
            seeded["project_id"],
            seeded["member_session_id"],
            seeded["run_id"],
            withdrawn.id,
            RunInterventionWithdraw(
                expected_intervention_revision=withdrawn.revision,
                reason="No longer relevant",
            ),
        )
        assert withdrawn.status == "withdrawn"

        claim = RunClaim(
            run_id=seeded["run_id"],
            tenant_id=seeded["context"].tenant_id,
            lease_token=seeded["lease_token"],
            previous_status="running",
        )
        frozen = await service.freeze_for_boundary(
            claim, worker_id=seeded["worker_id"], boundary_key="react:1"
        )
        assert [item.intervention_id for item in frozen] == [created.id]
        replayed_freeze = await service.freeze_for_boundary(
            claim, worker_id=seeded["worker_id"], boundary_key="react:1"
        )
        assert replayed_freeze == frozen

        metadata = {
            "interventions": [
                {
                    "intervention_id": str(frozen[0].intervention_id),
                    "content_hash": frozen[0].content_hash,
                    "boundary_key": frozen[0].boundary_key,
                }
            ]
        }
        async with database.tenant_transaction(seeded["context"]) as session:
            run = await session.get(Run, seeded["run_id"])
            assert run is not None
            await ProjectInterventionService.consume_context_in_session(
                session, seeded["context"], run, metadata
            )
        consumed = (
            await service.list_interventions(
                seeded["context"],
                seeded["project_id"],
                seeded["member_session_id"],
                seeded["run_id"],
            )
        )[0]
        assert consumed.status == "consumed"
        assert consumed.consumed_at is not None
        assert await service.freeze_for_boundary(
            claim, worker_id=seeded["worker_id"], boundary_key="react:1"
        ) == frozen
        with pytest.raises(DomainConflict) as terminal:
            await service.withdraw(
                seeded["context"],
                seeded["project_id"],
                seeded["member_session_id"],
                seeded["run_id"],
                consumed.id,
                RunInterventionWithdraw(
                    expected_intervention_revision=consumed.revision
                ),
            )
        assert terminal.value.code == "INTERVENTION_NOT_WITHDRAWABLE"

        async with database.admin_transaction() as session:
            runtime = await session.scalar(
                select(RuntimeSession).where(RuntimeSession.run_id == seeded["run_id"])
            )
            assert runtime is not None
            assert runtime.provider_state["safe"] == "unchanged"
            assert runtime.tool_policy_snapshot == {"allow": ["file.read@1.0.0"]}
            assert runtime.coordination_policy_snapshot == {
                "allowed_agent_version_ids": []
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_change_targets_lead_and_unsupported_runtime_fails_closed() -> None:
    engine = create_async_engine(Settings(environment="test", _env_file=None).resolved_database_url)
    database = Database(engine)
    try:
        seeded = await _seed(database)
        service = ProjectInterventionService(database)
        accepted = await service.escalate_project_change(
            seeded["context"],
            seeded["project_id"],
            seeded["member_session_id"],
            ProjectChangeCreate(content="Re-scope delivery around the new API constraint."),
            idempotency_key="project-change-one",
        )
        assert accepted.status.value == "queued"
        async with database.admin_transaction() as session:
            lead_task = await session.get(Task, accepted.task_id)
            assert lead_task is not None
            assert lead_task.project_session_id == seeded["lead_session_id"]
            runtime = await session.scalar(
                select(RuntimeSession).where(RuntimeSession.run_id == seeded["run_id"])
            )
            assert runtime is not None
            runtime.execution_mode = "direct"
        with pytest.raises(DomainConflict) as unsupported:
            await service.create_local_guidance(
                seeded["context"],
                seeded["project_id"],
                seeded["member_session_id"],
                seeded["run_id"],
                RunInterventionCreate(
                    content="Must be rejected for Direct mode.",
                    expected_run_revision=seeded["run_revision"],
                ),
                idempotency_key="unsupported-guidance",
            )
        assert unsupported.value.code == "INTERVENTION_UNSUPPORTED"

        async with database.admin_transaction() as session:
            project = await session.get(Project, seeded["project_id"])
            assert project is not None
            project.status = "archived"
        with pytest.raises(DomainConflict) as archived:
            await service.escalate_project_change(
                seeded["context"],
                seeded["project_id"],
                seeded["member_session_id"],
                ProjectChangeCreate(content="Too late"),
                idempotency_key="archived-change",
            )
        assert archived.value.code == "PROJECT_ARCHIVED"
    finally:
        await engine.dispose()
