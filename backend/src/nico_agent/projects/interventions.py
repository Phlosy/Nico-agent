"""Safe, durable runtime guidance and Project-change escalation."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.conversations.contracts import ConversationTurnCreate
from nico_agent.conversations.service import ConversationService
from nico_agent.database import Database, RunClaim, TenantContext
from nico_agent.domain.errors import DomainConflict, ResourceNotFound
from nico_agent.domain.models import (
    AuditRecord,
    Event,
    Project,
    ProjectMember,
    ProjectSession,
    Run,
    RunIntervention,
    RuntimeSession,
    Task,
)
from nico_agent.domain.states import RunStatus
from nico_agent.projects.contracts import (
    ProjectChangeCreate,
    RunInterventionCreate,
    RunInterventionWithdraw,
)
from nico_agent.projects.service import ProjectCollaborationService
from nico_agent.runtime.contracts import RuntimeIntervention

_ACTIVE_RUNS = {
    RunStatus.PLANNING.value,
    RunStatus.RUNNING.value,
    RunStatus.WAITING_FOR_TOOL.value,
    RunStatus.WAITING_FOR_APPROVAL.value,
    RunStatus.WAITING_FOR_SUBAGENT.value,
    RunStatus.PAUSED.value,
}


class ProjectInterventionService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create_local_guidance(
        self,
        context: TenantContext,
        project_id: UUID,
        session_id: UUID,
        run_id: UUID,
        command: RunInterventionCreate,
        *,
        idempotency_key: str,
    ) -> RunIntervention:
        content = command.content.strip()
        if not content:
            raise DomainConflict("INTERVENTION_EMPTY", "guidance cannot be blank")
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        async with self.database.tenant_transaction(context) as session:
            existing = await session.scalar(
                select(RunIntervention).where(
                    RunIntervention.tenant_id == context.tenant_id,
                    RunIntervention.run_id == run_id,
                    RunIntervention.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if (
                    existing.content_hash != content_hash
                    or existing.expected_run_revision != command.expected_run_revision
                ):
                    raise DomainConflict(
                        "IDEMPOTENCY_CONFLICT",
                        "the intervention idempotency key was reused with different input",
                    )
                return existing
            _, _, task, run = await self._active_run_scope(
                session,
                context,
                project_id,
                session_id,
                run_id,
                for_update=True,
            )
            if run.revision != command.expected_run_revision:
                raise DomainConflict(
                    "REVISION_CONFLICT",
                    "run revision does not match",
                    details={"expected": command.expected_run_revision, "actual": run.revision},
                )
            runtime = await session.scalar(
                select(RuntimeSession).where(
                    RuntimeSession.tenant_id == context.tenant_id,
                    RuntimeSession.run_id == run.id,
                )
            )
            if (
                runtime is None
                or runtime.provider_name != "nico_native"
                or runtime.execution_mode not in {"react", "plan_and_execute"}
                or "interventions" not in runtime.capabilities
            ):
                raise DomainConflict(
                    "INTERVENTION_UNSUPPORTED",
                    "local guidance requires an active Nico native ReAct or Plan Run",
                )
            intervention = RunIntervention(
                tenant_id=context.tenant_id,
                project_id=project_id,
                project_session_id=session_id,
                task_id=task.id,
                run_id=run.id,
                kind="local_guidance",
                content=content,
                content_hash=content_hash,
                expected_run_revision=command.expected_run_revision,
                idempotency_key=idempotency_key,
                created_by=context.actor_id,
            )
            session.add(intervention)
            await session.flush()
            self._record(
                session,
                context,
                intervention,
                "RunInterventionCreated",
                "run.intervention.create",
                {"content_hash": content_hash, "status": intervention.status},
            )
            await session.flush()
            return intervention

    async def list_interventions(
        self,
        context: TenantContext,
        project_id: UUID,
        session_id: UUID,
        run_id: UUID,
    ) -> list[RunIntervention]:
        async with self.database.tenant_transaction(context) as session:
            await self._run_scope(session, context, project_id, session_id, run_id)
            return list(
                await session.scalars(
                    select(RunIntervention)
                    .where(
                        RunIntervention.tenant_id == context.tenant_id,
                        RunIntervention.project_id == project_id,
                        RunIntervention.project_session_id == session_id,
                        RunIntervention.run_id == run_id,
                    )
                    .order_by(RunIntervention.created_at, RunIntervention.id)
                )
            )

    async def withdraw(
        self,
        context: TenantContext,
        project_id: UUID,
        session_id: UUID,
        run_id: UUID,
        intervention_id: UUID,
        command: RunInterventionWithdraw,
    ) -> RunIntervention:
        async with self.database.tenant_transaction(context) as session:
            await self._run_scope(session, context, project_id, session_id, run_id)
            intervention = await session.scalar(
                select(RunIntervention)
                .where(
                    RunIntervention.tenant_id == context.tenant_id,
                    RunIntervention.project_id == project_id,
                    RunIntervention.project_session_id == session_id,
                    RunIntervention.run_id == run_id,
                    RunIntervention.id == intervention_id,
                )
                .with_for_update()
            )
            if intervention is None:
                raise ResourceNotFound("run_intervention", str(intervention_id))
            if intervention.revision != command.expected_intervention_revision:
                raise DomainConflict("REVISION_CONFLICT", "intervention revision does not match")
            if intervention.status != "pending" or intervention.consumed_by is not None:
                raise DomainConflict(
                    "INTERVENTION_NOT_WITHDRAWABLE",
                    "only unfrozen pending guidance can be withdrawn",
                )
            intervention.status = "withdrawn"
            intervention.rejection_reason = command.reason
            intervention.revision += 1
            self._record(
                session,
                context,
                intervention,
                "RunInterventionWithdrawn",
                "run.intervention.withdraw",
                {"status": intervention.status, "reason": command.reason},
            )
            await session.flush()
            return intervention

    async def escalate_project_change(
        self,
        context: TenantContext,
        project_id: UUID,
        session_id: UUID,
        command: ProjectChangeCreate,
        *,
        idempotency_key: str,
    ):
        content = command.content.strip()
        if not content:
            raise DomainConflict("PROJECT_CHANGE_EMPTY", "Project change cannot be blank")
        async with self.database.tenant_transaction(context) as session:
            project, source_session = await self._project_session(
                session, context, project_id, session_id, require_active=True
            )
            lead_session_id = await session.scalar(
                select(ProjectSession.id)
                .join(
                    ProjectMember,
                    (ProjectMember.tenant_id == ProjectSession.tenant_id)
                    & (ProjectMember.id == ProjectSession.project_member_id),
                )
                .where(
                    ProjectSession.tenant_id == context.tenant_id,
                    ProjectSession.project_id == project.id,
                    ProjectSession.status == "active",
                    ProjectMember.role == "lead",
                    ProjectMember.status == "active",
                )
            )
            if lead_session_id is None:
                raise DomainConflict("PROJECT_LEAD_REQUIRED", "the Project has no active Lead")
            source_agent_id = source_session.agent_id
        conversation = await ProjectCollaborationService(
            self.database
        ).resolve_session_conversation(context, project_id, lead_session_id)
        return await ConversationService(self.database).create_turn(
            context,
            conversation.id,
            ConversationTurnCreate(
                user_input=(
                    "Project change request from member session "
                    f"{session_id} (Agent {source_agent_id}):\n\n{content}"
                ),
                idempotency_key=f"project-change:{idempotency_key}",
                max_steps=command.max_steps,
                token_budget=command.token_budget,
                timeout_seconds=command.timeout_seconds,
            ),
        )

    async def freeze_for_boundary(
        self,
        claim: RunClaim,
        *,
        worker_id: str,
        boundary_key: str,
    ) -> tuple[RuntimeIntervention, ...]:
        context = TenantContext(claim.tenant_id, worker_id, claim.lease_token)
        async with self.database.tenant_transaction(context) as session:
            run = await session.scalar(
                select(Run)
                .where(Run.tenant_id == claim.tenant_id, Run.id == claim.run_id)
                .with_for_update()
            )
            if run is None or run.lease_owner != worker_id or run.lease_token != claim.lease_token:
                raise DomainConflict(
                    "RUN_LEASE_LOST", "only the current Run owner may freeze guidance"
                )
            runtime = await session.scalar(
                select(RuntimeSession)
                .where(
                    RuntimeSession.tenant_id == claim.tenant_id,
                    RuntimeSession.run_id == run.id,
                )
                .with_for_update()
            )
            if runtime is None:
                return ()
            state = dict(runtime.provider_state or {})
            stored_freezes = state.get("intervention_freezes", [])
            freezes = list(stored_freezes) if isinstance(stored_freezes, list) else []
            existing = next(
                (
                    item
                    for item in freezes
                    if isinstance(item, dict) and item.get("boundary_key") == boundary_key
                ),
                None,
            )
            if existing is not None:
                return await self._frozen_values(session, context, run.id, boundary_key, existing)
            candidates = list(
                await session.scalars(
                    select(RunIntervention)
                    .where(
                        RunIntervention.tenant_id == claim.tenant_id,
                        RunIntervention.run_id == run.id,
                        RunIntervention.status == "pending",
                        RunIntervention.consumed_by.is_(None),
                    )
                    .order_by(RunIntervention.created_at, RunIntervention.id)
                    .with_for_update(skip_locked=True)
                    .limit(16)
                )
            )
            selected: list[RunIntervention] = []
            total_chars = 0
            for candidate in candidates:
                if selected and total_chars + len(candidate.content) > 16_000:
                    break
                selected.append(candidate)
                total_chars += len(candidate.content)
                if total_chars >= 16_000:
                    break
            if not selected:
                return ()
            for intervention in selected:
                intervention.consumed_by = boundary_key
                intervention.revision += 1
            frozen = {
                "boundary_key": boundary_key,
                "interventions": [
                    {"intervention_id": str(item.id), "content_hash": item.content_hash}
                    for item in selected
                ],
            }
            freezes.append(frozen)
            state["intervention_freezes"] = freezes[-32:]
            runtime.provider_state = state
            runtime.revision += 1
            for intervention in selected:
                self._record(
                    session,
                    context,
                    intervention,
                    "RunInterventionFrozen",
                    "run.intervention.freeze",
                    {"boundary_key": boundary_key, "content_hash": intervention.content_hash},
                    run_id=run.id,
                )
            await session.flush()
            return tuple(
                RuntimeIntervention(
                    intervention_id=item.id,
                    content=item.content,
                    content_hash=item.content_hash,
                    boundary_key=boundary_key,
                )
                for item in selected
            )

    @classmethod
    async def consume_context_in_session(
        cls,
        session: AsyncSession,
        context: TenantContext,
        run: Run,
        effect_metadata: Any,
    ) -> None:
        if not isinstance(effect_metadata, dict):
            return
        refs = effect_metadata.get("interventions", [])
        if not isinstance(refs, list):
            raise ValueError("intervention context metadata must be a list")
        service = cls.__new__(cls)
        for ref in refs:
            if not isinstance(ref, dict):
                raise ValueError("intervention context reference must be an object")
            try:
                intervention_id = UUID(str(ref["intervention_id"]))
            except (KeyError, ValueError) as exc:
                raise ValueError("intervention context reference has an invalid id") from exc
            boundary_key = str(ref.get("boundary_key") or "")
            content_hash = str(ref.get("content_hash") or "")
            intervention = await session.scalar(
                select(RunIntervention)
                .where(
                    RunIntervention.tenant_id == context.tenant_id,
                    RunIntervention.run_id == run.id,
                    RunIntervention.id == intervention_id,
                )
                .with_for_update()
            )
            if intervention is None:
                raise ValueError("context references an unavailable intervention")
            if (
                intervention.content_hash != content_hash
                or intervention.consumed_by != boundary_key
            ):
                raise ValueError("intervention context reference does not match its freeze")
            if intervention.status == "consumed":
                continue
            if intervention.status != "pending":
                raise ValueError("intervention context references a terminal instruction")
            intervention.status = "consumed"
            intervention.consumed_at = datetime.now(UTC)
            intervention.revision += 1
            service._record(
                session,
                context,
                intervention,
                "RunInterventionConsumed",
                "run.intervention.consume",
                {"boundary_key": boundary_key, "content_hash": content_hash},
                run_id=run.id,
            )

    async def _frozen_values(
        self,
        session: AsyncSession,
        context: TenantContext,
        run_id: UUID,
        boundary_key: str,
        frozen: dict[str, Any],
    ) -> tuple[RuntimeIntervention, ...]:
        values: list[RuntimeIntervention] = []
        for ref in frozen.get("interventions", []):
            intervention_id = UUID(str(ref["intervention_id"]))
            intervention = await session.scalar(
                select(RunIntervention).where(
                    RunIntervention.tenant_id == context.tenant_id,
                    RunIntervention.run_id == run_id,
                    RunIntervention.id == intervention_id,
                )
            )
            if (
                intervention is None
                or intervention.content_hash != ref.get("content_hash")
                or intervention.consumed_by != boundary_key
            ):
                raise ValueError("stored intervention freeze is inconsistent")
            values.append(
                RuntimeIntervention(
                    intervention_id=intervention.id,
                    content=intervention.content,
                    content_hash=intervention.content_hash,
                    boundary_key=boundary_key,
                )
            )
        return tuple(values)

    @staticmethod
    async def _active_run_scope(
        session: AsyncSession,
        context: TenantContext,
        project_id: UUID,
        session_id: UUID,
        run_id: UUID,
        *,
        for_update: bool,
    ) -> tuple[Project, ProjectSession, Task, Run]:
        project, project_session, task, run = await ProjectInterventionService._run_scope(
            session, context, project_id, session_id, run_id, for_update=for_update
        )
        if project.status != "active":
            raise DomainConflict("PROJECT_ARCHIVED", "archived Projects are read-only")
        member_status = await session.scalar(
            select(ProjectMember.status).where(
                ProjectMember.tenant_id == context.tenant_id,
                ProjectMember.id == project_session.project_member_id,
            )
        )
        if project_session.status != "active" or member_status != "active":
            raise DomainConflict(
                "PROJECT_MEMBER_INACTIVE", "guidance requires an active Project member Session"
            )
        if run.status not in _ACTIVE_RUNS:
            raise DomainConflict(
                "RUN_NOT_INTERVENABLE", "guidance requires a non-terminal active Run"
            )
        return project, project_session, task, run

    @staticmethod
    async def _run_scope(
        session: AsyncSession,
        context: TenantContext,
        project_id: UUID,
        session_id: UUID,
        run_id: UUID,
        *,
        for_update: bool = False,
    ) -> tuple[Project, ProjectSession, Task, Run]:
        project, project_session = await ProjectInterventionService._project_session(
            session, context, project_id, session_id, require_active=False
        )
        statement = (
            select(Run, Task)
            .join(Task, (Task.tenant_id == Run.tenant_id) & (Task.id == Run.task_id))
            .where(
                Run.tenant_id == context.tenant_id,
                Run.id == run_id,
                Task.project_id == project_id,
                Task.project_session_id == session_id,
            )
        )
        if for_update:
            statement = statement.with_for_update(of=Run)
        row = (await session.execute(statement)).one_or_none()
        if row is None:
            raise ResourceNotFound("run", str(run_id))
        run, task = row
        return project, project_session, task, run

    @staticmethod
    async def _project_session(
        session: AsyncSession,
        context: TenantContext,
        project_id: UUID,
        session_id: UUID,
        *,
        require_active: bool,
    ) -> tuple[Project, ProjectSession]:
        project = await session.scalar(
            select(Project).where(Project.tenant_id == context.tenant_id, Project.id == project_id)
        )
        if project is None:
            raise ResourceNotFound("project", str(project_id))
        managed = project.metadata_json.get("_nico_collaboration", {})
        if not isinstance(managed, dict) or managed.get("managed") is not True:
            raise DomainConflict(
                "PROJECT_NOT_MANAGED", "runtime guidance requires a managed Project"
            )
        project_session = await session.scalar(
            select(ProjectSession).where(
                ProjectSession.tenant_id == context.tenant_id,
                ProjectSession.project_id == project_id,
                ProjectSession.id == session_id,
            )
        )
        if project_session is None:
            raise ResourceNotFound("project_session", str(session_id))
        if require_active:
            if project.status != "active":
                raise DomainConflict("PROJECT_ARCHIVED", "Project Session is read-only")
            member_status = await session.scalar(
                select(ProjectMember.status).where(
                    ProjectMember.tenant_id == context.tenant_id,
                    ProjectMember.id == project_session.project_member_id,
                )
            )
            if project_session.status != "active" or member_status != "active":
                raise DomainConflict(
                    "PROJECT_MEMBER_INACTIVE", "Project member Session is read-only"
                )
        return project, project_session

    @staticmethod
    def _record(
        session: AsyncSession,
        context: TenantContext,
        intervention: RunIntervention,
        event_type: str,
        action: str,
        payload: dict[str, Any],
        *,
        run_id: UUID | None = None,
    ) -> None:
        safe_payload = {
            "project_id": str(intervention.project_id),
            "project_session_id": str(intervention.project_session_id),
            "intervention_id": str(intervention.id),
            **payload,
        }
        session.add(
            Event(
                tenant_id=context.tenant_id,
                event_type=event_type,
                aggregate_type="run_intervention",
                aggregate_id=intervention.id,
                run_id=run_id or intervention.run_id,
                actor_id=context.actor_id,
                payload=safe_payload,
                correlation_id=context.correlation_id,
            )
        )
        session.add(
            AuditRecord(
                tenant_id=context.tenant_id,
                action=action,
                resource_type="run_intervention",
                resource_id=intervention.id,
                actor_id=context.actor_id,
                details=safe_payload,
                correlation_id=context.correlation_id,
            )
        )


class RunInterventionHandler:
    """Lease-bound narrow runtime capability exposed only to Nico native loops."""

    def __init__(
        self,
        service: ProjectInterventionService,
        claim: RunClaim,
        *,
        worker_id: str,
    ) -> None:
        self.service = service
        self.claim = claim
        self.worker_id = worker_id

    async def freeze(self, boundary_key: str) -> tuple[RuntimeIntervention, ...]:
        return await self.service.freeze_for_boundary(
            self.claim,
            worker_id=self.worker_id,
            boundary_key=boundary_key,
        )
