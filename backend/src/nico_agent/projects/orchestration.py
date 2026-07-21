"""Durable, bounded orchestration for managed Project supervision cycles."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.agent_versions import AgentVersionLifecycle
from nico_agent.database import Database, ProjectSupervisionClaim, TenantContext
from nico_agent.domain.errors import DomainConflict, ResourceNotFound
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    Artifact,
    AuditRecord,
    Delegation,
    Event,
    Project,
    ProjectMember,
    ProjectSession,
    ProjectSupervisionCycle,
    Run,
    Task,
    Tenant,
)
from nico_agent.domain.states import ProjectSupervisionStatus, RunStatus, TaskStatus

_TERMINAL_CYCLES = {"completed", "failed", "cancelled"}


def next_cadence_slot(now: datetime, cadence_seconds: int | None) -> datetime | None:
    """Return one bounded future slot; missed intervals are deliberately not replayed."""

    if cadence_seconds is None:
        return None
    if cadence_seconds < 300 or cadence_seconds > 604_800:
        raise ValueError("supervision cadence must be between 300 and 604800 seconds")
    return now + timedelta(seconds=cadence_seconds)


def bounded_narrative(output: dict[str, Any] | None) -> str | None:
    """Extract only the bounded public narrative from a Lead result."""

    if not isinstance(output, dict):
        return None
    content = output.get("content")
    if not isinstance(content, str):
        nested = output.get("result")
        content = nested.get("content") if isinstance(nested, dict) else None
    if not isinstance(content, str) or not content.strip():
        return None
    return content.strip()[:4_000]


def supervision_facts(
    *,
    project_id: str,
    member_statuses: list[str],
    task_statuses: list[str],
    run_statuses: list[str],
    delegation_statuses: list[str],
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a deterministic, safe projection without model-derived authority."""

    members = Counter(member_statuses)
    tasks = Counter(task_statuses)
    runs = Counter(run_statuses)
    delegations = Counter(delegation_statuses)
    total_tasks = sum(tasks.values())
    completed_tasks = tasks["completed"]
    blockers = [
        {"kind": name, "count": count}
        for name, count in (
            ("failed_tasks", tasks["failed"]),
            ("failed_runs", runs["failed"]),
            ("timed_out_runs", runs["timed_out"]),
        )
        if count
    ]
    artifact_refs = sorted(
        (
            {
                "artifact_id": str(item["artifact_id"]),
                "name": str(item["name"]),
                "run_id": str(item["run_id"]),
            }
            for item in artifacts
        ),
        key=lambda item: (item["artifact_id"], item["run_id"]),
    )[:50]
    return {
        "project_id": project_id,
        "progress": {
            "completed_tasks": completed_tasks,
            "total_tasks": total_tasks,
            "percent": int(completed_tasks * 100 / total_tasks) if total_tasks else 0,
        },
        "members": dict(sorted(members.items())),
        "task_statuses": dict(sorted(tasks.items())),
        "run_statuses": dict(sorted(runs.items())),
        "delegation_statuses": dict(sorted(delegations.items())),
        "blockers": blockers,
        "risks": (
            [{"kind": "failed_delegations", "count": delegations["failed"]}]
            if delegations["failed"]
            else []
        ),
        "artifact_refs": artifact_refs,
    }


