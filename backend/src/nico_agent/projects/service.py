"""Transactional service for managed multi-Agent Projects."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.agent_versions import AgentVersionLifecycle
from nico_agent.api_schemas import ProjectRead
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainConflict, ResourceNotFound
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    AuditRecord,
    Conversation,
    Event,
    Project,
    ProjectMember,
    ProjectSession,
    Tenant,
)
from nico_agent.domain.states import (
    PROJECT_MEMBER_TRANSITIONS,
    PROJECT_SESSION_TRANSITIONS,
    AgentStatus,
    AgentVersionStatus,
    ProjectMemberStatus,
    ProjectSessionStatus,
    ProjectStatus,
    require_revision,
    transition_state,
)
from nico_agent.projects.contracts import (
    ProjectCollaborationCreate,
    ProjectCollaborationRead,
    ProjectLeadPreflightRead,
    ProjectLeadReplace,
    ProjectLeadReplaceRead,
    ProjectMemberAdd,
    ProjectMemberMutationRead,
    ProjectMemberRead,
    ProjectMemberStateCommand,
    ProjectPreflightRequest,
    ProjectSessionRead,
)

_MANAGED_KEY = "_nico_collaboration"


class ProjectCollaborationService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def preflight(
        self, context: TenantContext, command: ProjectPreflightRequest
    ) -> ProjectLeadPreflightRead:
        async with self.database.tenant_transaction(context) as session:
            return await self._preflight(session, context, command)

    async def create(
        self,
        context: TenantContext,
        command: ProjectCollaborationCreate,
        *,
        idempotency_key: str,
    ) -> ProjectCollaborationRead:
        fingerprint = self._fingerprint(command)
        async with self.database.tenant_transaction(context) as session:
            replay = await session.scalar(
                select(Project).where(
                    Project.tenant_id == context.tenant_id,
                    Project.idempotency_key == idempotency_key,
                )
            )
            if replay is not None:
                stored = self._managed_metadata(replay).get("request_fingerprint")
                if stored != fingerprint:
                    raise DomainConflict(
                        "IDEMPOTENCY_CONFLICT",
                        "the Project idempotency key was already used with another request",
                    )
                return await self._collaboration(session, context, replay)

            preflight = await self._preflight(session, context, command)
            if not preflight.compatible:
                raise DomainConflict(
                    "PROJECT_LEAD_INCOMPATIBLE",
                    "the selected Lead cannot coordinate this Project",
                    details={"issues": preflight.issues},
                )
            project = Project(
                tenant_id=context.tenant_id,
                name=command.name,
                description=command.description,
                kind="shared",
                supervision_cadence_seconds=command.supervision_cadence_seconds,
                next_supervision_at=(
                    datetime.now(UTC) if command.supervision_cadence_seconds is not None else None
                ),
                metadata_json={
                    _MANAGED_KEY: {
                        "managed": True,
                        "goal": command.goal,
                        "acceptance": command.acceptance,
                        "request_fingerprint": fingerprint,
                    }
                },
                idempotency_key=idempotency_key,
            )
            session.add(project)
            await session.flush()
            self._record(
                session,
                context,
                event_type="ProjectCollaborationCreated",
                aggregate_type="project",
                aggregate_id=project.id,
                action="project.collaboration.create",
                payload={
                    "name": project.name,
                    "lead_agent_id": str(command.lead_agent_id),
                    "member_agent_ids": [str(item) for item in command.member_agent_ids],
                    "idempotency_key": idempotency_key,
                },
            )
            ordered = [
                (command.lead_agent_id, "lead"),
                *[(item, "member") for item in command.member_agent_ids],
            ]
            for index, (agent_id, role) in enumerate(ordered):
                agent, version = await self._ready_version(session, context, agent_id)
                await self._create_member_session(
                    session,
                    context,
                    project,
                    agent,
                    version,
                    role=role,
                    idempotency_key=f"{idempotency_key}:member:{index}",
                )
            await session.flush()
            return await self._collaboration(session, context, project)

    async def list_members(
        self, context: TenantContext, project_id: UUID
    ) -> list[ProjectMember]:
        async with self.database.tenant_transaction(context) as session:
            await self._project(session, context, project_id)
            return await self._members(session, context, project_id)

    async def add_member(
        self,
        context: TenantContext,
        project_id: UUID,
        command: ProjectMemberAdd,
        *,
        idempotency_key: str,
    ) -> ProjectMemberMutationRead:
        async with self.database.tenant_transaction(context) as session:
            project = await self._managed_project(
                session, context, project_id, for_update=True
            )
            require_revision(
                "project", expected=command.expected_project_revision, actual=project.revision
            )
            existing = await session.scalar(
                select(ProjectMember).where(
                    ProjectMember.tenant_id == context.tenant_id,
                    ProjectMember.project_id == project.id,
                    ProjectMember.agent_id == command.agent_id,
                )
            )
            if existing is not None:
                session_value = await self._member_session(session, context, existing)
                if existing.status == ProjectMemberStatus.ACTIVE.value:
                    return self._mutation(project, existing, session_value)
                raise DomainConflict(
                    "PROJECT_MEMBER_INACTIVE",
                    "restore the existing Project membership instead of adding a duplicate",
                )
            agent, version = await self._ready_version(session, context, command.agent_id)
            member, session_value = await self._create_member_session(
                session,
                context,
                project,
                agent,
                version,
                role="member",
                idempotency_key=idempotency_key,
            )
            project.revision += 1
            self._record_project_revision(
                session, context, project, "ProjectMemberAdded", "project.member.add"
            )
            await session.flush()
            return self._mutation(project, member, session_value)

    async def set_member_state(
        self,
        context: TenantContext,
        project_id: UUID,
        agent_id: UUID,
        command: ProjectMemberStateCommand,
        *,
        idempotency_key: str,
    ) -> ProjectMemberMutationRead:
        async with self.database.tenant_transaction(context) as session:
            project = await self._managed_project(
                session, context, project_id, for_update=True
            )
            member = await self._member(
                session, context, project.id, agent_id, for_update=True
            )
            session_value = await self._member_session(session, context, member, for_update=True)
            if await self._is_replay(
                session, context, "project.member.state", member.id, idempotency_key
            ):
                return self._mutation(project, member, session_value)
            require_revision(
                "project", expected=command.expected_project_revision, actual=project.revision
            )
            require_revision(
                "project_member",
                expected=command.expected_member_revision,
                actual=member.revision,
            )
            if member.role == "lead" and command.target != "active":
                raise DomainConflict(
                    "PROJECT_LEAD_REQUIRED",
                    "replace the Lead before pausing or removing it",
                )
            target = ProjectMemberStatus(command.target)
            member.status = transition_state(
                "project_member",
                ProjectMemberStatus(member.status),
                target,
                PROJECT_MEMBER_TRANSITIONS,
            ).value
            member.removal_reason = (
                command.reason if target is ProjectMemberStatus.REMOVED else None
            )
            member.removed_at = datetime.now(UTC) if target is ProjectMemberStatus.REMOVED else None
            member.revision += 1
            session_target = (
                ProjectSessionStatus.ACTIVE
                if target is ProjectMemberStatus.ACTIVE
                else ProjectSessionStatus.PAUSED
            )
            if ProjectSessionStatus(session_value.status) is not session_target:
                session_value.status = transition_state(
                    "project_session",
                    ProjectSessionStatus(session_value.status),
                    session_target,
                    PROJECT_SESSION_TRANSITIONS,
                ).value
                session_value.revision += 1
            project.revision += 1
            payload = {
                "status": member.status,
                "reason": command.reason,
                "revision": member.revision,
                "idempotency_key": idempotency_key,
            }
            self._record(
                session,
                context,
                event_type="ProjectMemberStateChanged",
                aggregate_type="project_member",
                aggregate_id=member.id,
                action="project.member.state",
                payload=payload,
            )
            self._record(
                session,
                context,
                event_type="ProjectSessionStateChanged",
                aggregate_type="project_session",
                aggregate_id=session_value.id,
                action="project_session.state",
                payload={"status": session_value.status, "revision": session_value.revision},
            )
            self._record_project_revision(
                session, context, project, "ProjectMembershipChanged", "project.membership.update"
            )
            await session.flush()
            return self._mutation(project, member, session_value)

    async def replace_lead(
        self,
        context: TenantContext,
        project_id: UUID,
        command: ProjectLeadReplace,
        *,
        idempotency_key: str,
    ) -> ProjectLeadReplaceRead:
        async with self.database.tenant_transaction(context) as session:
            project = await self._managed_project(
                session, context, project_id, for_update=True
            )
            if await self._is_replay(
                session, context, "project.lead.replace", project.id, idempotency_key
            ):
                value = await self._collaboration(session, context, project)
                return ProjectLeadReplaceRead(**value.model_dump())
            require_revision(
                "project", expected=command.expected_project_revision, actual=project.revision
            )
            preflight = await self._preflight(
                session,
                context,
                ProjectPreflightRequest(
                    lead_agent_id=command.new_lead_agent_id,
                    member_agent_ids=[],
                ),
            )
            if not preflight.compatible:
                raise DomainConflict(
                    "PROJECT_LEAD_INCOMPATIBLE",
                    "the selected replacement cannot coordinate this Project",
                    details={"issues": preflight.issues},
                )
            old_lead = await session.scalar(
                select(ProjectMember)
                .where(
                    ProjectMember.tenant_id == context.tenant_id,
                    ProjectMember.project_id == project.id,
                    ProjectMember.role == "lead",
                    ProjectMember.status == ProjectMemberStatus.ACTIVE.value,
                )
                .with_for_update()
            )
            if old_lead is None:
                raise DomainConflict("PROJECT_LEAD_REQUIRED", "the Project has no active Lead")
            new_lead = await self._member(
                session,
                context,
                project.id,
                command.new_lead_agent_id,
                for_update=True,
            )
            if new_lead.status != ProjectMemberStatus.ACTIVE.value:
                raise DomainConflict(
                    "PROJECT_MEMBER_INACTIVE", "the replacement Lead must be an active member"
                )
            if new_lead.id == old_lead.id:
                value = await self._collaboration(session, context, project)
                return ProjectLeadReplaceRead(**value.model_dump())
            old_lead.role = "member"
            old_lead.revision += 1
            await session.flush()
            new_lead.role = "lead"
            new_lead.revision += 1
            project.revision += 1
            self._record(
                session,
                context,
                event_type="ProjectLeadReplaced",
                aggregate_type="project",
                aggregate_id=project.id,
                action="project.lead.replace",
                payload={
                    "old_lead_agent_id": str(old_lead.agent_id),
                    "new_lead_agent_id": str(new_lead.agent_id),
                    "idempotency_key": idempotency_key,
                    "revision": project.revision,
                },
            )
            await session.flush()
            value = await self._collaboration(session, context, project)
            return ProjectLeadReplaceRead(**value.model_dump())

    async def list_sessions(
        self, context: TenantContext, project_id: UUID
    ) -> list[ProjectSession]:
        async with self.database.tenant_transaction(context) as session:
            await self._project(session, context, project_id)
            return list(
                await session.scalars(
                    select(ProjectSession)
                    .where(
                        ProjectSession.tenant_id == context.tenant_id,
                        ProjectSession.project_id == project_id,
                    )
                    .order_by(ProjectSession.created_at, ProjectSession.id)
                )
            )

    async def get_session(
        self, context: TenantContext, project_id: UUID, session_id: UUID
    ) -> ProjectSession:
        async with self.database.tenant_transaction(context) as session:
            await self._project(session, context, project_id)
            value = await session.scalar(
                select(ProjectSession).where(
                    ProjectSession.tenant_id == context.tenant_id,
                    ProjectSession.project_id == project_id,
                    ProjectSession.id == session_id,
                )
            )
            if value is None:
                raise ResourceNotFound("project_session", str(session_id))
            return value

    async def resolve_session_conversation(
        self,
        context: TenantContext,
        project_id: UUID,
        session_id: UUID,
    ) -> Conversation:
        """Return the writable frozen Conversation, rotating on published version changes."""

        async with self.database.tenant_transaction(context) as session:
            project = await self._managed_project(
                session,
                context,
                project_id,
                for_update=True,
            )
            project_session = await session.scalar(
                select(ProjectSession)
                .where(
                    ProjectSession.tenant_id == context.tenant_id,
                    ProjectSession.project_id == project.id,
                    ProjectSession.id == session_id,
                )
                .with_for_update()
            )
            if project_session is None:
                raise ResourceNotFound("project_session", str(session_id))
            member = await session.scalar(
                select(ProjectMember)
                .where(
                    ProjectMember.tenant_id == context.tenant_id,
                    ProjectMember.id == project_session.project_member_id,
                )
                .with_for_update()
            )
            if member is None:
                raise ResourceNotFound("project_member", str(project_session.project_member_id))
            if (
                ProjectMemberStatus(member.status) is not ProjectMemberStatus.ACTIVE
                or ProjectSessionStatus(project_session.status) is not ProjectSessionStatus.ACTIVE
            ):
                raise DomainConflict(
                    "PROJECT_MEMBER_INACTIVE",
                    "only an active Project member Session accepts messages",
                )
            agent, version = await self._ready_version(session, context, member.agent_id)
            current = None
            if project_session.current_conversation_id is not None:
                current = await session.scalar(
                    select(Conversation).where(
                        Conversation.tenant_id == context.tenant_id,
                        Conversation.project_id == project.id,
                        Conversation.project_session_id == project_session.id,
                        Conversation.id == project_session.current_conversation_id,
                    )
                )
            if current is not None and current.agent_version_id == version.id:
                current._conversation_mode = "project"
                return current

            conversation_key = f"project-session:{project_session.id}:version:{version.id}"
            replacement = await session.scalar(
                select(Conversation).where(
                    Conversation.tenant_id == context.tenant_id,
                    Conversation.idempotency_key == conversation_key,
                )
            )
            if replacement is None:
                replacement = Conversation(
                    tenant_id=context.tenant_id,
                    project_id=project.id,
                    project_session_id=project_session.id,
                    agent_id=agent.id,
                    agent_version_id=version.id,
                    title=f"{project.name} — {agent.display_name}",
                    created_by=context.actor_id,
                    idempotency_key=conversation_key,
                )
                session.add(replacement)
                await session.flush([replacement])
                self._record(
                    session,
                    context,
                    event_type="ConversationCreated",
                    aggregate_type="conversation",
                    aggregate_id=replacement.id,
                    action="conversation.create",
                    payload={
                        "project_id": str(project.id),
                        "project_session_id": str(project_session.id),
                        "agent_id": str(agent.id),
                        "agent_version_id": str(version.id),
                    },
                )
            previous_id = project_session.current_conversation_id
            project_session.current_conversation_id = replacement.id
            project_session.revision += 1
            self._record(
                session,
                context,
                event_type="ProjectSessionConversationRotated",
                aggregate_type="project_session",
                aggregate_id=project_session.id,
                action="project_session.rotate_conversation",
                payload={
                    "previous_conversation_id": str(previous_id) if previous_id else None,
                    "conversation_id": str(replacement.id),
                    "agent_version_id": str(version.id),
                    "revision": project_session.revision,
                },
            )
            await session.flush()
            replacement._conversation_mode = "project"
            return replacement

    async def _preflight(
        self,
        session: AsyncSession,
        context: TenantContext,
        command: ProjectPreflightRequest,
    ) -> ProjectLeadPreflightRead:
        issues: list[str] = []
        lead, version = await self._agent_version(session, context, command.lead_agent_id)
        if AgentStatus(lead.status) is not AgentStatus.READY:
            issues.append("Lead Agent must be ready")
        if (
            version is None
            or AgentVersionStatus(version.status) is not AgentVersionStatus.PUBLISHED
        ):
            issues.append("Lead Agent must have a published current version")
        else:
            tenant = await session.scalar(
                select(Tenant).where(Tenant.id == context.tenant_id)
            )
            issues.extend(
                AgentVersionLifecycle.project_lead_compatibility_issues(
                    version,
                    tenant.settings if tenant is not None else {},
                )
            )
        member_versions: list[UUID] = []
        for member_id in command.member_agent_ids:
            member, member_version = await self._agent_version(session, context, member_id)
            if AgentStatus(member.status) is not AgentStatus.READY:
                issues.append(f"Member Agent {member_id} must be ready")
            if (
                member_version is None
                or AgentVersionStatus(member_version.status) is not AgentVersionStatus.PUBLISHED
            ):
                issues.append(f"Member Agent {member_id} needs a published current version")
            else:
                member_versions.append(member_version.id)
        return ProjectLeadPreflightRead(
            compatible=not issues,
            lead_agent_id=command.lead_agent_id,
            lead_agent_version_id=version.id if version is not None else None,
            member_agent_version_ids=member_versions,
            issues=issues,
        )

    async def _create_member_session(
        self,
        session: AsyncSession,
        context: TenantContext,
        project: Project,
        agent: Agent,
        version: AgentVersion,
        *,
        role: str,
        idempotency_key: str,
    ) -> tuple[ProjectMember, ProjectSession]:
        member = ProjectMember(
            tenant_id=context.tenant_id,
            project_id=project.id,
            agent_id=agent.id,
            role=role,
            status=ProjectMemberStatus.ACTIVE.value,
            created_by=context.actor_id,
            idempotency_key=f"{idempotency_key}:membership",
        )
        session.add(member)
        await session.flush()
        project_session = ProjectSession(
            tenant_id=context.tenant_id,
            project_id=project.id,
            project_member_id=member.id,
            agent_id=agent.id,
            status=ProjectSessionStatus.ACTIVE.value,
            idempotency_key=f"{idempotency_key}:session",
        )
        session.add(project_session)
        await session.flush()
        conversation = Conversation(
            tenant_id=context.tenant_id,
            project_id=project.id,
            project_session_id=project_session.id,
            agent_id=agent.id,
            agent_version_id=version.id,
            title=f"{project.name} — {agent.display_name}",
            created_by=context.actor_id,
            idempotency_key=f"project-session:{project_session.id}:version:{version.id}",
        )
        session.add(conversation)
        await session.flush()
        project_session.current_conversation_id = conversation.id
        self._record(
            session,
            context,
            event_type="ProjectMemberAdded",
            aggregate_type="project_member",
            aggregate_id=member.id,
            action="project_member.create",
            payload={
                "project_id": str(project.id),
                "agent_id": str(agent.id),
                "role": role,
                "idempotency_key": member.idempotency_key,
            },
        )
        self._record(
            session,
            context,
            event_type="ProjectSessionCreated",
            aggregate_type="project_session",
            aggregate_id=project_session.id,
            action="project_session.create",
            payload={
                "project_id": str(project.id),
                "agent_id": str(agent.id),
                "conversation_id": str(conversation.id),
                "idempotency_key": project_session.idempotency_key,
            },
        )
        await session.flush()
        return member, project_session

    async def _collaboration(
        self, session: AsyncSession, context: TenantContext, project: Project
    ) -> ProjectCollaborationRead:
        members = await self._members(session, context, project.id)
        sessions = list(
            await session.scalars(
                select(ProjectSession)
                .where(
                    ProjectSession.tenant_id == context.tenant_id,
                    ProjectSession.project_id == project.id,
                )
                .order_by(ProjectSession.created_at, ProjectSession.id)
            )
        )
        lead = next(
            (
                item
                for item in members
                if item.role == "lead" and item.status == ProjectMemberStatus.ACTIVE.value
            ),
            None,
        )
        if lead is None:
            raise DomainConflict("PROJECT_LEAD_REQUIRED", "the Project has no active Lead")
        return ProjectCollaborationRead(
            project=ProjectRead.model_validate(project),
            members=[ProjectMemberRead.model_validate(item) for item in members],
            sessions=[ProjectSessionRead.model_validate(item) for item in sessions],
            lead_agent_id=lead.agent_id,
        )

    async def _members(
        self, session: AsyncSession, context: TenantContext, project_id: UUID
    ) -> list[ProjectMember]:
        return list(
            await session.scalars(
                select(ProjectMember)
                .where(
                    ProjectMember.tenant_id == context.tenant_id,
                    ProjectMember.project_id == project_id,
                )
                .order_by(
                    case((ProjectMember.role == "lead", 0), else_=1),
                    ProjectMember.created_at,
                    ProjectMember.id,
                )
            )
        )

    async def _project(
        self,
        session: AsyncSession,
        context: TenantContext,
        project_id: UUID,
        *,
        for_update: bool = False,
    ) -> Project:
        statement = select(Project).where(
            Project.tenant_id == context.tenant_id,
            Project.id == project_id,
        )
        if for_update:
            statement = statement.with_for_update()
        project = await session.scalar(statement)
        if project is None:
            raise ResourceNotFound("project", str(project_id))
        return project

    async def _managed_project(
        self,
        session: AsyncSession,
        context: TenantContext,
        project_id: UUID,
        *,
        for_update: bool = False,
    ) -> Project:
        project = await self._project(
            session, context, project_id, for_update=for_update
        )
        if ProjectStatus(project.status) is ProjectStatus.ARCHIVED:
            raise DomainConflict("PROJECT_ARCHIVED", "archived Projects are read-only")
        if not self.is_managed(project):
            raise DomainConflict(
                "PROJECT_NOT_MANAGED",
                "this legacy Project has no collaboration membership",
            )
        return project

    async def _member(
        self,
        session: AsyncSession,
        context: TenantContext,
        project_id: UUID,
        agent_id: UUID,
        *,
        for_update: bool = False,
    ) -> ProjectMember:
        statement = select(ProjectMember).where(
            ProjectMember.tenant_id == context.tenant_id,
            ProjectMember.project_id == project_id,
            ProjectMember.agent_id == agent_id,
        )
        if for_update:
            statement = statement.with_for_update()
        member = await session.scalar(statement)
        if member is None:
            raise ResourceNotFound("project_member", str(agent_id))
        return member

    async def _member_session(
        self,
        session: AsyncSession,
        context: TenantContext,
        member: ProjectMember,
        *,
        for_update: bool = False,
    ) -> ProjectSession:
        statement = select(ProjectSession).where(
            ProjectSession.tenant_id == context.tenant_id,
            ProjectSession.project_member_id == member.id,
        )
        if for_update:
            statement = statement.with_for_update()
        value = await session.scalar(statement)
        if value is None:
            raise ResourceNotFound("project_session", str(member.id))
        return value

    async def _agent_version(
        self, session: AsyncSession, context: TenantContext, agent_id: UUID
    ) -> tuple[Agent, AgentVersion | None]:
        agent = await session.scalar(
            select(Agent).where(
                Agent.tenant_id == context.tenant_id,
                Agent.id == agent_id,
            )
        )
        if agent is None:
            raise ResourceNotFound("agent", str(agent_id))
        version = None
        if agent.current_version_id is not None:
            version = await session.scalar(
                select(AgentVersion).where(
                    AgentVersion.tenant_id == context.tenant_id,
                    AgentVersion.agent_id == agent.id,
                    AgentVersion.id == agent.current_version_id,
                )
            )
        return agent, version

    async def _ready_version(
        self, session: AsyncSession, context: TenantContext, agent_id: UUID
    ) -> tuple[Agent, AgentVersion]:
        agent, version = await self._agent_version(session, context, agent_id)
        if AgentStatus(agent.status) is not AgentStatus.READY:
            raise DomainConflict("AGENT_NOT_READY", "Project members must be ready")
        if (
            version is None
            or AgentVersionStatus(version.status) is not AgentVersionStatus.PUBLISHED
        ):
            raise DomainConflict(
                "AGENT_VERSION_REQUIRED", "Project members need a published current version"
            )
        return agent, version

    async def _is_replay(
        self,
        session: AsyncSession,
        context: TenantContext,
        action: str,
        resource_id: UUID,
        idempotency_key: str,
    ) -> bool:
        return (
            await session.scalar(
                select(AuditRecord.id).where(
                    AuditRecord.tenant_id == context.tenant_id,
                    AuditRecord.action == action,
                    AuditRecord.resource_id == resource_id,
                    AuditRecord.details["idempotency_key"].astext == idempotency_key,
                )
            )
        ) is not None

    @staticmethod
    def _managed_metadata(project: Project) -> dict:
        value = project.metadata_json.get(_MANAGED_KEY, {})
        return value if isinstance(value, dict) else {}

    @classmethod
    def is_managed(cls, project: Project) -> bool:
        return cls._managed_metadata(project).get("managed") is True

    @staticmethod
    def _fingerprint(command: ProjectCollaborationCreate) -> str:
        encoded = json.dumps(
            command.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _mutation(
        project: Project, member: ProjectMember, session: ProjectSession
    ) -> ProjectMemberMutationRead:
        return ProjectMemberMutationRead(
            project=ProjectRead.model_validate(project),
            member=ProjectMemberRead.model_validate(member),
            session=ProjectSessionRead.model_validate(session),
        )

    def _record_project_revision(
        self,
        session: AsyncSession,
        context: TenantContext,
        project: Project,
        event_type: str,
        action: str,
    ) -> None:
        self._record(
            session,
            context,
            event_type=event_type,
            aggregate_type="project",
            aggregate_id=project.id,
            action=action,
            payload={"revision": project.revision},
        )

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
    ) -> None:
        session.add(
            Event(
                tenant_id=context.tenant_id,
                event_type=event_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
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
