"""Controlled Memory publication, immutable revision, invalidation, expiry and tombstone."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainConflict, ResourceNotFound
from nico_agent.domain.models import AuditRecord, Event, GrowthSource, Memory
from nico_agent.domain.states import require_revision
from nico_agent.growth.approval import GrowthApprovalService
from nico_agent.growth.contracts import canonical_hash
from nico_agent.memory.chunking import content_hash, normalize_text
from nico_agent.memory.service import MemoryIndexService


class MemoryLifecycleResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    memory_key: UUID
    version: int
    memory_type: str
    scope_type: str
    status: str
    content_hash: str
    revision: int
    indexed: bool = False
    chunk_count: int = 0
    already_in_state: bool = False


class MemoryLifecycleService:
    revision_generator_name = "manual-memory-revision"
    revision_generator_version = "1.0.0"

    def __init__(
        self,
        database: Database,
        *,
        indexer: MemoryIndexService | None = None,
    ) -> None:
        self.database = database
        self.indexer = indexer or MemoryIndexService(database)

    async def publish(
        self,
        context: TenantContext,
        memory_id: UUID,
        *,
        expected_revision: int,
    ) -> MemoryLifecycleResult:
        now = datetime.now(UTC)
        already_active = False
        async with self.database.tenant_transaction(context) as session:
            memory = await self._lock_memory(session, memory_id)
            if memory.status == "active":
                already_active = True
            else:
                require_revision(
                    "memory",
                    expected=expected_revision,
                    actual=memory.revision,
                )
                if memory.status != "candidate":
                    raise DomainConflict(
                        "MEMORY_NOT_PUBLISHABLE",
                        "only a candidate memory can be published",
                        details={"memory_id": str(memory_id), "status": memory.status},
                    )
                latest = await session.scalar(
                    select(Memory)
                    .where(Memory.memory_key == memory.memory_key)
                    .order_by(Memory.version.desc(), Memory.id.desc())
                    .limit(1)
                    .with_for_update()
                )
                if latest is None or latest.id != memory.id:
                    raise DomainConflict(
                        "MEMORY_VERSION_NOT_LATEST",
                        "only the latest memory version can be published",
                        details={"memory_id": str(memory_id)},
                    )
                self._require_content_hash(memory)
                if memory.expires_at is not None and memory.expires_at <= now:
                    raise DomainConflict(
                        "MEMORY_EXPIRED",
                        "an expired memory candidate cannot be published",
                        details={"memory_id": str(memory_id)},
                    )
                source = await session.scalar(
                    select(GrowthSource)
                    .where(GrowthSource.memory_id == memory.id)
                    .order_by(GrowthSource.created_at, GrowthSource.id)
                )
                if source is None:
                    raise DomainConflict(
                        "MEMORY_SOURCE_MISSING",
                        "memory publication requires an immutable growth source",
                    )
                evaluation = await GrowthApprovalService.latest_evaluation(
                    session, "memory", memory.id, memory.content_hash
                )
                if evaluation is None or not (
                    evaluation.status == "completed" and evaluation.verdict == "pass"
                ):
                    raise DomainConflict(
                        "GROWTH_EVALUATION_NOT_PASSED",
                        "latest evaluation must pass before memory publication",
                        details={"memory_id": str(memory_id)},
                    )
                approval = await GrowthApprovalService.valid_approval(
                    session,
                    "memory",
                    memory.id,
                    memory.content_hash,
                    now=now,
                )
                if approval is None or approval.reviewer is None:
                    raise DomainConflict(
                        "GROWTH_APPROVAL_REQUIRED",
                        "memory publication requires an unexpired human approval",
                        details={"memory_id": str(memory_id)},
                    )
                if approval.requester == approval.reviewer:
                    raise DomainConflict(
                        "GROWTH_APPROVAL_INVALID",
                        "self-reviewed approval cannot publish a memory",
                        details={"memory_id": str(memory_id)},
                    )

                active = await session.scalar(
                    select(Memory)
                    .where(
                        Memory.memory_key == memory.memory_key,
                        Memory.status == "active",
                        Memory.id != memory.id,
                    )
                    .with_for_update()
                )
                if active is not None:
                    active.status = "invalidated"
                    active.invalidated_at = now
                    active.revision += 1
                    self._record(
                        session,
                        context,
                        active,
                        event_type="MemorySuperseded",
                        action="memory.supersede",
                        details={"replacement_memory_id": str(memory.id)},
                    )
                    await session.flush()

                memory.status = "active"
                memory.approved_at = now
                memory.revision += 1
                self._record(
                    session,
                    context,
                    memory,
                    event_type="MemoryPublished",
                    action="memory.publish",
                    details={
                        "evaluation_id": str(evaluation.id),
                        "approval_id": str(approval.id),
                        "source_id": str(source.id),
                    },
                )
                await session.flush()
            indexed = await self.indexer.index_memory_in_session(session, memory)
            return self._result(memory, already_in_state=already_active).model_copy(
                update={"indexed": True, "chunk_count": indexed.chunk_count}
            )

    async def revise(
        self,
        context: TenantContext,
        memory_id: UUID,
        *,
        content: str,
        reason: str,
        expected_revision: int,
        confidence: float | None = None,
        expires_at: datetime | None = None,
    ) -> MemoryLifecycleResult:
        normalized = normalize_text(content)
        reason = self._reason(reason)
        if confidence is not None and not 0 <= confidence <= 1:
            raise ValueError("memory confidence must be between 0 and 1")
        async with self.database.tenant_transaction(context) as session:
            previous = await self._lock_memory(session, memory_id)
            require_revision(
                "memory",
                expected=expected_revision,
                actual=previous.revision,
            )
            if previous.status == "deleted":
                raise DomainConflict(
                    "MEMORY_DELETED",
                    "a deleted memory cannot be revised",
                    details={"memory_id": str(memory_id)},
                )
            latest = await session.scalar(
                select(Memory)
                .where(Memory.memory_key == previous.memory_key)
                .order_by(Memory.version.desc(), Memory.id.desc())
                .limit(1)
                .with_for_update()
            )
            if latest is None or latest.id != previous.id:
                raise DomainConflict(
                    "MEMORY_REVISION_STALE",
                    "only the latest memory version can be revised",
                    details={"memory_id": str(memory_id)},
                )
            new_hash = content_hash(normalized)
            if new_hash == previous.content_hash:
                raise DomainConflict(
                    "MEMORY_REVISION_UNCHANGED",
                    "memory revision must change normalized content",
                )
            source = await session.scalar(
                select(GrowthSource)
                .where(GrowthSource.memory_id == previous.id)
                .order_by(GrowthSource.created_at, GrowthSource.id)
            )
            if source is None:
                raise DomainConflict(
                    "MEMORY_SOURCE_MISSING",
                    "memory revision requires an existing immutable source",
                )
            revision_expiry = expires_at if expires_at is not None else previous.expires_at
            if previous.memory_type == "working" and revision_expiry is None:
                raise DomainConflict(
                    "MEMORY_WORKING_EXPIRY_REQUIRED",
                    "working memory revisions require an expiry",
                )
            revised = Memory(
                tenant_id=context.tenant_id,
                memory_key=previous.memory_key,
                version=previous.version + 1,
                memory_type=previous.memory_type,
                scope_type=previous.scope_type,
                project_id=previous.project_id,
                agent_id=previous.agent_id,
                status="candidate",
                content=normalized,
                confidence=previous.confidence if confidence is None else confidence,
                content_hash=new_hash,
                supersedes_id=previous.id,
                created_by=context.actor_id,
                expires_at=revision_expiry,
            )
            session.add(revised)
            await session.flush()
            source_hash = canonical_hash(
                {
                    "tenant_id": context.tenant_id,
                    "memory_key": previous.memory_key,
                    "version": revised.version,
                    "content_hash": revised.content_hash,
                    "parent_source_hash": source.source_hash,
                    "reason": reason,
                    "generator": self.revision_generator_name,
                    "generator_version": self.revision_generator_version,
                }
            )
            revision_source = GrowthSource(
                tenant_id=context.tenant_id,
                subject_type="memory",
                memory_id=revised.id,
                run_id=source.run_id,
                run_step_id=source.run_step_id,
                tool_call_id=source.tool_call_id,
                runtime_session_id=source.runtime_session_id,
                agent_version_id=source.agent_version_id,
                trajectory_hash=source.trajectory_hash,
                generator_name=self.revision_generator_name,
                generator_version=self.revision_generator_version,
                source_hash=source_hash,
                snapshot={
                    "candidate_key": source_hash,
                    "policy_hash": source.snapshot.get("policy_hash"),
                    "trajectory": source.snapshot.get("trajectory"),
                    "revision": {
                        "parent_memory_id": str(previous.id),
                        "parent_source_hash": source.source_hash,
                        "reason": reason,
                        "actor": context.actor_id,
                    },
                },
            )
            session.add(revision_source)
            await session.flush()
            self._record(
                session,
                context,
                revised,
                event_type="MemoryRevisionCandidateCreated",
                action="memory.revise",
                details={
                    "supersedes_id": str(previous.id),
                    "source_hash": source_hash,
                    "reason": reason,
                },
            )
            return self._result(revised)

    async def invalidate(
        self,
        context: TenantContext,
        memory_id: UUID,
        *,
        reason: str,
        expected_revision: int,
    ) -> MemoryLifecycleResult:
        return await self._terminal_transition(
            context,
            memory_id,
            target="invalidated",
            reason=reason,
            expected_revision=expected_revision,
        )

    async def expire(
        self,
        context: TenantContext,
        memory_id: UUID,
        *,
        reason: str,
        expected_revision: int,
    ) -> MemoryLifecycleResult:
        return await self._terminal_transition(
            context,
            memory_id,
            target="expired",
            reason=reason,
            expected_revision=expected_revision,
        )

    async def delete(
        self,
        context: TenantContext,
        memory_id: UUID,
        *,
        reason: str,
        expected_revision: int,
    ) -> MemoryLifecycleResult:
        return await self._terminal_transition(
            context,
            memory_id,
            target="deleted",
            reason=reason,
            expected_revision=expected_revision,
        )

    async def _terminal_transition(
        self,
        context: TenantContext,
        memory_id: UUID,
        *,
        target: str,
        reason: str,
        expected_revision: int,
    ) -> MemoryLifecycleResult:
        reason = self._reason(reason)
        now = datetime.now(UTC)
        async with self.database.tenant_transaction(context) as session:
            memory = await self._lock_memory(session, memory_id)
            if memory.status == target:
                return self._result(memory, already_in_state=True)
            require_revision(
                "memory",
                expected=expected_revision,
                actual=memory.revision,
            )
            if target == "invalidated":
                if memory.status != "active":
                    self._invalid_transition(memory, target)
                memory.invalidated_at = now
                event_type = "MemoryInvalidated"
                action = "memory.invalidate"
            elif target == "expired":
                if memory.status != "active":
                    self._invalid_transition(memory, target)
                if memory.expires_at is None or memory.expires_at > now:
                    raise DomainConflict(
                        "MEMORY_NOT_DUE_FOR_EXPIRY",
                        "memory cannot expire before its configured expiry",
                        details={"memory_id": str(memory_id)},
                    )
                event_type = "MemoryExpired"
                action = "memory.expire"
            else:
                if memory.status not in {"candidate", "active", "invalidated", "expired"}:
                    self._invalid_transition(memory, target)
                memory.deleted_at = now
                event_type = "MemoryDeleted"
                action = "memory.delete"
            memory.status = target
            memory.revision += 1
            self._record(
                session,
                context,
                memory,
                event_type=event_type,
                action=action,
                details={"reason": reason},
            )
            await session.flush()
            return self._result(memory)

    @staticmethod
    async def _lock_memory(session: AsyncSession, memory_id: UUID) -> Memory:
        memory = await session.scalar(
            select(Memory).where(Memory.id == memory_id).with_for_update()
        )
        if memory is None:
            raise ResourceNotFound("memory", str(memory_id))
        return memory

    @staticmethod
    def _require_content_hash(memory: Memory) -> None:
        try:
            actual = content_hash(memory.content)
        except (TypeError, ValueError):
            actual = ""
        if actual != memory.content_hash:
            raise DomainConflict(
                "MEMORY_CONTENT_HASH_MISMATCH",
                "memory content does not match its immutable hash",
                details={"memory_id": str(memory.id)},
            )

    @staticmethod
    def _invalid_transition(memory: Memory, target: str) -> None:
        raise DomainConflict(
            "INVALID_STATE_TRANSITION",
            f"memory cannot transition from {memory.status} to {target}",
            details={"entity": "memory", "current": memory.status, "target": target},
        )

    @staticmethod
    def _reason(value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 10_000:
            raise ValueError("memory reason must contain between 1 and 10000 characters")
        return normalized

    @staticmethod
    def _result(memory: Memory, *, already_in_state: bool = False) -> MemoryLifecycleResult:
        return MemoryLifecycleResult(
            id=memory.id,
            memory_key=memory.memory_key,
            version=memory.version,
            memory_type=memory.memory_type,
            scope_type=memory.scope_type,
            status=memory.status,
            content_hash=memory.content_hash,
            revision=memory.revision,
            already_in_state=already_in_state,
        )

    @staticmethod
    def _record(
        session: AsyncSession,
        context: TenantContext,
        memory: Memory,
        *,
        event_type: str,
        action: str,
        details: dict[str, str],
    ) -> None:
        payload = {
            "memory_key": str(memory.memory_key),
            "version": memory.version,
            "content_hash": memory.content_hash,
            "status": memory.status,
            **details,
        }
        session.add_all(
            [
                Event(
                    tenant_id=context.tenant_id,
                    event_type=event_type,
                    aggregate_type="memory",
                    aggregate_id=memory.id,
                    actor_id=context.actor_id,
                    payload=payload,
                    correlation_id=context.correlation_id,
                ),
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action=action,
                    resource_type="memory",
                    resource_id=memory.id,
                    actor_id=context.actor_id,
                    details=payload,
                    correlation_id=context.correlation_id,
                ),
            ]
        )