class ProjectOrchestrationService:
    """Create, materialize, and finalize supervision cycles transactionally."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def create_manual(
        self,
        context: TenantContext,
        project_id: UUID,
        *,
        idempotency_key: str,
    ) -> ProjectSupervisionCycle:
        async with self.database.tenant_transaction(context) as session:
            existing = await session.scalar(
                select(ProjectSupervisionCycle).where(
                    ProjectSupervisionCycle.tenant_id == context.tenant_id,
                    ProjectSupervisionCycle.project_id == project_id,
                    ProjectSupervisionCycle.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                return existing
            project, lead, lead_session = await self._active_lead(
                session, context, project_id, for_update=True
            )
            now = datetime.now(UTC)
            cycle = ProjectSupervisionCycle(
                tenant_id=context.tenant_id,
                project_id=project.id,
                lead_project_member_id=lead.id,
                lead_project_session_id=lead_session.id,
                trigger="manual",
                cadence_slot=now,
                scheduled_for=now,
                idempotency_key=idempotency_key,
            )
            session.add(cycle)
            await session.flush()
            self._record(
                session,
                context,
                cycle,
                "ProjectSupervisionRequested",
                "project.supervision.request",
                {"trigger": cycle.trigger, "scheduled_for": now.isoformat()},
            )
            await session.flush()
            return cycle

    async def update_cadence(
        self,
        context: TenantContext,
        project_id: UUID,
        *,
        cadence_seconds: int | None,
        expected_revision: int,
        reason: str | None,
    ) -> Project:
        async with self.database.tenant_transaction(context) as session:
            project, _, _ = await self._active_lead(
                session, context, project_id, for_update=True
            )
            if project.revision != expected_revision:
                raise DomainConflict(
                    "REVISION_CONFLICT",
                    "project revision does not match",
                    details={"expected": expected_revision, "actual": project.revision},
                )
            now = datetime.now(UTC)
            project.supervision_cadence_seconds = cadence_seconds
            project.next_supervision_at = next_cadence_slot(now, cadence_seconds)
            project.revision += 1
            payload = {
                "cadence_seconds": cadence_seconds,
                "next_supervision_at": (
                    project.next_supervision_at.isoformat()
                    if project.next_supervision_at is not None
                    else None
                ),
                "reason": reason,
                "revision": project.revision,
            }
            session.add(
                Event(
                    tenant_id=context.tenant_id,
                    event_type="ProjectSupervisionCadenceChanged",
                    aggregate_type="project",
                    aggregate_id=project.id,
                    actor_id=context.actor_id,
                    payload=payload,
                    correlation_id=context.correlation_id,
                )
            )
            session.add(
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action="project.supervision.cadence",
                    resource_type="project",
                    resource_id=project.id,
                    actor_id=context.actor_id,
                    details=payload,
                    correlation_id=context.correlation_id,
                )
            )
            await session.flush()
            return project

    async def list_cycles(
        self, context: TenantContext, project_id: UUID
    ) -> list[ProjectSupervisionCycle]:
        async with self.database.tenant_transaction(context) as session:
            project = await session.scalar(
                select(Project).where(
                    Project.tenant_id == context.tenant_id, Project.id == project_id
                )
            )
            if project is None:
                raise ResourceNotFound("project", str(project_id))
            return list(
                await session.scalars(
                    select(ProjectSupervisionCycle)
                    .where(
                        ProjectSupervisionCycle.tenant_id == context.tenant_id,
                        ProjectSupervisionCycle.project_id == project_id,
                    )
                    .order_by(
                        ProjectSupervisionCycle.created_at.desc(),
                        ProjectSupervisionCycle.id.desc(),
                    )
                    .limit(100)
                )
            )

    async def get_cycle(
        self, context: TenantContext, project_id: UUID, cycle_id: UUID
    ) -> ProjectSupervisionCycle:
        async with self.database.tenant_transaction(context) as session:
            cycle = await session.scalar(
                select(ProjectSupervisionCycle).where(
                    ProjectSupervisionCycle.tenant_id == context.tenant_id,
                    ProjectSupervisionCycle.project_id == project_id,
                    ProjectSupervisionCycle.id == cycle_id,
                )
            )
            if cycle is None:
                raise ResourceNotFound("project_supervision_cycle", str(cycle_id))
            return cycle

    async def materialize_claim(
        self, claim: ProjectSupervisionClaim, *, worker_id: str
    ) -> ProjectSupervisionCycle:
        context = TenantContext(claim.tenant_id, worker_id, claim.lease_token)
        async with self.database.tenant_transaction(context) as session:
            cycle_ref = await session.scalar(
                select(ProjectSupervisionCycle).where(
                    ProjectSupervisionCycle.tenant_id == claim.tenant_id,
                    ProjectSupervisionCycle.id == claim.cycle_id,
                )
            )
            if cycle_ref is None:
                raise ResourceNotFound("project_supervision_cycle", str(claim.cycle_id))
            project, lead, lead_session = await self._active_lead(
                session, context, cycle_ref.project_id, for_update=True
            )
            cycle = await session.scalar(
                select(ProjectSupervisionCycle)
                .where(
                    ProjectSupervisionCycle.tenant_id == claim.tenant_id,
                    ProjectSupervisionCycle.id == claim.cycle_id,
                )
                .with_for_update()
            )
            if cycle is None:
                raise ResourceNotFound("project_supervision_cycle", str(claim.cycle_id))
            if cycle.status == ProjectSupervisionStatus.RUNNING.value:
                return cycle
            if (
                cycle.status != ProjectSupervisionStatus.CLAIMED.value
                or cycle.lease_owner != worker_id
                or cycle.lease_token != claim.lease_token
            ):
                raise DomainConflict(
                    "SUPERVISION_LEASE_LOST",
                    "supervision claim is no longer owned",
                )
            if cycle.task_id is not None and cycle.run_id is not None:
                cycle.status = ProjectSupervisionStatus.RUNNING.value
                cycle.started_at = cycle.started_at or datetime.now(UTC)
                cycle.revision += 1
                return cycle
            agent = await session.scalar(
                select(Agent).where(
                    Agent.tenant_id == context.tenant_id,
                    Agent.id == lead.agent_id,
                    Agent.status == "ready",
                )
            )
            version = None
            if agent is not None and agent.current_version_id is not None:
                version = await session.scalar(
                    select(AgentVersion).where(
                        AgentVersion.tenant_id == context.tenant_id,
                        AgentVersion.agent_id == agent.id,
                        AgentVersion.id == agent.current_version_id,
                        AgentVersion.status == "published",
                    )
                )
            tenant = await session.scalar(select(Tenant).where(Tenant.id == context.tenant_id))
            issues = (
                ["Lead Agent must be ready with a published current version"]
                if version is None
                else AgentVersionLifecycle.project_lead_compatibility_issues(
                    version, tenant.settings if tenant is not None else {}
                )
            )
            if agent is None or version is None or issues:
                self._fail_cycle(
                    session,
                    context,
                    cycle,
                    "PROJECT_LEAD_INCOMPATIBLE",
                    "; ".join(issues),
                )
                return cycle

            now = datetime.now(UTC)
            task = Task(
                tenant_id=context.tenant_id,
                project_id=project.id,
                project_session_id=lead_session.id,
                assignee_agent_id=agent.id,
                title=f"Project supervision: {project.name}"[:300],
                input={
                    "objective": (
                        "Review project progress, blockers, risks, artifacts, and next steps."
                    ),
                    "project_id": str(project.id),
                    "supervision_cycle_id": str(cycle.id),
                    "bounded_summary": True,
                },
                acceptance={"structured_facts_authority": "database", "narrative_max_chars": 4000},
                status=TaskStatus.RUNNING.value,
                priority=100,
            )
            session.add(task)
            await session.flush()
            run = Run(
                tenant_id=context.tenant_id,
                task_id=task.id,
                agent_id=agent.id,
                agent_version_id=version.id,
                attempt=1,
                max_steps=min(int(version.budgets.get("max_steps", 32)), 32),
                token_budget=min(int(version.budgets.get("token_budget", 16_000)), 16_000),
                timeout_seconds=min(int(version.budgets.get("timeout_seconds", 900)), 900),
                budgets={"supervision_cycle_id": str(cycle.id)},
            )
            session.add(run)
            await session.flush()
            cycle.lead_project_member_id = lead.id
            cycle.lead_project_session_id = lead_session.id
            cycle.task_id = task.id
            cycle.run_id = run.id
            cycle.status = ProjectSupervisionStatus.RUNNING.value
            cycle.started_at = now
            cycle.lease_expires_at = None
            cycle.heartbeat_at = now
            cycle.revision += 1
            self._record(
                session,
                context,
                cycle,
                "ProjectSupervisionStarted",
                "project.supervision.start",
                {"task_id": str(task.id), "run_id": str(run.id), "lead_agent_id": str(agent.id)},
                run_id=run.id,
            )
            await session.flush()
            return cycle

    @classmethod
    async def finalize_in_session(
        cls,
        session: AsyncSession,
        context: TenantContext,
        run: Run,
        *,
        narrative_output: dict[str, Any] | None,
    ) -> None:
        cycle = await session.scalar(
            select(ProjectSupervisionCycle)
            .where(
                ProjectSupervisionCycle.tenant_id == context.tenant_id,
                ProjectSupervisionCycle.run_id == run.id,
                ProjectSupervisionCycle.status == ProjectSupervisionStatus.RUNNING.value,
            )
            .with_for_update()
        )
        if cycle is None:
            return
        cycle_task = await session.scalar(
            select(Task)
            .where(
                Task.tenant_id == context.tenant_id,
                Task.id == cycle.task_id,
            )
            .with_for_update()
        )
        if cycle_task is not None:
            terminal_task_status = {
                RunStatus.COMPLETED.value: TaskStatus.COMPLETED.value,
                RunStatus.CANCELLED.value: TaskStatus.CANCELLED.value,
            }.get(run.status, TaskStatus.FAILED.value)
            if cycle_task.status != terminal_task_status:
                cycle_task.status = terminal_task_status
                cycle_task.revision += 1
        task_statuses = list(
            await session.scalars(
                select(Task.status).where(
                    Task.tenant_id == context.tenant_id, Task.project_id == cycle.project_id
                )
            )
        )
        run_statuses = list(
            await session.scalars(
                select(Run.status)
                .join(Task, (Task.tenant_id == Run.tenant_id) & (Task.id == Run.task_id))
                .where(
                    Run.tenant_id == context.tenant_id, Task.project_id == cycle.project_id
                )
            )
        )
        member_statuses = list(
            await session.scalars(
                select(ProjectMember.status).where(
                    ProjectMember.tenant_id == context.tenant_id,
                    ProjectMember.project_id == cycle.project_id,
                )
            )
        )
        delegation_statuses = list(
            await session.scalars(
                select(Delegation.status)
                .join(
                    Task,
                    (Task.tenant_id == Delegation.tenant_id)
                    & (Task.id == Delegation.child_task_id),
                )
                .where(
                    Delegation.tenant_id == context.tenant_id,
                    Task.project_id == cycle.project_id,
                )
            )
        )
        artifacts = list(
            (
                await session.execute(
                    select(Artifact.id, Artifact.name, Artifact.owner_run_id)
                    .where(
                        Artifact.tenant_id == context.tenant_id,
                        Artifact.project_id == cycle.project_id,
                        Artifact.status == "available",
                    )
                    .order_by(Artifact.created_at.desc(), Artifact.id.desc())
                    .limit(50)
                )
            ).mappings()
        )
        cycle.metrics = supervision_facts(
            project_id=str(cycle.project_id),
            member_statuses=member_statuses,
            task_statuses=task_statuses,
            run_statuses=run_statuses,
            delegation_statuses=delegation_statuses,
            artifacts=[
                {"artifact_id": row["id"], "name": row["name"], "run_id": row["owner_run_id"]}
                for row in artifacts
            ],
        )
        cycle.narrative_summary = bounded_narrative(narrative_output)
        cycle.status = {
            RunStatus.COMPLETED.value: ProjectSupervisionStatus.COMPLETED.value,
            RunStatus.CANCELLED.value: ProjectSupervisionStatus.CANCELLED.value,
        }.get(run.status, ProjectSupervisionStatus.FAILED.value)
        cycle.error = run.error if cycle.status == ProjectSupervisionStatus.FAILED.value else None
        cycle.ended_at = run.ended_at or datetime.now(UTC)
        cycle.revision += 1
        service = cls.__new__(cls)
        service._record(
            session,
            context,
            cycle,
            "ProjectSupervisionCompleted",
            "project.supervision.complete",
            {"status": cycle.status, "metrics": cycle.metrics},
            run_id=run.id,
        )

    @staticmethod
    async def _active_lead(
        session: AsyncSession,
        context: TenantContext,
        project_id: UUID,
        *,
        for_update: bool,
    ) -> tuple[Project, ProjectMember, ProjectSession]:
        project_query = select(Project).where(
            Project.tenant_id == context.tenant_id, Project.id == project_id
        )
        if for_update:
            project_query = project_query.with_for_update()
        project = await session.scalar(project_query)
        if project is None:
            raise ResourceNotFound("project", str(project_id))
        managed = project.metadata_json.get("_nico_collaboration", {})
        if project.status != "active":
            raise DomainConflict("PROJECT_ARCHIVED", "archived Projects are read-only")
        if not isinstance(managed, dict) or managed.get("managed") is not True:
            raise DomainConflict("PROJECT_NOT_MANAGED", "supervision requires a managed Project")
        lead = await session.scalar(
            select(ProjectMember).where(
                ProjectMember.tenant_id == context.tenant_id,
                ProjectMember.project_id == project.id,
                ProjectMember.role == "lead",
                ProjectMember.status == "active",
            )
        )
        if lead is None:
            raise DomainConflict("PROJECT_LEAD_REQUIRED", "the Project has no active Lead")
        lead_session = await session.scalar(
            select(ProjectSession).where(
                ProjectSession.tenant_id == context.tenant_id,
                ProjectSession.project_member_id == lead.id,
                ProjectSession.status == "active",
            )
        )
        if lead_session is None:
            raise DomainConflict("PROJECT_LEAD_REQUIRED", "the active Lead has no active Session")
        return project, lead, lead_session

    def _fail_cycle(
        self,
        session: AsyncSession,
        context: TenantContext,
        cycle: ProjectSupervisionCycle,
        code: str,
        message: str,
    ) -> None:
        cycle.status = ProjectSupervisionStatus.FAILED.value
        cycle.error = {"code": code, "message": message}
        cycle.ended_at = datetime.now(UTC)
        cycle.revision += 1
        self._record(
            session,
            context,
            cycle,
            "ProjectSupervisionFailed",
            "project.supervision.fail",
            {"code": code, "message": message},
        )

    @staticmethod
    def _record(
        session: AsyncSession,
        context: TenantContext,
        cycle: ProjectSupervisionCycle,
        event_type: str,
        action: str,
        payload: dict[str, Any],
        *,
        run_id: UUID | None = None,
    ) -> None:
        safe_payload = {"project_id": str(cycle.project_id), "cycle_id": str(cycle.id), **payload}
        session.add(
            Event(
                tenant_id=context.tenant_id,
                event_type=event_type,
                aggregate_type="project_supervision_cycle",
                aggregate_id=cycle.id,
                run_id=run_id,
                actor_id=context.actor_id,
                payload=safe_payload,
                correlation_id=context.correlation_id,
            )
        )
        session.add(
            AuditRecord(
                tenant_id=context.tenant_id,
                action=action,
                resource_type="project_supervision_cycle",
                resource_id=cycle.id,
                actor_id=context.actor_id,
                details=safe_payload,
                correlation_id=context.correlation_id,
            )
        )
