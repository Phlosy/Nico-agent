"""Tenant-scoped Conversation and Turn transaction orchestration."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.config import Settings
from nico_agent.conversations.contracts import (
    DEFAULT_CONVERSATION_BUDGETS,
    ConversationCompact,
    ConversationCompactAccepted,
    ConversationCreate,
    ConversationPatch,
    ConversationQueueRead,
    ConversationQueueResume,
    ConversationTurnCreate,
    ConversationTurnRead,
    ConversationTurnRetry,
)
from nico_agent.coordination.service import CoordinationService
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import AccessDenied, DomainConflict, ResourceNotFound
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    Artifact,
    AuditRecord,
    Conversation,
    ConversationAttachment,
    ConversationTurn,
    Event,
    Project,
    ProjectMember,
    ProjectSession,
    Run,
    Task,
)
from nico_agent.domain.states import (
    AgentStatus,
    AgentVersionStatus,
    ConversationApprovalMode,
    ConversationQueueState,
    ConversationStatus,
    ConversationTurnStatus,
    RunStatus,
    TaskStatus,
    conversation_auto_approved_risks,
    require_revision,
)
from nico_agent.tool_providers.service import ToolProviderService

_TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
    RunStatus.TIMED_OUT.value,
}
_CONVERSATION_QUEUE_CAPACITY = 20


class ConversationService:
    def __init__(
        self,
        database: Database,
        *,
        approval_locked_risks: frozenset[str] = frozenset(),
        settings: Settings | None = None,
        tool_provider_service: ToolProviderService | None = None,
    ) -> None:
        if not approval_locked_risks <= {"medium", "high"}:
            raise ValueError("approval_locked_risks may contain only medium and high")
        self.database = database
        self.approval_locked_risks = approval_locked_risks
        self.coordination_service = CoordinationService(
            database,
            settings=settings,
            tool_provider_service=tool_provider_service,
        )

    async def create(self, context: TenantContext, command: ConversationCreate) -> Conversation:
        async with self.database.tenant_transaction(context) as session:
            existing = await session.scalar(
                select(Conversation).where(
                    Conversation.tenant_id == context.tenant_id,
                    Conversation.idempotency_key == command.idempotency_key,
                )
            )
            if existing is not None:
                existing_project = await self._project(
                    session,
                    context,
                    existing.project_id,
                    include_foreign_personal=True,
                )
                expected_project_id = command.project_id
                if command.mode == "personal":
                    if (
                        existing_project.kind != "personal"
                        or existing_project.owner_actor_id != context.actor_id
                    ):
                        raise DomainConflict(
                            "IDEMPOTENCY_KEY_REUSED",
                            "conversation idempotency key was reused with different input",
                        )
                    expected_project_id = existing.project_id
                expected = (
                    expected_project_id,
                    command.agent_id,
                    command.agent_version_id or existing.agent_version_id,
                    command.title,
                )
                actual = (
                    existing.project_id,
                    existing.agent_id,
                    existing.agent_version_id,
                    existing.title,
                )
                if expected != actual:
                    raise DomainConflict(
                        "IDEMPOTENCY_KEY_REUSED",
                        "conversation idempotency key was reused with different input",
                    )
                existing._conversation_mode = self._mode_for_project(existing_project.kind)
                return existing

            project = (
                await self._personal_project(session, context)
                if command.mode == "personal"
                else await self._project(session, context, command.project_id)
            )
            if command.mode == "project" and project.kind != "shared":
                raise DomainConflict(
                    "PERSONAL_PROJECT_EXPLICIT",
                    "personal Projects are resolved by mode=personal and cannot be selected",
                )
            if project.status != "active":
                raise DomainConflict("PROJECT_NOT_ACTIVE", "conversation project must be active")
            agent = await self._agent(session, context, command.agent_id, for_share=True)
            if AgentStatus(agent.status) is not AgentStatus.READY:
                raise DomainConflict("AGENT_NOT_READY", "conversation agent must be ready")
            version_id = command.agent_version_id or agent.current_version_id
            if version_id is None:
                raise DomainConflict(
                    "AGENT_VERSION_REQUIRED", "conversation agent needs a published version"
                )
            version = await self._agent_version(session, context, agent.id, version_id)
            if AgentVersionStatus(version.status) is not AgentVersionStatus.PUBLISHED:
                raise DomainConflict(
                    "AGENT_VERSION_NOT_PUBLISHED",
                    "new conversations require a published AgentVersion",
                )
            conversation = Conversation(
                tenant_id=context.tenant_id,
                project_id=project.id,
                agent_id=agent.id,
                agent_version_id=version.id,
                title=command.title,
                approval_mode=agent.default_approval_mode,
                created_by=context.actor_id,
                idempotency_key=command.idempotency_key,
            )
            conversation._conversation_mode = self._mode_for_project(project.kind)
            session.add(conversation)
            await session.flush()
            self._record(
                session,
                context,
                event_type="ConversationCreated",
                aggregate_type="conversation",
                aggregate_id=conversation.id,
                action="conversation.create",
                payload={
                    "project_id": str(project.id),
                    "agent_id": str(agent.id),
                    "agent_version_id": str(version.id),
                },
            )
            await session.flush()
            return conversation

    async def list(
        self,
        context: TenantContext,
        *,
        project_id: UUID | None = None,
        agent_id: UUID | None = None,
        status: ConversationStatus | None = None,
        mode: str | None = None,
        before: datetime | None = None,
        limit: int = 50,
    ) -> list[Conversation]:
        async with self.database.tenant_transaction(context) as session:
            statement = (
                select(Conversation, Project.kind)
                .join(
                    Project,
                    (Project.tenant_id == Conversation.tenant_id)
                    & (Project.id == Conversation.project_id),
                )
                .where(
                    Conversation.tenant_id == context.tenant_id,
                    or_(Project.kind == "shared", Project.owner_actor_id == context.actor_id),
                )
            )
            if mode == "personal":
                statement = statement.where(
                    Project.kind == "personal",
                    Project.owner_actor_id == context.actor_id,
                )
            elif mode == "project":
                statement = statement.where(Project.kind == "shared")
            if project_id is not None:
                statement = statement.where(Conversation.project_id == project_id)
            if agent_id is not None:
                statement = statement.where(Conversation.agent_id == agent_id)
            if status is not None:
                statement = statement.where(Conversation.status == status.value)
            if before is not None:
                statement = statement.where(Conversation.updated_at < before)
            rows = (
                await session.execute(
                    statement.order_by(Conversation.updated_at.desc(), Conversation.id).limit(limit)
                )
            ).all()
            conversations: list[Conversation] = []
            for conversation, project_kind in rows:
                conversation._conversation_mode = self._mode_for_project(project_kind)
                conversations.append(conversation)
            return conversations

    async def get(self, context: TenantContext, conversation_id: UUID) -> Conversation:
        async with self.database.tenant_transaction(context) as session:
            return await self._conversation(session, context, conversation_id)

    async def patch(
        self,
        context: TenantContext,
        conversation_id: UUID,
        command: ConversationPatch,
    ) -> Conversation:
        async with self.database.tenant_transaction(context) as session:
            conversation = await self._conversation(
                session, context, conversation_id, for_update=True
            )
            require_revision(
                "conversation", expected=command.expected_revision, actual=conversation.revision
            )
            if command.status is not None:
                current = ConversationStatus(conversation.status)
                if current is ConversationStatus.ARCHIVED and command.status is not current:
                    raise DomainConflict(
                        "CONVERSATION_ARCHIVED", "archived conversations cannot be reactivated"
                    )
                if command.status is ConversationStatus.ARCHIVED:
                    await self._require_no_active_run(session, conversation)
                conversation.status = command.status.value
            if command.title is not None:
                conversation.title = command.title
            approval_mode_changed = (
                command.approval_mode is not None
                and command.approval_mode.value != conversation.approval_mode
            )
            previous_approval_mode = conversation.approval_mode
            if command.approval_mode is not None:
                if conversation.created_by != context.actor_id:
                    raise AccessDenied(
                        "CONVERSATION_APPROVAL_MODE_FORBIDDEN",
                        "only the Conversation creator may change its approval mode",
                    )
                conflicts = self._locked_mode_risks(command.approval_mode)
                if conflicts:
                    raise DomainConflict(
                        "CONVERSATION_APPROVAL_MODE_LOCKED",
                        "deployment policy requires approval for risks this mode would allow",
                        details={"locked_risks": sorted(conflicts)},
                    )
                conversation.approval_mode = command.approval_mode.value
            conversation.revision += 1
            self._record(
                session,
                context,
                event_type="ConversationUpdated",
                aggregate_type="conversation",
                aggregate_id=conversation.id,
                action="conversation.update",
                payload={"status": conversation.status, "revision": conversation.revision},
            )
            if approval_mode_changed:
                self._record(
                    session,
                    context,
                    event_type="ConversationApprovalModeChanged",
                    aggregate_type="conversation",
                    aggregate_id=conversation.id,
                    action="conversation.approval_mode.change",
                    payload={
                        "previous_mode": previous_approval_mode,
                        "approval_mode": conversation.approval_mode,
                        "revision": conversation.revision,
                    },
                )
            await session.flush()
            return conversation

    def _locked_mode_risks(self, mode: ConversationApprovalMode) -> frozenset[str]:
        return conversation_auto_approved_risks(mode) & self.approval_locked_risks

    async def create_turn(
        self,
        context: TenantContext,
        conversation_id: UUID,
        command: ConversationTurnCreate,
    ) -> ConversationTurnRead:
        async with self.database.tenant_transaction(context) as session:
            conversation = await self._conversation(
                session, context, conversation_id, for_update=True
            )
            if ConversationStatus(conversation.status) is not ConversationStatus.ACTIVE:
                raise DomainConflict(
                    "CONVERSATION_ARCHIVED", "cannot add a turn to an archived conversation"
                )
            if conversation.project_session_id is not None:
                await self._require_writable_project_session(session, context, conversation)
            existing = await session.scalar(
                select(ConversationTurn).where(
                    ConversationTurn.tenant_id == context.tenant_id,
                    ConversationTurn.conversation_id == conversation.id,
                    ConversationTurn.idempotency_key == command.idempotency_key,
                )
            )
            if existing is not None:
                if existing.user_input != command.user_input:
                    raise DomainConflict(
                        "IDEMPOTENCY_KEY_REUSED",
                        "turn idempotency key was reused with different input",
                    )
                return await self._turn_view(session, context, existing)

            queued_count = await session.scalar(
                select(func.count(ConversationTurn.id))
                .join(
                    Run,
                    (Run.tenant_id == ConversationTurn.tenant_id)
                    & (Run.id == ConversationTurn.run_id),
                )
                .where(
                    ConversationTurn.tenant_id == context.tenant_id,
                    ConversationTurn.conversation_id == conversation.id,
                    Run.status == RunStatus.PENDING.value,
                )
            )
            if (queued_count or 0) >= _CONVERSATION_QUEUE_CAPACITY:
                raise DomainConflict(
                    "CONVERSATION_QUEUE_FULL",
                    "conversation queue already contains the maximum 20 unstarted Turns",
                    details={"capacity": _CONVERSATION_QUEUE_CAPACITY},
                )
            if await self._active_compaction(session, conversation) is not None:
                raise DomainConflict(
                    "CONVERSATION_COMPACTION_ACTIVE",
                    "wait for context compaction before submitting another Turn",
                )
            agent = await self._agent(session, context, conversation.agent_id)
            if AgentStatus(agent.status) is not AgentStatus.READY:
                raise DomainConflict(
                    "AGENT_NOT_READY",
                    "cannot add a turn while the conversation agent is not ready",
                )
            version = await self._agent_version(
                session, context, conversation.agent_id, conversation.agent_version_id
            )
            if AgentVersionStatus(version.status) not in {
                AgentVersionStatus.PUBLISHED,
                AgentVersionStatus.SUPERSEDED,
            }:
                raise DomainConflict(
                    "CONVERSATION_VERSION_UNAVAILABLE",
                    "the frozen AgentVersion is not executable",
                )
            sequence = (
                await session.scalar(
                    select(func.max(ConversationTurn.sequence)).where(
                        ConversationTurn.tenant_id == context.tenant_id,
                        ConversationTurn.conversation_id == conversation.id,
                    )
                )
                or 0
            ) + 1
            turn_id, task_id, run_id = uuid4(), uuid4(), uuid4()
            task = Task(
                id=task_id,
                tenant_id=context.tenant_id,
                project_id=conversation.project_id,
                project_session_id=conversation.project_session_id,
                assignee_agent_id=conversation.agent_id,
                title=f"{conversation.title} · turn {sequence}"[:300],
                input={
                    "conversation": {
                        "conversation_id": str(conversation.id),
                        "turn_id": str(turn_id),
                        "sequence": sequence,
                    },
                    "message": command.user_input,
                },
                acceptance={
                    "conversation_turn_id": str(turn_id),
                    "response_required": True,
                },
                status=TaskStatus.RUNNING.value,
            )
            run = Run(
                id=run_id,
                tenant_id=context.tenant_id,
                task_id=task.id,
                agent_id=conversation.agent_id,
                agent_version_id=conversation.agent_version_id,
                attempt=1,
                status=RunStatus.PENDING.value,
                max_steps=command.max_steps,
                token_budget=command.token_budget,
                timeout_seconds=command.timeout_seconds,
                budgets={**DEFAULT_CONVERSATION_BUDGETS, **command.budgets},
            )
            now = datetime.now(UTC)
            staged = list(
                await session.scalars(
                    select(ConversationAttachment)
                    .where(
                        ConversationAttachment.tenant_id == context.tenant_id,
                        ConversationAttachment.conversation_id == conversation.id,
                        ConversationAttachment.status == "staged",
                    )
                    .order_by(ConversationAttachment.created_at, ConversationAttachment.id)
                    .with_for_update()
                )
            )
            for attachment in staged:
                if attachment.expires_at <= now:
                    attachment.status = "expired"
                    attachment.revision += 1
            staged = [attachment for attachment in staged if attachment.status == "staged"]
            artifact_refs: list[dict] = []
            turn = ConversationTurn(
                id=turn_id,
                tenant_id=context.tenant_id,
                conversation_id=conversation.id,
                sequence=sequence,
                user_input=command.user_input,
                task_id=task.id,
                run_id=run.id,
                status="queued",
                artifact_refs=artifact_refs,
                idempotency_key=command.idempotency_key,
            )
            # Materialize the explicit Task -> Run -> Turn dependency chain before
            # advancing the Conversation pointer. All flushes remain in one DB
            # transaction, so callers can never observe a partial chain.
            session.add(task)
            await session.flush([task])
            session.add(run)
            await session.flush([run])
            for attachment in staged:
                artifact = Artifact(
                    tenant_id=context.tenant_id,
                    project_id=conversation.project_id,
                    owner_run_id=run.id,
                    name=attachment.name,
                    content_type=attachment.content_type,
                    artifact_type="conversation_attachment",
                    status="available",
                    object_key=attachment.object_key,
                    sha256=attachment.sha256,
                    size_bytes=attachment.size_bytes,
                    metadata_json={
                        "source": "conversation_attachment",
                        "conversation_id": str(conversation.id),
                        "attachment_id": str(attachment.id),
                        "text_excerpt": attachment.text_excerpt,
                    },
                    idempotency_key=f"conversation-attachment:{attachment.id}",
                    available_at=now,
                )
                session.add(artifact)
                await session.flush([artifact])
                artifact_refs.append(self._artifact_ref(artifact))
            session.add(turn)
            await session.flush([turn])
            for attachment, reference in zip(staged, artifact_refs, strict=True):
                attachment.status = "consumed"
                attachment.consumed_by_turn_id = turn.id
                attachment.artifact_id = UUID(reference["artifact_id"])
                attachment.revision += 1
            conversation.last_turn_id = turn.id
            conversation.revision += 1
            conversation.updated_at = datetime.now(UTC)
            for event_type, aggregate_type, aggregate_id, action, payload, event_run_id in (
                (
                    "TaskCreated",
                    "task",
                    task.id,
                    "task.create",
                    {"conversation_turn_id": str(turn.id), "status": task.status},
                    run.id,
                ),
                (
                    "RunCreated",
                    "run",
                    run.id,
                    "run.create",
                    {"task_id": str(task.id), "attempt": 1},
                    run.id,
                ),
                (
                    "ConversationTurnQueued",
                    "conversation_turn",
                    turn.id,
                    "conversation.turn.create",
                    {
                        "conversation_id": str(conversation.id),
                        "sequence": sequence,
                        "task_id": str(task.id),
                        "run_id": str(run.id),
                    },
                    run.id,
                ),
            ):
                self._record(
                    session,
                    context,
                    event_type=event_type,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    action=action,
                    payload=payload,
                    run_id=event_run_id,
                )
            await session.flush()
            return self._view(turn, run)

    async def compact(
        self,
        context: TenantContext,
        conversation_id: UUID,
        command: ConversationCompact,
    ) -> ConversationCompactAccepted:
        """Queue one audited model Run that replaces covered Turns with a summary."""

        async with self.database.tenant_transaction(context) as session:
            conversation = await self._conversation(
                session, context, conversation_id, for_update=True
            )
            if ConversationStatus(conversation.status) is not ConversationStatus.ACTIVE:
                raise DomainConflict(
                    "CONVERSATION_ARCHIVED", "cannot compact an archived conversation"
                )
            await session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtextextended(
                            f"conversation-compact:{context.tenant_id}:{conversation.id}:{command.idempotency_key}",
                            0,
                        )
                    )
                )
            )
            tasks = list(
                await session.scalars(
                    select(Task).where(
                        Task.tenant_id == context.tenant_id,
                        Task.project_id == conversation.project_id,
                    )
                )
            )
            for candidate in tasks:
                marker = candidate.acceptance.get("conversation_compaction", {})
                if (
                    isinstance(marker, dict)
                    and marker.get("conversation_id") == str(conversation.id)
                    and marker.get("idempotency_key") == command.idempotency_key
                ):
                    existing_run = await session.scalar(
                        select(Run).where(
                            Run.tenant_id == context.tenant_id,
                            Run.task_id == candidate.id,
                        )
                    )
                    if existing_run is None:
                        raise ResourceNotFound("run", str(candidate.id))
                    return ConversationCompactAccepted(
                        conversation_id=conversation.id,
                        task_id=candidate.id,
                        run_id=existing_run.id,
                        through_sequence=int(marker["through_sequence"]),
                        input_hash=str(marker["input_hash"]),
                        status=RunStatus(existing_run.status),
                        replayed=True,
                    )
            await self._require_no_active_run(session, conversation)
            if await self._active_compaction(session, conversation) is not None:
                raise DomainConflict(
                    "CONVERSATION_COMPACTION_ACTIVE",
                    "wait for the active context compaction before starting another",
                )
            turns = list(
                await session.scalars(
                    select(ConversationTurn)
                    .where(
                        ConversationTurn.tenant_id == context.tenant_id,
                        ConversationTurn.conversation_id == conversation.id,
                        ConversationTurn.sequence > conversation.summary_through_sequence,
                        ConversationTurn.status == ConversationTurnStatus.COMPLETED.value,
                    )
                    .order_by(ConversationTurn.sequence)
                )
            )
            if not turns:
                raise DomainConflict(
                    "CONVERSATION_NOT_COMPACTABLE", "no completed Turns require compaction"
                )
            through_sequence = turns[-1].sequence
            transcript = [
                {
                    "sequence": item.sequence,
                    "user": item.user_input,
                    "assistant": item.assistant_output,
                    "artifacts": item.artifact_refs,
                }
                for item in turns
            ]
            source = {
                "previous_summary": conversation.summary,
                "previous_through_sequence": conversation.summary_through_sequence,
                "turns": transcript,
            }
            encoded = json.dumps(
                source, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
            ).encode()
            input_hash = hashlib.sha256(encoded).hexdigest()
            marker = {
                "conversation_id": str(conversation.id),
                "through_sequence": through_sequence,
                "input_hash": input_hash,
                "idempotency_key": command.idempotency_key,
            }
            task = Task(
                tenant_id=context.tenant_id,
                project_id=conversation.project_id,
                assignee_agent_id=conversation.agent_id,
                title=f"{conversation.title} · context compact"[:300],
                input={
                    "operation": "conversation_compaction",
                    "instruction": (
                        "Summarize the supplied conversation faithfully and compactly. Preserve "
                        "decisions, constraints, unresolved work, user preferences, identifiers, "
                        "and artifact references. Do not invent facts. Return summary text only."
                    ),
                    "source": source,
                },
                acceptance={
                    "conversation_compaction": marker,
                    "response_required": True,
                },
                status=TaskStatus.RUNNING.value,
            )
            session.add(task)
            await session.flush([task])
            run = Run(
                tenant_id=context.tenant_id,
                task_id=task.id,
                agent_id=conversation.agent_id,
                agent_version_id=conversation.agent_version_id,
                attempt=1,
                status=RunStatus.PENDING.value,
                max_steps=1,
                token_budget=command.token_budget,
                timeout_seconds=command.timeout_seconds,
                budgets={"conversation_compaction": True},
            )
            session.add(run)
            await session.flush([run])
            self._record(
                session,
                context,
                event_type="ConversationCompactionQueued",
                aggregate_type="conversation",
                aggregate_id=conversation.id,
                action="conversation.compact",
                payload={**marker, "task_id": str(task.id), "run_id": str(run.id)},
                run_id=run.id,
            )
            await session.flush()
            return ConversationCompactAccepted(
                conversation_id=conversation.id,
                task_id=task.id,
                run_id=run.id,
                through_sequence=through_sequence,
                input_hash=input_hash,
                status=RunStatus.PENDING,
            )

    async def list_turns(
        self,
        context: TenantContext,
        conversation_id: UUID,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[ConversationTurnRead]:
        async with self.database.tenant_transaction(context) as session:
            await self._conversation(session, context, conversation_id)
            rows = (
                await session.execute(
                    select(ConversationTurn, Run)
                    .join(
                        Run,
                        (Run.tenant_id == ConversationTurn.tenant_id)
                        & (Run.id == ConversationTurn.run_id),
                    )
                    .where(
                        ConversationTurn.tenant_id == context.tenant_id,
                        ConversationTurn.conversation_id == conversation_id,
                        ConversationTurn.sequence > after_sequence,
                    )
                    .order_by(ConversationTurn.sequence)
                    .limit(limit)
                )
            ).all()
            return [self._view(turn, run) for turn, run in rows]

    async def get_queue(
        self,
        context: TenantContext,
        conversation_id: UUID,
    ) -> ConversationQueueRead:
        async with self.database.tenant_transaction(context) as session:
            conversation = await self._conversation(session, context, conversation_id)
            return await self._queue_view(session, conversation)

    async def resume_queue(
        self,
        context: TenantContext,
        conversation_id: UUID,
        command: ConversationQueueResume,
    ) -> ConversationQueueRead:
        async with self.database.tenant_transaction(context) as session:
            conversation = await self._conversation(
                session, context, conversation_id, for_update=True
            )
            replay = await session.scalar(
                select(AuditRecord.id).where(
                    AuditRecord.tenant_id == context.tenant_id,
                    AuditRecord.action == "conversation.queue.resume",
                    AuditRecord.resource_type == "conversation",
                    AuditRecord.resource_id == conversation.id,
                    AuditRecord.details.contains({"idempotency_key": command.idempotency_key}),
                )
            )
            if replay is not None:
                return await self._queue_view(session, conversation)
            require_revision(
                "conversation",
                expected=command.expected_revision,
                actual=conversation.revision,
            )
            queue_state = ConversationQueueState(conversation.queue_state)
            if queue_state is not ConversationQueueState.PAUSED:
                raise DomainConflict(
                    "CONVERSATION_QUEUE_NOT_PAUSED",
                    "conversation queue is not paused",
                )
            pause_turn = await session.scalar(
                select(ConversationTurn).where(
                    ConversationTurn.tenant_id == context.tenant_id,
                    ConversationTurn.conversation_id == conversation.id,
                    ConversationTurn.id == conversation.queue_pause_turn_id,
                )
            )
            if pause_turn is None:
                raise ResourceNotFound("conversation_turn", str(conversation.queue_pause_turn_id))
            pause_run = await session.scalar(
                select(Run).where(
                    Run.tenant_id == context.tenant_id,
                    Run.id == pause_turn.run_id,
                )
            )
            if pause_run is None:
                raise ResourceNotFound("run", str(pause_turn.run_id))
            if pause_run.status not in _TERMINAL_RUN_STATUSES:
                raise DomainConflict(
                    "CONVERSATION_RECOVERY_ACTIVE",
                    "wait for the pause-causing Turn retry to finish before resuming",
                )

            previous_reason = conversation.queue_pause_reason
            previous_turn_id = conversation.queue_pause_turn_id
            conversation.queue_state = ConversationQueueState.ACTIVE.value
            conversation.queue_pause_reason = None
            conversation.queue_pause_turn_id = None
            conversation.queue_paused_at = None
            conversation.revision += 1
            conversation.updated_at = datetime.now(UTC)
            self._record(
                session,
                context,
                event_type="ConversationQueueResumed",
                aggregate_type="conversation",
                aggregate_id=conversation.id,
                action="conversation.queue.resume",
                payload={
                    "idempotency_key": command.idempotency_key,
                    "previous_pause_reason": previous_reason,
                    "previous_pause_turn_id": str(previous_turn_id),
                    "revision": conversation.revision,
                },
            )
            await session.flush()
            return await self._queue_view(session, conversation)

    async def get_turn(self, context: TenantContext, turn_id: UUID) -> ConversationTurnRead:
        async with self.database.tenant_transaction(context) as session:
            turn = await self._turn(session, context, turn_id)
            return await self._turn_view(session, context, turn)

    async def cancel_turn(
        self,
        context: TenantContext,
        turn_id: UUID,
        *,
        expected_run_revision: int,
    ) -> ConversationTurnRead:
        async with self.database.tenant_transaction(context) as session:
            turn = await self._turn(session, context, turn_id)
            run_id = turn.run_id
        await self.coordination_service.cancel_tree(
            context,
            run_id,
            expected_revision=expected_run_revision,
        )
        return await self.get_turn(context, turn_id)

    async def retry_turn(
        self,
        context: TenantContext,
        turn_id: UUID,
        command: ConversationTurnRetry,
    ) -> ConversationTurnRead:
        async with self.database.tenant_transaction(context) as session:
            turn = await self._turn(session, context, turn_id, for_update=True)
            conversation = await self._conversation(
                session, context, turn.conversation_id, for_update=True
            )
            if ConversationStatus(conversation.status) is not ConversationStatus.ACTIVE:
                raise DomainConflict(
                    "CONVERSATION_ARCHIVED", "cannot retry a turn in an archived conversation"
                )
            if (
                ConversationQueueState(conversation.queue_state)
                is not ConversationQueueState.PAUSED
                or conversation.queue_pause_turn_id != turn.id
            ):
                raise DomainConflict(
                    "CONVERSATION_RETRY_NOT_PAUSE_CAUSE",
                    "only the Turn that paused the conversation queue can be retried",
                )
            if turn.run_id != command.expected_run_id:
                raise DomainConflict(
                    "CONVERSATION_TURN_RUN_CHANGED",
                    "the conversation turn now points to a different Run",
                )
            previous = await session.scalar(
                select(Run)
                .where(Run.tenant_id == context.tenant_id, Run.id == turn.run_id)
                .with_for_update()
            )
            if previous is None:
                raise ResourceNotFound("run", str(turn.run_id))
            require_revision(
                "run", expected=command.expected_run_revision, actual=previous.revision
            )
            if RunStatus(previous.status) not in {RunStatus.FAILED, RunStatus.TIMED_OUT}:
                raise DomainConflict(
                    "RUN_NOT_RETRYABLE", "only failed or timed-out conversation Runs can retry"
                )

            agent = await self._agent(session, context, conversation.agent_id)
            if AgentStatus(agent.status) is not AgentStatus.READY:
                raise DomainConflict(
                    "AGENT_NOT_READY", "cannot retry while the conversation agent is not ready"
                )
            version = await self._agent_version(
                session, context, conversation.agent_id, conversation.agent_version_id
            )
            if AgentVersionStatus(version.status) not in {
                AgentVersionStatus.PUBLISHED,
                AgentVersionStatus.SUPERSEDED,
            }:
                raise DomainConflict(
                    "CONVERSATION_VERSION_UNAVAILABLE",
                    "the frozen AgentVersion is not executable",
                )
            task = await session.scalar(
                select(Task)
                .where(Task.tenant_id == context.tenant_id, Task.id == turn.task_id)
                .with_for_update()
            )
            if task is None:
                raise ResourceNotFound("task", str(turn.task_id))
            if TaskStatus(task.status) not in {TaskStatus.RUNNING, TaskStatus.FAILED}:
                raise DomainConflict("TASK_NOT_RUNNABLE", "task state does not allow a retry")

            latest_attempt = (
                await session.scalar(
                    select(func.max(Run.attempt)).where(
                        Run.tenant_id == context.tenant_id,
                        Run.task_id == task.id,
                    )
                )
                or 0
            )
            new_run = Run(
                tenant_id=context.tenant_id,
                task_id=task.id,
                agent_id=conversation.agent_id,
                agent_version_id=conversation.agent_version_id,
                retry_of_run_id=previous.id,
                attempt=latest_attempt + 1,
                status=RunStatus.PENDING.value,
                max_steps=command.max_steps or previous.max_steps,
                token_budget=(
                    command.token_budget
                    if command.token_budget is not None
                    else previous.token_budget
                ),
                timeout_seconds=(
                    command.timeout_seconds
                    if command.timeout_seconds is not None
                    else previous.timeout_seconds
                ),
                budgets=command.budgets if command.budgets is not None else previous.budgets,
            )
            session.add(new_run)
            await session.flush([new_run])

            previous_refs = list(turn.artifact_refs)
            next_refs: list[dict] = []
            for reference in previous_refs:
                artifact_id = reference.get("artifact_id") if isinstance(reference, dict) else None
                if not artifact_id:
                    continue
                original = await session.scalar(
                    select(Artifact).where(
                        Artifact.tenant_id == context.tenant_id,
                        Artifact.id == UUID(str(artifact_id)),
                        Artifact.status == "available",
                    )
                )
                if original is None:
                    continue
                replay = Artifact(
                    tenant_id=context.tenant_id,
                    project_id=original.project_id,
                    owner_run_id=new_run.id,
                    name=original.name,
                    content_type=original.content_type,
                    artifact_type=original.artifact_type,
                    status="available",
                    object_key=original.object_key,
                    sha256=original.sha256,
                    size_bytes=original.size_bytes,
                    metadata_json={
                        **original.metadata_json,
                        "retry_source_artifact_id": str(original.id),
                    },
                    idempotency_key=f"retry:{turn.id}:{original.id}",
                    available_at=original.available_at,
                )
                session.add(replay)
                await session.flush([replay])
                next_refs.append(self._artifact_ref(replay))

            if TaskStatus(task.status) is TaskStatus.FAILED:
                task.status = TaskStatus.RUNNING.value
                task.revision += 1
                self._record(
                    session,
                    context,
                    event_type="TaskRetryStarted",
                    aggregate_type="task",
                    aggregate_id=task.id,
                    action="task.retry",
                    payload={"run_id": str(new_run.id), "revision": task.revision},
                    run_id=new_run.id,
                )
            turn.run_id = new_run.id
            turn.status = ConversationTurnStatus.QUEUED.value
            turn.assistant_output = None
            turn.artifact_refs = next_refs
            turn.usage = {}
            turn.error = None
            turn.revision += 1
            conversation.revision += 1
            conversation.updated_at = datetime.now(UTC)
            for event_type, aggregate_type, aggregate_id, action, payload in (
                (
                    "RunRetried",
                    "run",
                    new_run.id,
                    "run.retry",
                    {
                        "retry_of_run_id": str(previous.id),
                        "attempt": new_run.attempt,
                        "conversation_turn_id": str(turn.id),
                    },
                ),
                (
                    "ConversationTurnRetried",
                    "conversation_turn",
                    turn.id,
                    "conversation.turn.retry",
                    {
                        "previous_run_id": str(previous.id),
                        "run_id": str(new_run.id),
                        "attempt": new_run.attempt,
                    },
                ),
            ):
                self._record(
                    session,
                    context,
                    event_type=event_type,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    action=action,
                    payload=payload,
                    run_id=new_run.id,
                )
            await session.flush()
            return self._view(turn, new_run)

    @staticmethod
    def _view(turn: ConversationTurn, run: Run) -> ConversationTurnRead:
        return ConversationTurnRead(
            id=turn.id,
            conversation_id=turn.conversation_id,
            sequence=turn.sequence,
            user_input=turn.user_input,
            task_id=turn.task_id,
            run_id=turn.run_id,
            status=turn.status,
            run_status=run.status,
            run_revision=run.revision,
            assistant_output=turn.assistant_output,
            artifact_refs=turn.artifact_refs,
            usage=turn.usage,
            error=turn.error,
            revision=turn.revision,
            created_at=turn.created_at,
            updated_at=turn.updated_at,
        )

    async def _queue_view(
        self,
        session: AsyncSession,
        conversation: Conversation,
    ) -> ConversationQueueRead:
        rows = (
            await session.execute(
                select(ConversationTurn, Run)
                .join(
                    Run,
                    (Run.tenant_id == ConversationTurn.tenant_id)
                    & (Run.id == ConversationTurn.run_id),
                )
                .where(
                    ConversationTurn.tenant_id == conversation.tenant_id,
                    ConversationTurn.conversation_id == conversation.id,
                    or_(
                        Run.status.not_in(_TERMINAL_RUN_STATUSES),
                        ConversationTurn.id == conversation.queue_pause_turn_id,
                    ),
                )
                .order_by(ConversationTurn.sequence)
            )
        ).all()
        views = [(turn, run, self._view(turn, run)) for turn, run in rows]
        nonterminal = [item for item in views if item[1].status not in _TERMINAL_RUN_STATUSES]
        queued = [item[2] for item in nonterminal if item[1].status == RunStatus.PENDING.value]
        active = next(
            (item[2] for item in nonterminal if item[1].status != RunStatus.PENDING.value),
            None,
        )
        pause = next(
            (item[2] for item in views if item[0].id == conversation.queue_pause_turn_id),
            None,
        )
        return ConversationQueueRead(
            conversation_id=conversation.id,
            revision=conversation.revision,
            state=conversation.queue_state,
            pause_reason=conversation.queue_pause_reason,
            pause_turn_id=conversation.queue_pause_turn_id,
            paused_at=conversation.queue_paused_at,
            head_turn=nonterminal[0][2] if nonterminal else None,
            active_turn=active,
            pause_turn=pause,
            queued_turns=queued,
            queued_count=len(queued),
            capacity=_CONVERSATION_QUEUE_CAPACITY,
        )

    async def _turn_view(
        self,
        session: AsyncSession,
        context: TenantContext,
        turn: ConversationTurn,
    ) -> ConversationTurnRead:
        run = await session.scalar(
            select(Run).where(Run.tenant_id == context.tenant_id, Run.id == turn.run_id)
        )
        if run is None:
            raise ResourceNotFound("run", str(turn.run_id))
        return self._view(turn, run)

    @staticmethod
    async def _require_no_active_run(session: AsyncSession, conversation: Conversation) -> None:
        active_run = await session.scalar(
            select(Run.id)
            .join(
                ConversationTurn,
                (ConversationTurn.tenant_id == Run.tenant_id) & (ConversationTurn.run_id == Run.id),
            )
            .where(
                ConversationTurn.tenant_id == conversation.tenant_id,
                ConversationTurn.conversation_id == conversation.id,
                Run.status.not_in(_TERMINAL_RUN_STATUSES),
            )
            .limit(1)
        )
        if active_run is not None:
            raise DomainConflict(
                "CONVERSATION_RUN_ACTIVE", "cannot archive a conversation with an active Run"
            )

    @staticmethod
    async def _require_writable_project_session(
        session: AsyncSession,
        context: TenantContext,
        conversation: Conversation,
    ) -> None:
        project = await session.scalar(
            select(Project).where(
                Project.tenant_id == context.tenant_id,
                Project.id == conversation.project_id,
            )
        )
        if project is None:
            raise ResourceNotFound("project", str(conversation.project_id))
        if project.status != "active":
            raise DomainConflict("PROJECT_ARCHIVED", "archived Projects are read-only")
        project_session = await session.scalar(
            select(ProjectSession).where(
                ProjectSession.tenant_id == context.tenant_id,
                ProjectSession.project_id == conversation.project_id,
                ProjectSession.id == conversation.project_session_id,
            )
        )
        if project_session is None:
            raise ResourceNotFound("project_session", str(conversation.project_session_id))
        member = await session.scalar(
            select(ProjectMember).where(
                ProjectMember.tenant_id == context.tenant_id,
                ProjectMember.id == project_session.project_member_id,
            )
        )
        if member is None or member.status != "active" or project_session.status != "active":
            raise DomainConflict(
                "PROJECT_MEMBER_INACTIVE",
                "only an active Project member Session accepts messages",
            )
        if project_session.current_conversation_id != conversation.id:
            raise DomainConflict(
                "PROJECT_SESSION_CONVERSATION_STALE",
                "this historical Conversation is read-only; open the current Project Session",
            )

    @staticmethod
    async def _active_compaction(session: AsyncSession, conversation: Conversation) -> Run | None:
        rows = (
            await session.execute(
                select(Run, Task)
                .join(
                    Task,
                    (Task.tenant_id == Run.tenant_id) & (Task.id == Run.task_id),
                )
                .where(
                    Run.tenant_id == conversation.tenant_id,
                    Task.project_id == conversation.project_id,
                    Run.status.not_in(_TERMINAL_RUN_STATUSES),
                )
            )
        ).all()
        for run, task in rows:
            marker = task.acceptance.get("conversation_compaction", {})
            if isinstance(marker, dict) and marker.get("conversation_id") == str(conversation.id):
                return run
        return None

    @staticmethod
    def _artifact_ref(artifact: Artifact) -> dict:
        reference = {
            "artifact_id": str(artifact.id),
            "owner_run_id": str(artifact.owner_run_id),
            "name": artifact.name,
            "content_type": artifact.content_type,
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
        }
        excerpt = artifact.metadata_json.get("text_excerpt")
        if isinstance(excerpt, str) and excerpt:
            reference["summary"] = excerpt
        return reference

    @staticmethod
    async def _conversation(
        session: AsyncSession,
        context: TenantContext,
        conversation_id: UUID,
        *,
        for_update: bool = False,
    ) -> Conversation:
        statement = select(Conversation).where(
            Conversation.tenant_id == context.tenant_id,
            Conversation.id == conversation_id,
        )
        if for_update:
            statement = statement.with_for_update()
        value = await session.scalar(statement)
        if value is None:
            raise ResourceNotFound("conversation", str(conversation_id))
        project = await ConversationService._project(
            session,
            context,
            value.project_id,
            include_foreign_personal=True,
        )
        if project.kind == "personal" and project.owner_actor_id != context.actor_id:
            raise ResourceNotFound("conversation", str(conversation_id))
        value._conversation_mode = ConversationService._mode_for_project(project.kind)
        return value

    @staticmethod
    def _mode_for_project(project_kind: str) -> str:
        return "personal" if project_kind == "personal" else "project"

    @staticmethod
    async def _turn(
        session: AsyncSession,
        context: TenantContext,
        turn_id: UUID,
        *,
        for_update: bool = False,
    ) -> ConversationTurn:
        statement = (
            select(ConversationTurn)
            .join(
                Conversation,
                (Conversation.tenant_id == ConversationTurn.tenant_id)
                & (Conversation.id == ConversationTurn.conversation_id),
            )
            .join(
                Project,
                (Project.tenant_id == Conversation.tenant_id)
                & (Project.id == Conversation.project_id),
            )
            .where(
                ConversationTurn.tenant_id == context.tenant_id,
                ConversationTurn.id == turn_id,
                or_(Project.kind == "shared", Project.owner_actor_id == context.actor_id),
            )
        )
        if for_update:
            statement = statement.with_for_update()
        value = await session.scalar(statement)
        if value is None:
            raise ResourceNotFound("conversation_turn", str(turn_id))
        return value

    @staticmethod
    async def _project(
        session: AsyncSession,
        context: TenantContext,
        project_id: UUID | None,
        *,
        include_foreign_personal: bool = False,
    ) -> Project:
        if project_id is None:
            raise ResourceNotFound("project", "None")
        value = await session.scalar(
            select(Project).where(
                Project.tenant_id == context.tenant_id,
                Project.id == project_id,
            )
        )
        if value is None:
            raise ResourceNotFound("project", str(project_id))
        if (
            not include_foreign_personal
            and value.kind == "personal"
            and value.owner_actor_id != context.actor_id
        ):
            raise ResourceNotFound("project", str(project_id))
        return value

    async def _personal_project(
        self,
        session: AsyncSession,
        context: TenantContext,
    ) -> Project:
        actor_hash = hashlib.sha256(context.actor_id.encode()).hexdigest()
        project_id = uuid4()
        inserted = await session.scalar(
            pg_insert(Project.__table__)
            .values(
                id=project_id,
                tenant_id=context.tenant_id,
                name=f"Personal workspace {actor_hash[:16]}",
                kind="personal",
                owner_actor_id=context.actor_id,
                idempotency_key=f"personal-project:{actor_hash}",
                description=None,
                metadata={"_nico_collaboration": {"managed": True, "system": True}},
                status="active",
                revision=1,
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "owner_actor_id"],
                index_where=text("kind = 'personal'"),
            )
            .returning(Project.id)
        )
        project = await session.scalar(
            select(Project).where(
                Project.tenant_id == context.tenant_id,
                Project.owner_actor_id == context.actor_id,
                Project.kind == "personal",
            )
        )
        if project is None:
            raise DomainConflict(
                "PERSONAL_PROJECT_RESOLUTION_FAILED",
                "could not resolve the actor Personal Project",
            )
        if inserted is not None:
            self._record(
                session,
                context,
                event_type="PersonalProjectCreated",
                aggregate_type="project",
                aggregate_id=project.id,
                action="personal_project.create",
                payload={"kind": "personal", "owner_actor_id": context.actor_id},
            )
        return project

    @staticmethod
    async def _agent(
        session: AsyncSession,
        context: TenantContext,
        agent_id: UUID,
        *,
        for_update: bool = False,
        for_share: bool = False,
    ) -> Agent:
        statement = select(Agent).where(
            Agent.tenant_id == context.tenant_id,
            Agent.id == agent_id,
        )
        if for_update:
            statement = statement.with_for_update()
        elif for_share:
            statement = statement.with_for_update(read=True)
        value = await session.scalar(statement)
        if value is None:
            raise ResourceNotFound("agent", str(agent_id))
        return value

    @staticmethod
    async def _agent_version(
        session: AsyncSession,
        context: TenantContext,
        agent_id: UUID,
        version_id: UUID,
    ) -> AgentVersion:
        value = await session.scalar(
            select(AgentVersion).where(
                AgentVersion.tenant_id == context.tenant_id,
                AgentVersion.agent_id == agent_id,
                AgentVersion.id == version_id,
            )
        )
        if value is None:
            raise ResourceNotFound("agent_version", str(version_id))
        return value

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
