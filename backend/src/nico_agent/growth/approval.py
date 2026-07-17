"""Human review requests and immutable Approval decisions for growth subjects."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import AccessDenied, DomainConflict, ResourceNotFound
from nico_agent.domain.models import (
    Approval,
    AuditRecord,
    Evaluation,
    Event,
    Memory,
    SkillVersion,
)
from nico_agent.domain.states import require_revision
from nico_agent.growth.evaluation import SubjectType


class ApprovalReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    subject_type: SubjectType
    subject_id: UUID
    content_hash: str
    action: str
    status: str
    requester: str
    reviewer: str | None
    reason: str | None
    expires_at: datetime | None
    decided_at: datetime | None
    revision: int
    already_exists: bool = False


class GrowthApprovalService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def request(
        self,
        context: TenantContext,
        subject_type: SubjectType,
        subject_id: UUID,
        *,
        expected_revision: int,
        expires_in_seconds: int = 86_400,
    ) -> ApprovalReference:
        if not 300 <= expires_in_seconds <= 2_592_000:
            raise ValueError("approval expiry must be between 5 minutes and 30 days")
        now = datetime.now(UTC)
        async with self.database.tenant_transaction(context) as session:
            subject = await self._lock_subject(session, subject_type, subject_id)
            require_revision(
                subject_type,
                expected=expected_revision,
                actual=subject.revision,
            )
            self._require_reviewable(subject_type, subject.status, subject_id)
            evaluation = await self.latest_evaluation(
                session, subject_type, subject_id, subject.content_hash
            )
            if evaluation is None or not (
                evaluation.status == "completed" and evaluation.verdict == "pass"
            ):
                raise DomainConflict(
                    "GROWTH_EVALUATION_NOT_PASSED",
                    "latest evaluation must pass before review can be requested",
                    details={"subject_id": str(subject_id)},
                )

            requested = await session.scalar(
                select(Approval)
                .where(
                    Approval.subject_type == subject_type,
                    self._subject_predicate(subject_type, subject_id),
                    Approval.content_hash == subject.content_hash,
                    Approval.action == "publish",
                    Approval.status == "requested",
                )
                .order_by(Approval.created_at.desc(), Approval.id.desc())
                .with_for_update()
            )
            if requested is not None and (
                requested.expires_at is None or requested.expires_at > now
            ):
                return self._reference(requested, already_exists=True)
            if requested is not None:
                self._expire(session, context, requested, subject_id, now)
                await session.flush()

            approved = await self.valid_approval(
                session,
                subject_type,
                subject_id,
                subject.content_hash,
                now=now,
            )
            if approved is not None:
                return self._reference(approved, already_exists=True)

            approval = Approval(
                tenant_id=context.tenant_id,
                subject_type=subject_type,
                memory_id=subject_id if subject_type == "memory" else None,
                skill_version_id=subject_id if subject_type == "skill_version" else None,
                action="publish",
                content_hash=subject.content_hash,
                status="requested",
                requester=context.actor_id,
                expires_at=now + timedelta(seconds=expires_in_seconds),
            )
            session.add(approval)
            await session.flush()
            self._record(
                session,
                context,
                approval,
                subject_id,
                event_type="ReviewRequested",
                action="growth.review.request",
            )
            return self._reference(approval)

    async def decide(
        self,
        context: TenantContext,
        approval_id: UUID,
        *,
        decision: Literal["approved", "rejected"],
        reason: str,
        expected_revision: int,
    ) -> ApprovalReference:
        reason = self._reason(reason)
        now = datetime.now(UTC)
        async with self.database.tenant_transaction(context) as session:
            approval = await session.scalar(
                select(Approval).where(Approval.id == approval_id).with_for_update()
            )
            if approval is None:
                raise ResourceNotFound("approval", str(approval_id))
            subject_id = self._subject_id(approval)
            if approval.status == decision:
                return self._reference(approval, already_exists=True)
            require_revision(
                "approval",
                expected=expected_revision,
                actual=approval.revision,
            )
            if approval.status != "requested":
                raise DomainConflict(
                    "APPROVAL_ALREADY_DECIDED",
                    "approval is already terminal",
                    details={"approval_id": str(approval_id), "status": approval.status},
                )
            if approval.expires_at is not None and approval.expires_at <= now:
                self._expire(session, context, approval, subject_id, now)
                await session.flush()
                return self._reference(approval)
            if approval.requester == context.actor_id:
                raise AccessDenied(
                    "APPROVAL_SELF_REVIEW_FORBIDDEN",
                    "the approval requester cannot review the same growth subject",
                )
            approval.status = decision
            approval.reviewer = context.actor_id
            approval.reason = reason
            approval.decided_at = now
            approval.revision += 1
            self._record(
                session,
                context,
                approval,
                subject_id,
                event_type="ReviewApproved" if decision == "approved" else "ReviewRejected",
                action="growth.review.decide",
            )
            await session.flush()
            return self._reference(approval)

    async def cancel(
        self,
        context: TenantContext,
        approval_id: UUID,
        *,
        reason: str,
        expected_revision: int,
    ) -> ApprovalReference:
        reason = self._reason(reason)
        async with self.database.tenant_transaction(context) as session:
            approval = await session.scalar(
                select(Approval).where(Approval.id == approval_id).with_for_update()
            )
            if approval is None:
                raise ResourceNotFound("approval", str(approval_id))
            if approval.status == "cancelled":
                return self._reference(approval, already_exists=True)
            require_revision(
                "approval",
                expected=expected_revision,
                actual=approval.revision,
            )
            if approval.status != "requested":
                raise DomainConflict(
                    "APPROVAL_ALREADY_DECIDED",
                    "approval is already terminal",
                    details={"approval_id": str(approval_id), "status": approval.status},
                )
            if approval.requester != context.actor_id:
                raise AccessDenied(
                    "APPROVAL_CANCEL_FORBIDDEN",
                    "only the requester can cancel a growth approval",
                )
            approval.status = "cancelled"
            approval.reason = reason
            approval.decided_at = datetime.now(UTC)
            approval.revision += 1
            subject_id = self._subject_id(approval)
            self._record(
                session,
                context,
                approval,
                subject_id,
                event_type="ReviewCancelled",
                action="growth.review.cancel",
            )
            await session.flush()
            return self._reference(approval)

    @staticmethod
    async def latest_evaluation(
        session: AsyncSession,
        subject_type: SubjectType,
        subject_id: UUID,
        content_hash: str,
    ) -> Evaluation | None:
        return await session.scalar(
            select(Evaluation)
            .where(
                Evaluation.subject_type == subject_type,
                GrowthApprovalService._subject_predicate(subject_type, subject_id, Evaluation),
                Evaluation.content_hash == content_hash,
                Evaluation.status.in_(("completed", "failed")),
            )
            .order_by(Evaluation.created_at.desc(), Evaluation.id.desc())
        )

    @staticmethod
    async def valid_approval(
        session: AsyncSession,
        subject_type: SubjectType,
        subject_id: UUID,
        content_hash: str,
        *,
        now: datetime,
    ) -> Approval | None:
        return await session.scalar(
            select(Approval)
            .where(
                Approval.subject_type == subject_type,
                GrowthApprovalService._subject_predicate(subject_type, subject_id),
                Approval.content_hash == content_hash,
                Approval.action == "publish",
                Approval.status == "approved",
                or_(Approval.expires_at.is_(None), Approval.expires_at > now),
            )
            .order_by(Approval.decided_at.desc(), Approval.id.desc())
        )

    @staticmethod
    async def _lock_subject(
        session: AsyncSession, subject_type: SubjectType, subject_id: UUID
    ) -> Memory | SkillVersion:
        model = Memory if subject_type == "memory" else SkillVersion
        subject = await session.scalar(
            select(model).where(model.id == subject_id).with_for_update()
        )
        if subject is None:
            raise ResourceNotFound(subject_type, str(subject_id))
        return subject

    @staticmethod
    def _require_reviewable(subject_type: SubjectType, status: str, subject_id: UUID) -> None:
        valid = (
            status == "candidate" if subject_type == "memory" else status in {"draft", "testing"}
        )
        if not valid:
            raise DomainConflict(
                "GROWTH_SUBJECT_NOT_REVIEWABLE",
                "growth subject is not in a reviewable candidate state",
                details={"subject_id": str(subject_id), "status": status},
            )

    @staticmethod
    def _subject_predicate(subject_type: SubjectType, subject_id: UUID, model=Approval):
        return (
            model.memory_id == subject_id
            if subject_type == "memory"
            else model.skill_version_id == subject_id
        )

    @staticmethod
    def _subject_id(approval: Approval) -> UUID:
        subject_id = approval.memory_id or approval.skill_version_id
        assert subject_id is not None
        return subject_id

    def _expire(
        self,
        session: AsyncSession,
        context: TenantContext,
        approval: Approval,
        subject_id: UUID,
        now: datetime,
    ) -> None:
        approval.status = "expired"
        approval.reason = "Approval request expired before decision."
        approval.decided_at = now
        approval.revision += 1
        self._record(
            session,
            context,
            approval,
            subject_id,
            event_type="ReviewExpired",
            action="growth.review.expire",
        )

    @staticmethod
    def _reason(value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 10_000:
            raise ValueError("review reason must contain between 1 and 10000 characters")
        return normalized

    @staticmethod
    def _reference(approval: Approval, *, already_exists: bool = False) -> ApprovalReference:
        return ApprovalReference(
            id=approval.id,
            subject_type=approval.subject_type,
            subject_id=GrowthApprovalService._subject_id(approval),
            content_hash=approval.content_hash,
            action=approval.action,
            status=approval.status,
            requester=approval.requester,
            reviewer=approval.reviewer,
            reason=approval.reason,
            expires_at=approval.expires_at,
            decided_at=approval.decided_at,
            revision=approval.revision,
            already_exists=already_exists,
        )

    @staticmethod
    def _record(
        session: AsyncSession,
        context: TenantContext,
        approval: Approval,
        subject_id: UUID,
        *,
        event_type: str,
        action: str,
    ) -> None:
        payload = {
            "approval_id": str(approval.id),
            "content_hash": approval.content_hash,
            "action": approval.action,
            "status": approval.status,
            "requester": approval.requester,
            "reviewer": approval.reviewer,
        }
        session.add_all(
            [
                Event(
                    tenant_id=context.tenant_id,
                    event_type=event_type,
                    aggregate_type=approval.subject_type,
                    aggregate_id=subject_id,
                    actor_id=context.actor_id,
                    payload=payload,
                    correlation_id=context.correlation_id,
                ),
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action=action,
                    resource_type=approval.subject_type,
                    resource_id=subject_id,
                    actor_id=context.actor_id,
                    details=payload,
                    correlation_id=context.correlation_id,
                ),
            ]
        )
