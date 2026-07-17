"""Transactional application service for Goal C control-plane primitives."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.api_schemas import (
    AgentClone,
    AgentCreate,
    AgentPatch,
    AgentVersionCreate,
    ProjectCreate,
    ProjectPatch,
    RunCreate,
    RunStepCreate,
    RunStepTransition,
    RunTransition,
    TaskCreate,
    TaskTransition,
    TenantCreate,
)
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainConflict, ResourceNotFound
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    AuditRecord,
    Event,
    Project,
    Run,
    RunStep,
    RuntimeSession,
    Task,
    Tenant,
)
from nico_agent.domain.states import (
    AGENT_TRANSITIONS,
    AGENT_VERSION_TRANSITIONS,
    PROJECT_TRANSITIONS,
    RUN_STEP_TRANSITIONS,
    RUN_TRANSITIONS,
    TASK_TRANSITIONS,
    AgentStatus,
    AgentVersionStatus,
    ProjectStatus,
    RunStatus,
    RunStepStatus,
    TaskStatus,
    require_revision,
    transition_state,
)
from nico_agent.runtime.contracts import RuntimeTrajectory


class ControlPlaneService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def bootstrap_tenant(
        self, command: TenantCreate, *, actor_id: str, correlation_id: UUID
    ) -> Tenant:
        async with self.database.admin_transaction() as session:
            tenant = Tenant(
                name=command.name,
                slug=command.slug,
                settings=command.settings,
                limits=command.limits,
            )
            session.add(tenant)
            await session.flush()
            context = TenantContext(tenant.id, actor_id, correlation_id)
            self._record(
                session,
                context,
                event_type="TenantCreated",
                aggregate_type="tenant",
                aggregate_id=tenant.id,
                action="tenant.create",
                payload={"slug": tenant.slug},
            )
            await session.flush()
            return tenant

    async def create_project(self, context: TenantContext, command: ProjectCreate) -> Project:
        async with self.database.tenant_transaction(context) as session:
            project = Project(
                tenant_id=context.tenant_id,
                name=command.name,
                description=command.description,
                metadata_json=command.metadata,
            )
            session.add(project)
            await session.flush()
            self._record(
                session,
                context,
                event_type="ProjectCreated",
                aggregate_type="project",
                aggregate_id=project.id,
                action="project.create",
                payload={"name": project.name},
            )
            await session.flush()
            return project

    async def list_projects(self, context: TenantContext) -> list[Project]:
        async with self.database.tenant_transaction(context) as session:
            return list(
                await session.scalars(
                    select(Project)
                    .where(Project.tenant_id == context.tenant_id)
                    .order_by(Project.created_at, Project.id)
                )
            )

    async def get_project(self, context: TenantContext, project_id: UUID) -> Project:
        async with self.database.tenant_transaction(context) as session:
            return await self._project(session, context, project_id)

    async def update_project(
        self, context: TenantContext, project_id: UUID, command: ProjectPatch
    ) -> Project:
        async with self.database.tenant_transaction(context) as session:
            project = await self._project(session, context, project_id, for_update=True)
            require_revision("project", expected=command.expected_revision, actual=project.revision)
            if "name" in command.model_fields_set and command.name is not None:
                project.name = command.name
            if "description" in command.model_fields_set:
                project.description = command.description
            if "metadata" in command.model_fields_set and command.metadata is not None:
                project.metadata_json = command.metadata
            project.revision += 1
            self._record_change(session, context, project, "ProjectUpdated", "project.update")
            await session.flush()
            return project

    async def archive_project(
        self, context: TenantContext, project_id: UUID, *, expected_revision: int
    ) -> Project:
        async with self.database.tenant_transaction(context) as session:
            project = await self._project(session, context, project_id, for_update=True)
            require_revision("project", expected=expected_revision, actual=project.revision)
            project.status = transition_state(
                "project",
                ProjectStatus(project.status),
                ProjectStatus.ARCHIVED,
                PROJECT_TRANSITIONS,
            ).value
            project.revision += 1
            self._record_change(session, context, project, "ProjectArchived", "project.archive")
            await session.flush()
            return project

    async def create_agent(self, context: TenantContext, command: AgentCreate) -> Agent:
        async with self.database.tenant_transaction(context) as session:
            agent = Agent(
                tenant_id=context.tenant_id,
                name=command.name,
                display_name=command.display_name,
                description=command.description,
            )
            session.add(agent)
            await session.flush()
            self._record_change(session, context, agent, "AgentCreated", "agent.create")
            await session.flush()
            return agent

    async def list_agents(self, context: TenantContext) -> list[Agent]:
        async with self.database.tenant_transaction(context) as session:
            return list(
                await session.scalars(
                    select(Agent)
                    .where(Agent.tenant_id == context.tenant_id)
                    .order_by(Agent.created_at, Agent.id)
                )
            )

    async def get_agent(self, context: TenantContext, agent_id: UUID) -> Agent:
        async with self.database.tenant_transaction(context) as session:
            return await self._agent(session, context, agent_id)

    async def update_agent(
        self, context: TenantContext, agent_id: UUID, command: AgentPatch
    ) -> Agent:
        async with self.database.tenant_transaction(context) as session:
            agent = await self._agent(session, context, agent_id, for_update=True)
            require_revision("agent", expected=command.expected_revision, actual=agent.revision)
            if "display_name" in command.model_fields_set and command.display_name is not None:
                agent.display_name = command.display_name
            if "description" in command.model_fields_set:
                agent.description = command.description
            agent.revision += 1
            self._record_change(session, context, agent, "AgentUpdated", "agent.update")
            await session.flush()
            return agent

    async def clone_agent(
        self, context: TenantContext, source_id: UUID, command: AgentClone
    ) -> Agent:
        async with self.database.tenant_transaction(context) as session:
            source = await self._agent(session, context, source_id)
            clone = Agent(
                tenant_id=context.tenant_id,
                name=command.name,
                display_name=command.display_name,
                description=source.description,
            )
            session.add(clone)
            await session.flush()
            if source.current_version_id is not None:
                version = await self._agent_version(
                    session, context, source.id, source.current_version_id
                )
                session.add(self._copy_version(version, clone.id, version=1))
            self._record(
                session,
                context,
                event_type="AgentCloned",
                aggregate_type="agent",
                aggregate_id=clone.id,
                action="agent.clone",
                payload={"source_agent_id": str(source.id)},
            )
            await session.flush()
            return clone

    async def archive_agent(
        self, context: TenantContext, agent_id: UUID, *, expected_revision: int
    ) -> Agent:
        return await self._transition_agent(
            context,
            agent_id,
            expected_revision=expected_revision,
            target=AgentStatus.ARCHIVED,
            event_type="AgentArchived",
            action="agent.archive",
        )

    async def restore_agent(
        self, context: TenantContext, agent_id: UUID, *, expected_revision: int
    ) -> Agent:
        async with self.database.tenant_transaction(context) as session:
            agent = await self._agent(session, context, agent_id, for_update=True)
            if agent.current_version_id is None:
                raise DomainConflict(
                    "AGENT_VERSION_REQUIRED",
                    "an archived agent needs a published version before it can be restored",
                )
            require_revision("agent", expected=expected_revision, actual=agent.revision)
            agent.status = transition_state(
                "agent", AgentStatus(agent.status), AgentStatus.READY, AGENT_TRANSITIONS
            ).value
            agent.revision += 1
            self._record_change(session, context, agent, "AgentRestored", "agent.restore")
            await session.flush()
            return agent

    async def delete_agent(
        self, context: TenantContext, agent_id: UUID, *, expected_revision: int
    ) -> None:
        async with self.database.tenant_transaction(context) as session:
            agent = await self._agent(session, context, agent_id, for_update=True)
            require_revision("agent", expected=expected_revision, actual=agent.revision)
            if AgentStatus(agent.status) is not AgentStatus.DRAFT:
                raise DomainConflict(
                    "RESOURCE_IN_USE", "only an unreferenced draft agent can be physically deleted"
                )
            version_count = await session.scalar(
                select(func.count())
                .select_from(AgentVersion)
                .where(AgentVersion.agent_id == agent.id)
            )
            task_count = await session.scalar(
                select(func.count()).select_from(Task).where(Task.assignee_agent_id == agent.id)
            )
            run_count = await session.scalar(
                select(func.count()).select_from(Run).where(Run.agent_id == agent.id)
            )
            reference_count = (version_count or 0) + (task_count or 0) + (run_count or 0)
            if reference_count:
                raise DomainConflict(
                    "RESOURCE_IN_USE", "only an unreferenced draft agent can be physically deleted"
                )
            await session.execute(
                delete(Agent).where(
                    Agent.tenant_id == context.tenant_id,
                    Agent.id == agent.id,
                )
            )
            self._record(
                session,
                context,
                event_type="AgentDeleted",
                aggregate_type="agent",
                aggregate_id=agent.id,
                action="agent.delete",
                payload={"name": agent.name},
            )

    async def create_agent_version(
        self, context: TenantContext, agent_id: UUID, command: AgentVersionCreate
    ) -> AgentVersion:
        async with self.database.tenant_transaction(context) as session:
            agent = await self._agent(session, context, agent_id, for_update=True)
            if AgentStatus(agent.status) is AgentStatus.ARCHIVED:
                raise DomainConflict(
                    "AGENT_ARCHIVED", "restore the agent before creating a new version"
                )
            latest = await session.scalar(
                select(func.max(AgentVersion.version)).where(
                    AgentVersion.tenant_id == context.tenant_id,
                    AgentVersion.agent_id == agent.id,
                )
            )
            version = AgentVersion(
                tenant_id=context.tenant_id,
                agent_id=agent.id,
                version=(latest or 0) + 1,
                role=command.role,
                mandate=command.mandate,
                boundaries=command.boundaries,
                long_term_goal=command.long_term_goal,
                current_goal=command.current_goal,
                model_config_json=command.model_config_data,
                tool_policy=command.tool_policy,
                memory_policy=command.memory_policy,
                skill_policy=command.skill_policy,
                plugin_refs=command.plugin_refs,
                budgets=command.budgets,
                run_config=command.run_config,
                content_hash=self._version_hash(command),
            )
            session.add(version)
            await session.flush()
            self._record(
                session,
                context,
                event_type="AgentVersionCreated",
                aggregate_type="agent_version",
                aggregate_id=version.id,
                action="agent_version.create",
                payload={"agent_id": str(agent.id), "version": version.version},
            )
            await session.flush()
            return version

    async def list_agent_versions(
        self, context: TenantContext, agent_id: UUID
    ) -> list[AgentVersion]:
        async with self.database.tenant_transaction(context) as session:
            await self._agent(session, context, agent_id)
            return list(
                await session.scalars(
                    select(AgentVersion)
                    .where(
                        AgentVersion.tenant_id == context.tenant_id,
                        AgentVersion.agent_id == agent_id,
                    )
                    .order_by(AgentVersion.version)
                )
            )

    async def publish_agent_version(
        self,
        context: TenantContext,
        agent_id: UUID,
        version_id: UUID,
        *,
        expected_revision: int,
    ) -> Agent:
        async with self.database.tenant_transaction(context) as session:
            agent = await self._agent(session, context, agent_id, for_update=True)
            require_revision("agent", expected=expected_revision, actual=agent.revision)
            if AgentStatus(agent.status) is AgentStatus.ARCHIVED:
                raise DomainConflict("AGENT_ARCHIVED", "restore the agent before publishing")
            version = await self._agent_version(
                session, context, agent.id, version_id, for_update=True
            )
            version.status = transition_state(
                "agent_version",
                AgentVersionStatus(version.status),
                AgentVersionStatus.PUBLISHED,
                AGENT_VERSION_TRANSITIONS,
            ).value
            if agent.current_version_id is not None:
                current = await self._agent_version(
                    session,
                    context,
                    agent.id,
                    agent.current_version_id,
                    for_update=True,
                )
                current.status = transition_state(
                    "agent_version",
                    AgentVersionStatus(current.status),
                    AgentVersionStatus.SUPERSEDED,
                    AGENT_VERSION_TRANSITIONS,
                ).value
            agent.current_version_id = version.id
            if AgentStatus(agent.status) in {AgentStatus.DRAFT, AgentStatus.ERROR}:
                agent.status = transition_state(
                    "agent", AgentStatus(agent.status), AgentStatus.READY, AGENT_TRANSITIONS
                ).value
            agent.revision += 1
            self._record(
                session,
                context,
                event_type="AgentVersionPublished",
                aggregate_type="agent",
                aggregate_id=agent.id,
                action="agent_version.publish",
                payload={"version_id": str(version.id), "version": version.version},
            )
            await session.flush()
            return agent

    async def rollback_agent_version(
        self,
        context: TenantContext,
        agent_id: UUID,
        version_id: UUID,
        *,
        expected_revision: int,
    ) -> Agent:
        async with self.database.tenant_transaction(context) as session:
            agent = await self._agent(session, context, agent_id, for_update=True)
            require_revision("agent", expected=expected_revision, actual=agent.revision)
            if agent.current_version_id == version_id:
                raise DomainConflict(
                    "VERSION_ALREADY_ACTIVE", "the requested version is already active"
                )
            target = await self._agent_version(
                session, context, agent.id, version_id, for_update=True
            )
            target.status = transition_state(
                "agent_version",
                AgentVersionStatus(target.status),
                AgentVersionStatus.PUBLISHED,
                AGENT_VERSION_TRANSITIONS,
            ).value
            if agent.current_version_id is not None:
                current = await self._agent_version(
                    session,
                    context,
                    agent.id,
                    agent.current_version_id,
                    for_update=True,
                )
                current.status = transition_state(
                    "agent_version",
                    AgentVersionStatus(current.status),
                    AgentVersionStatus.SUPERSEDED,
                    AGENT_VERSION_TRANSITIONS,
                ).value
            agent.current_version_id = target.id
            agent.revision += 1
            self._record(
                session,
                context,
                event_type="AgentVersionRolledBack",
                aggregate_type="agent",
                aggregate_id=agent.id,
                action="agent_version.rollback",
                payload={"version_id": str(target.id), "version": target.version},
            )
            await session.flush()
            return agent

    async def create_task(self, context: TenantContext, command: TaskCreate) -> Task:
        async with self.database.tenant_transaction(context) as session:
            await self._project(session, context, command.project_id)
            if command.parent_task_id is not None:
                await self._task(session, context, command.parent_task_id)
            status = TaskStatus.CREATED
            if command.assignee_agent_id is not None:
                await self._ready_agent(session, context, command.assignee_agent_id)
                status = TaskStatus.ASSIGNED
            task = Task(
                tenant_id=context.tenant_id,
                project_id=command.project_id,
                assignee_agent_id=command.assignee_agent_id,
                parent_task_id=command.parent_task_id,
                title=command.title,
                input=command.input,
                acceptance=command.acceptance,
                status=status.value,
                priority=command.priority,
            )
            session.add(task)
            await session.flush()
            self._record_change(session, context, task, "TaskCreated", "task.create")
            await session.flush()
            return task

    async def get_task(self, context: TenantContext, task_id: UUID) -> Task:
        async with self.database.tenant_transaction(context) as session:
            return await self._task(session, context, task_id)

    async def transition_task(
        self, context: TenantContext, task_id: UUID, command: TaskTransition
    ) -> Task:
        async with self.database.tenant_transaction(context) as session:
            task = await self._task(session, context, task_id, for_update=True)
            require_revision("task", expected=command.expected_revision, actual=task.revision)
            if command.target is TaskStatus.ASSIGNED:
                if command.assignee_agent_id is None:
                    raise DomainConflict("ASSIGNEE_REQUIRED", "assigned tasks need an agent")
                await self._ready_agent(session, context, command.assignee_agent_id)
                task.assignee_agent_id = command.assignee_agent_id
            task.status = transition_state(
                "task", TaskStatus(task.status), command.target, TASK_TRANSITIONS
            ).value
            task.revision += 1
            self._record_change(session, context, task, "TaskStatusChanged", "task.transition")
            await session.flush()
            return task

    async def create_run(self, context: TenantContext, task_id: UUID, command: RunCreate) -> Run:
        async with self.database.tenant_transaction(context) as session:
            task = await self._task(session, context, task_id, for_update=True)
            if TaskStatus(task.status) not in {TaskStatus.ASSIGNED, TaskStatus.REVISION_REQUIRED}:
                raise DomainConflict(
                    "TASK_NOT_RUNNABLE", "task must be assigned or require revision"
                )
            run = await self._new_run(session, context, task, command)
            task.status = transition_state(
                "task", TaskStatus(task.status), TaskStatus.RUNNING, TASK_TRANSITIONS
            ).value
            task.revision += 1
            self._record_change(
                session, context, task, "TaskStatusChanged", "task.transition", run_id=run.id
            )
            self._record(
                session,
                context,
                event_type="RunCreated",
                aggregate_type="run",
                aggregate_id=run.id,
                action="run.create",
                payload={"task_id": str(task.id), "attempt": run.attempt},
                run_id=run.id,
            )
            await session.flush()
            return run

    async def get_run(self, context: TenantContext, run_id: UUID) -> Run:
        async with self.database.tenant_transaction(context) as session:
            return await self._run(session, context, run_id)

    async def get_runtime_session(self, context: TenantContext, run_id: UUID) -> RuntimeSession:
        async with self.database.tenant_transaction(context) as session:
            await self._run(session, context, run_id)
            runtime_session = await session.scalar(
                select(RuntimeSession).where(
                    RuntimeSession.tenant_id == context.tenant_id,
                    RuntimeSession.run_id == run_id,
                )
            )
            if runtime_session is None:
                raise ResourceNotFound("runtime_session", str(run_id))
            return runtime_session

    async def get_runtime_trajectory(
        self, context: TenantContext, run_id: UUID
    ) -> RuntimeTrajectory:
        runtime_session = await self.get_runtime_session(context, run_id)
        if runtime_session.trajectory is None:
            raise DomainConflict(
                "RUNTIME_TRAJECTORY_UNAVAILABLE",
                "the runtime trajectory is not available until execution completes",
            )
        return RuntimeTrajectory.model_validate(runtime_session.trajectory)

    async def transition_run(
        self, context: TenantContext, run_id: UUID, command: RunTransition
    ) -> Run:
        async with self.database.tenant_transaction(context) as session:
            run = await self._run(session, context, run_id, for_update=True)
            require_revision("run", expected=command.expected_revision, actual=run.revision)
            current = RunStatus(run.status)
            run.status = transition_state("run", current, command.target, RUN_TRANSITIONS).value
            now = datetime.now(UTC)
            if current is RunStatus.PENDING and command.target is RunStatus.PLANNING:
                run.started_at = now
            if command.target in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.TIMED_OUT,
            }:
                run.ended_at = now
                run.lease_owner = None
                run.lease_token = None
                run.lease_expires_at = None
                run.heartbeat_at = None
                if command.target is RunStatus.CANCELLED:
                    runtime_session = await session.scalar(
                        select(RuntimeSession)
                        .where(
                            RuntimeSession.tenant_id == context.tenant_id,
                            RuntimeSession.run_id == run.id,
                        )
                        .with_for_update()
                    )
                    if runtime_session is not None:
                        runtime_session.status = "cancelled"
                        runtime_session.ended_at = now
                        runtime_session.revision += 1
            if command.result is not None:
                run.result = command.result
            if command.error is not None:
                run.error = command.error
            run.revision += 1
            if command.target is RunStatus.COMPLETED:
                task = await self._task(session, context, run.task_id, for_update=True)
                if TaskStatus(task.status) is TaskStatus.RUNNING:
                    task.status = transition_state(
                        "task",
                        TaskStatus(task.status),
                        TaskStatus.WAITING_FOR_REVIEW,
                        TASK_TRANSITIONS,
                    ).value
                    task.revision += 1
                    self._record_change(
                        session,
                        context,
                        task,
                        "TaskStatusChanged",
                        "task.transition",
                        run_id=run.id,
                    )
            elif command.target is RunStatus.CANCELLED:
                task = await self._task(session, context, run.task_id, for_update=True)
                if TaskStatus(task.status) is TaskStatus.RUNNING:
                    task.status = transition_state(
                        "task",
                        TaskStatus(task.status),
                        TaskStatus.CANCELLED,
                        TASK_TRANSITIONS,
                    ).value
                    task.revision += 1
                    self._record_change(
                        session,
                        context,
                        task,
                        "TaskCancelled",
                        "task.cancel",
                        run_id=run.id,
                    )
            event_type = {
                RunStatus.PLANNING: "RunPlanningStarted",
                RunStatus.RUNNING: "RunStarted",
                RunStatus.COMPLETED: "RunCompleted",
                RunStatus.FAILED: "RunFailed",
                RunStatus.CANCELLED: "RunCancelled",
                RunStatus.TIMED_OUT: "RunTimedOut",
            }.get(command.target, "RunStatusChanged")
            self._record_change(session, context, run, event_type, "run.transition", run_id=run.id)
            await session.flush()
            return run

    async def cancel_run(
        self, context: TenantContext, run_id: UUID, *, expected_revision: int
    ) -> Run:
        command = RunTransition(target=RunStatus.CANCELLED, expected_revision=expected_revision)
        return await self.transition_run(context, run_id, command)

    async def retry_run(self, context: TenantContext, run_id: UUID, command: RunCreate) -> Run:
        async with self.database.tenant_transaction(context) as session:
            previous = await self._run(session, context, run_id, for_update=True)
            if RunStatus(previous.status) not in {
                RunStatus.FAILED,
                RunStatus.TIMED_OUT,
            }:
                raise DomainConflict("RUN_NOT_RETRYABLE", "only failed or timed-out runs can retry")
            task = await self._task(session, context, previous.task_id, for_update=True)
            if TaskStatus(task.status) not in {TaskStatus.RUNNING, TaskStatus.REVISION_REQUIRED}:
                raise DomainConflict("TASK_NOT_RUNNABLE", "task state does not allow another run")
            new_run = await self._new_run(
                session, context, task, command, retry_of_run_id=previous.id
            )
            if TaskStatus(task.status) is TaskStatus.REVISION_REQUIRED:
                task.status = transition_state(
                    "task", TaskStatus(task.status), TaskStatus.RUNNING, TASK_TRANSITIONS
                ).value
                task.revision += 1
                self._record_change(
                    session,
                    context,
                    task,
                    "TaskStatusChanged",
                    "task.transition",
                    run_id=new_run.id,
                )
            self._record(
                session,
                context,
                event_type="RunRetried",
                aggregate_type="run",
                aggregate_id=new_run.id,
                action="run.retry",
                payload={"retry_of_run_id": str(previous.id), "attempt": new_run.attempt},
                run_id=new_run.id,
            )
            await session.flush()
            return new_run

    async def create_run_step(
        self, context: TenantContext, run_id: UUID, command: RunStepCreate
    ) -> RunStep:
        async with self.database.tenant_transaction(context) as session:
            run = await self._run(session, context, run_id)
            if RunStatus(run.status) not in {
                RunStatus.PLANNING,
                RunStatus.RUNNING,
                RunStatus.WAITING_FOR_TOOL,
                RunStatus.WAITING_FOR_APPROVAL,
            }:
                raise DomainConflict("RUN_NOT_ACTIVE", "steps can only be added to an active run")
            step = RunStep(
                tenant_id=context.tenant_id,
                run_id=run.id,
                sequence=command.sequence,
                kind=command.kind,
                input=command.input,
            )
            session.add(step)
            await session.flush()
            self._record(
                session,
                context,
                event_type="RunStepCreated",
                aggregate_type="run_step",
                aggregate_id=step.id,
                action="run_step.create",
                payload={"sequence": step.sequence, "kind": step.kind},
                run_id=run.id,
            )
            await session.flush()
            return step

    async def transition_run_step(
        self,
        context: TenantContext,
        run_id: UUID,
        step_id: UUID,
        command: RunStepTransition,
    ) -> RunStep:
        async with self.database.tenant_transaction(context) as session:
            step = await self._run_step(session, context, run_id, step_id, for_update=True)
            require_revision("run_step", expected=command.expected_revision, actual=step.revision)
            current = RunStepStatus(step.status)
            step.status = transition_state(
                "run_step", current, command.target, RUN_STEP_TRANSITIONS
            ).value
            now = datetime.now(UTC)
            if current is RunStepStatus.PENDING and command.target is RunStepStatus.RUNNING:
                step.started_at = now
            if command.target in {
                RunStepStatus.COMPLETED,
                RunStepStatus.FAILED,
                RunStepStatus.CANCELLED,
            }:
                step.ended_at = now
            if command.output is not None:
                step.output = command.output
            if command.error is not None:
                step.error = command.error
            step.revision += 1
            event_type = {
                RunStepStatus.RUNNING: "StepStarted",
                RunStepStatus.COMPLETED: "StepCompleted",
                RunStepStatus.FAILED: "StepFailed",
            }.get(command.target, "StepStatusChanged")
            self._record_change(
                session, context, step, event_type, "run_step.transition", run_id=run_id
            )
            await session.flush()
            return step

    async def list_run_events(self, context: TenantContext, run_id: UUID) -> list[Event]:
        async with self.database.tenant_transaction(context) as session:
            await self._run(session, context, run_id)
            return list(
                await session.scalars(
                    select(Event)
                    .where(Event.tenant_id == context.tenant_id, Event.run_id == run_id)
                    .order_by(Event.sequence)
                )
            )

    async def list_audit(self, context: TenantContext, *, limit: int = 100) -> list[AuditRecord]:
        async with self.database.tenant_transaction(context) as session:
            return list(
                await session.scalars(
                    select(AuditRecord)
                    .where(AuditRecord.tenant_id == context.tenant_id)
                    .order_by(AuditRecord.sequence.desc())
                    .limit(limit)
                )
            )

    async def _transition_agent(
        self,
        context: TenantContext,
        agent_id: UUID,
        *,
        expected_revision: int,
        target: AgentStatus,
        event_type: str,
        action: str,
    ) -> Agent:
        async with self.database.tenant_transaction(context) as session:
            agent = await self._agent(session, context, agent_id, for_update=True)
            require_revision("agent", expected=expected_revision, actual=agent.revision)
            agent.status = transition_state(
                "agent", AgentStatus(agent.status), target, AGENT_TRANSITIONS
            ).value
            agent.revision += 1
            self._record_change(session, context, agent, event_type, action)
            await session.flush()
            return agent

    async def _new_run(
        self,
        session: AsyncSession,
        context: TenantContext,
        task: Task,
        command: RunCreate,
        *,
        retry_of_run_id: UUID | None = None,
    ) -> Run:
        if task.assignee_agent_id is None:
            raise DomainConflict("ASSIGNEE_REQUIRED", "task needs an agent before creating a run")
        agent = await self._ready_agent(session, context, task.assignee_agent_id)
        if agent.current_version_id is None:
            raise DomainConflict("AGENT_VERSION_REQUIRED", "agent needs a published version")
        latest_attempt = await session.scalar(
            select(func.max(Run.attempt)).where(
                Run.tenant_id == context.tenant_id,
                Run.task_id == task.id,
            )
        )
        run = Run(
            tenant_id=context.tenant_id,
            task_id=task.id,
            agent_id=agent.id,
            agent_version_id=agent.current_version_id,
            retry_of_run_id=retry_of_run_id,
            attempt=(latest_attempt or 0) + 1,
            max_steps=command.max_steps,
            token_budget=command.token_budget,
            timeout_seconds=command.timeout_seconds,
            budgets=command.budgets,
        )
        session.add(run)
        await session.flush()
        return run

    @staticmethod
    def _record(
        session: AsyncSession,
        context: TenantContext,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: UUID,
        action: str,
        payload: dict,
        run_id: UUID | None = None,
    ) -> None:
        session.add(
            Event(
                tenant_id=context.tenant_id,
                event_type=event_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                run_id=run_id,
                actor_id=context.actor_id,
                payload=payload,
                correlation_id=context.correlation_id,
            )
        )
        session.add(
            AuditRecord(
                tenant_id=context.tenant_id,
                action=action,
                resource_type=aggregate_type,
                resource_id=aggregate_id,
                actor_id=context.actor_id,
                details=payload,
                correlation_id=context.correlation_id,
            )
        )

    def _record_change(
        self,
        session: AsyncSession,
        context: TenantContext,
        entity,
        event_type: str,
        action: str,
        *,
        run_id: UUID | None = None,
    ) -> None:
        self._record(
            session,
            context,
            event_type=event_type,
            aggregate_type=entity.__tablename__.removesuffix("s"),
            aggregate_id=entity.id,
            action=action,
            payload={"status": getattr(entity, "status", None), "revision": entity.revision},
            run_id=run_id,
        )

    @staticmethod
    async def _one(session: AsyncSession, statement, entity: str, resource_id: UUID):
        instance = await session.scalar(statement)
        if instance is None:
            raise ResourceNotFound(entity, str(resource_id))
        return instance

    async def _project(
        self,
        session: AsyncSession,
        context: TenantContext,
        project_id: UUID,
        *,
        for_update: bool = False,
    ) -> Project:
        statement = select(Project).where(
            Project.tenant_id == context.tenant_id, Project.id == project_id
        )
        if for_update:
            statement = statement.with_for_update()
        return await self._one(session, statement, "project", project_id)

    async def _agent(
        self,
        session: AsyncSession,
        context: TenantContext,
        agent_id: UUID,
        *,
        for_update: bool = False,
    ) -> Agent:
        statement = select(Agent).where(Agent.tenant_id == context.tenant_id, Agent.id == agent_id)
        if for_update:
            statement = statement.with_for_update()
        return await self._one(session, statement, "agent", agent_id)

    async def _ready_agent(
        self, session: AsyncSession, context: TenantContext, agent_id: UUID
    ) -> Agent:
        agent = await self._agent(session, context, agent_id)
        if AgentStatus(agent.status) is not AgentStatus.READY:
            raise DomainConflict("AGENT_NOT_READY", "assigned agent must be ready")
        return agent

    async def _agent_version(
        self,
        session: AsyncSession,
        context: TenantContext,
        agent_id: UUID,
        version_id: UUID,
        *,
        for_update: bool = False,
    ) -> AgentVersion:
        statement = select(AgentVersion).where(
            AgentVersion.tenant_id == context.tenant_id,
            AgentVersion.agent_id == agent_id,
            AgentVersion.id == version_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return await self._one(session, statement, "agent_version", version_id)

    async def _task(
        self,
        session: AsyncSession,
        context: TenantContext,
        task_id: UUID,
        *,
        for_update: bool = False,
    ) -> Task:
        statement = select(Task).where(Task.tenant_id == context.tenant_id, Task.id == task_id)
        if for_update:
            statement = statement.with_for_update()
        return await self._one(session, statement, "task", task_id)

    async def _run(
        self,
        session: AsyncSession,
        context: TenantContext,
        run_id: UUID,
        *,
        for_update: bool = False,
    ) -> Run:
        statement = select(Run).where(Run.tenant_id == context.tenant_id, Run.id == run_id)
        if for_update:
            statement = statement.with_for_update()
        return await self._one(session, statement, "run", run_id)

    async def _run_step(
        self,
        session: AsyncSession,
        context: TenantContext,
        run_id: UUID,
        step_id: UUID,
        *,
        for_update: bool = False,
    ) -> RunStep:
        statement = select(RunStep).where(
            RunStep.tenant_id == context.tenant_id,
            RunStep.run_id == run_id,
            RunStep.id == step_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return await self._one(session, statement, "run_step", step_id)

    @staticmethod
    def _version_hash(command: AgentVersionCreate) -> str:
        payload = command.model_dump(mode="json", by_alias=True)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _copy_version(source: AgentVersion, agent_id: UUID, *, version: int) -> AgentVersion:
        return AgentVersion(
            tenant_id=source.tenant_id,
            agent_id=agent_id,
            version=version,
            role=source.role,
            mandate=source.mandate,
            boundaries=source.boundaries,
            long_term_goal=source.long_term_goal,
            current_goal=source.current_goal,
            model_config_json=source.model_config_json,
            tool_policy=source.tool_policy,
            memory_policy=source.memory_policy,
            skill_policy=source.skill_policy,
            plugin_refs=source.plugin_refs,
            budgets=source.budgets,
            run_config=source.run_config,
            content_hash=source.content_hash,
        )
