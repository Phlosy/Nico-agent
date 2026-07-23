"""Tenant-safe decisions and wake-up semantics for sensitive ToolCalls."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainConflict, ResourceNotFound
from nico_agent.domain.models import (
    AuditRecord,
    Event,
    Run,
    RunStep,
    RuntimeSession,
    ToolApprovalRequest,
    ToolCall,
)
from nico_agent.domain.states import (
    RunStatus,
    RunStepStatus,
    ToolApprovalScope,
    ToolApprovalStatus,
    ToolCallStatus,
    require_revision,
)
from nico_agent.runtime.lifecycle import RunLifecycleAuthority
from nico_agent.tool_approvals.contracts import ToolApprovalDecision, ToolApprovalRead

_TERMINAL_RUNS = {
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
    RunStatus.TIMED_OUT.value,
}


class ToolApprovalService:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    async def create_policy_approval(
        session: AsyncSession,
        context: TenantContext,
        *,
        call: ToolCall,
        risk_level: str,
        requester: str,
        arguments_redacted: dict,
        arguments_hash: str,
        ttl_seconds: int,
        policy: dict,
    ) -> ToolApprovalRequest:
        """Persist an auditable, one-call approval granted by frozen policy."""

        now = datetime.now(UTC)
        actor = "policy:conversation"
        approval = ToolApprovalRequest(
            tenant_id=context.tenant_id,
            run_id=call.run_id,
            run_step_id=call.run_step_id,
            tool_call_id=call.id,
            tool_definition_id=call.tool_definition_id,
            risk_level=risk_level,
            requester=requester,
            arguments_redacted=arguments_redacted,
            arguments_hash=arguments_hash,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        session.add(approval)
        await session.flush()
        approval.status = ToolApprovalStatus.APPROVED.value
        approval.allowed_scope = ToolApprovalScope.ONCE.value
        approval.decided_by = actor
        approval.decision = {
            "status": ToolApprovalStatus.APPROVED.value,
            "scope": ToolApprovalScope.ONCE.value,
            "reason": "approved by frozen Conversation policy",
            "source": policy["source"],
            "conversation_id": policy.get("conversation_id"),
            "approval_mode": policy["mode"],
        }
        approval.decision_idempotency_key = f"policy:{call.id}"
        approval.decided_at = now
        approval.revision += 1
        await session.flush()
        payload = {
            "approval_id": str(approval.id),
            "tool_call_id": str(call.id),
            "tool": f"{call.tool_name}@{call.tool_version}",
            "risk_level": risk_level,
            "status": approval.status,
            "allowed_scope": approval.allowed_scope,
            "decided_by": actor,
            "source": policy["source"],
            "conversation_id": policy.get("conversation_id"),
            "approval_mode": policy["mode"],
        }
        session.add(
            Event(
                tenant_id=context.tenant_id,
                event_type="ToolApprovalPolicyApproved",
                aggregate_type="tool_approval_request",
                aggregate_id=approval.id,
                run_id=call.run_id,
                actor_id=actor,
                payload=payload,
                correlation_id=context.correlation_id,
            )
        )
        session.add(
            AuditRecord(
                tenant_id=context.tenant_id,
                action="tool.approval.policy_approved",
                resource_type="tool_approval_request",
                resource_id=approval.id,
                actor_id=actor,
                details=payload,
                correlation_id=context.correlation_id,
            )
        )
        return approval

    async def get(self, context: TenantContext, approval_id: UUID) -> ToolApprovalRead:
        async with self.database.tenant_transaction(context) as session:
            approval = await self._approval(session, context, approval_id, lock=True)
            call = await self._call(session, context, approval.tool_call_id, lock=True)
            await self._expire_if_due(session, context, approval, call)
            await session.flush()
            return self._view(approval, call)

    async def list(
        self,
        context: TenantContext,
        *,
        run_id: UUID | None = None,
        status: ToolApprovalStatus | None = None,
        limit: int = 100,
    ) -> list[ToolApprovalRead]:
        async with self.database.tenant_transaction(context) as session:
            statement = select(ToolApprovalRequest).where(
                ToolApprovalRequest.tenant_id == context.tenant_id
            )
            if run_id is not None:
                statement = statement.where(ToolApprovalRequest.run_id == run_id)
            if status is not None:
                if status is ToolApprovalStatus.EXPIRED:
                    statement = statement.where(
                        or_(
                            ToolApprovalRequest.status == status.value,
                            (
                                (ToolApprovalRequest.status == ToolApprovalStatus.REQUESTED.value)
                                & (ToolApprovalRequest.expires_at <= datetime.now(UTC))
                            ),
                        )
                    )
                else:
                    statement = statement.where(ToolApprovalRequest.status == status.value)
            approvals = list(
                await session.scalars(
                    statement.order_by(
                        ToolApprovalRequest.created_at.desc(), ToolApprovalRequest.id.desc()
                    )
                    .limit(limit)
                    .with_for_update()
                )
            )
            result: list[ToolApprovalRead] = []
            for approval in approvals:
                call = await self._call(session, context, approval.tool_call_id, lock=True)
                await self._expire_if_due(session, context, approval, call)
                if status is None or approval.status == status.value:
                    result.append(self._view(approval, call))
            await session.flush()
            return result

    async def decide(
        self,
        context: TenantContext,
        approval_id: UUID,
        command: ToolApprovalDecision,
        *,
        idempotency_key: str,
    ) -> ToolApprovalRead:
        async with self.database.tenant_transaction(context) as session:
            approval = await self._approval(session, context, approval_id, lock=True)
            call = await self._call(session, context, approval.tool_call_id, lock=True)
            target = (
                ToolApprovalStatus.APPROVED
                if command.decision == "approve"
                else ToolApprovalStatus.REJECTED
            )
            scope = (
                ToolApprovalScope(command.allowed_scope)
                if command.allowed_scope is not None
                else ToolApprovalScope.NONE
            )
            current = ToolApprovalStatus(approval.status)
            if current is not ToolApprovalStatus.REQUESTED:
                if (
                    current is target
                    and approval.allowed_scope == scope.value
                    and approval.decision_idempotency_key == idempotency_key
                ):
                    return self._view(approval, call)
                raise DomainConflict(
                    "TOOL_APPROVAL_ALREADY_DECIDED",
                    "tool approval already has a different terminal decision",
                    details={"status": approval.status, "allowed_scope": approval.allowed_scope},
                )

            if approval.expires_at <= datetime.now(UTC):
                await self._finish(
                    session,
                    context,
                    approval,
                    call,
                    status=ToolApprovalStatus.EXPIRED,
                    scope=ToolApprovalScope.NONE,
                    actor="system:approval-expiry",
                    idempotency_key=f"expiry:{approval.id}",
                    reason="approval request expired before a decision",
                )
                await session.flush()
                return self._view(approval, call)

            require_revision(
                "tool_approval_request",
                expected=command.expected_revision,
                actual=approval.revision,
            )
            run = await self._run(session, context, approval.run_id, lock=True)
            if run.status in _TERMINAL_RUNS:
                await self._finish(
                    session,
                    context,
                    approval,
                    call,
                    status=ToolApprovalStatus.CANCELLED,
                    scope=ToolApprovalScope.NONE,
                    actor=context.actor_id,
                    idempotency_key=idempotency_key,
                    reason="Run became terminal before the decision",
                )
                await session.flush()
                return self._view(approval, call)

            await self._finish(
                session,
                context,
                approval,
                call,
                status=target,
                scope=scope,
                actor=context.actor_id,
                idempotency_key=idempotency_key,
                reason=command.reason,
                run=run,
            )
            await session.flush()
            return self._view(approval, call)

    async def _expire_if_due(
        self,
        session: AsyncSession,
        context: TenantContext,
        approval: ToolApprovalRequest,
        call: ToolCall,
    ) -> None:
        if (
            approval.status == ToolApprovalStatus.REQUESTED.value
            and approval.expires_at <= datetime.now(UTC)
        ):
            await self._finish(
                session,
                context,
                approval,
                call,
                status=ToolApprovalStatus.EXPIRED,
                scope=ToolApprovalScope.NONE,
                actor="system:approval-expiry",
                idempotency_key=f"expiry:{approval.id}",
                reason="approval request expired before a decision",
            )

    async def _finish(
        self,
        session: AsyncSession,
        context: TenantContext,
        approval: ToolApprovalRequest,
        call: ToolCall,
        *,
        status: ToolApprovalStatus,
        scope: ToolApprovalScope,
        actor: str,
        idempotency_key: str,
        reason: str | None,
        run: Run | None = None,
    ) -> None:
        if run is None:
            run = await self._run(session, context, approval.run_id, lock=True)
        step = await session.scalar(
            select(RunStep)
            .where(
                RunStep.tenant_id == context.tenant_id,
                RunStep.id == approval.run_step_id,
            )
            .with_for_update()
        )
        if step is None:
            raise ResourceNotFound("run_step", str(approval.run_step_id))
        runtime_session = await session.scalar(
            select(RuntimeSession)
            .where(
                RuntimeSession.tenant_id == context.tenant_id,
                RuntimeSession.run_id == approval.run_id,
            )
            .with_for_update()
        )
        now = datetime.now(UTC)
        approval.status = status.value
        approval.allowed_scope = scope.value
        approval.decided_by = actor
        approval.decision = {"status": status.value, "scope": scope.value, "reason": reason}
        approval.decision_idempotency_key = idempotency_key
        approval.decided_at = now
        approval.revision += 1

        if status is not ToolApprovalStatus.APPROVED and call.status in {
            ToolCallStatus.PENDING.value,
            ToolCallStatus.RUNNING.value,
        }:
            error_code = {
                ToolApprovalStatus.REJECTED: "TOOL_APPROVAL_REJECTED",
                ToolApprovalStatus.EXPIRED: "TOOL_APPROVAL_EXPIRED",
                ToolApprovalStatus.CANCELLED: "TOOL_APPROVAL_CANCELLED",
            }[status]
            call.status = (
                ToolCallStatus.CANCELLED.value
                if status is ToolApprovalStatus.CANCELLED
                else ToolCallStatus.FAILED.value
            )
            call.error = {
                "code": error_code,
                "message": reason or "tool execution was not approved",
            }
            call.ended_at = now
            call.revision += 1
            if RunStepStatus(step.status) in {
                RunStepStatus.PENDING,
                RunStepStatus.RUNNING,
                RunStepStatus.WAITING,
            }:
                step.status = (
                    RunStepStatus.CANCELLED.value
                    if status is ToolApprovalStatus.CANCELLED
                    else RunStepStatus.FAILED.value
                )
                step.error = call.error
                step.ended_at = now
                step.revision += 1

        if run.status == RunStatus.WAITING_FOR_APPROVAL.value:
            lifecycle_context = TenantContext(
                context.tenant_id,
                actor,
                context.correlation_id,
            )
            await RunLifecycleAuthority.transition(
                session,
                lifecycle_context,
                run,
                target=RunStatus.RUNNING,
                reason="tool_approval_decided",
                metadata={
                    "approval_id": str(approval.id),
                    "tool_call_id": str(call.id),
                    "approval_status": status.value,
                    "allowed_scope": scope.value,
                },
                event_type="RunWoken",
                action="tool.approval.lifecycle.wake",
            )
        if runtime_session is not None:
            runtime_session.provider_state = {
                **runtime_session.provider_state,
                "tool_approval_decision": {
                    "approval_id": str(approval.id),
                    "status": status.value,
                    "scope": scope.value,
                },
            }
            runtime_session.revision += 1

        event_type = {
            ToolApprovalStatus.APPROVED: "ToolApprovalApproved",
            ToolApprovalStatus.REJECTED: "ToolApprovalRejected",
            ToolApprovalStatus.EXPIRED: "ToolApprovalExpired",
            ToolApprovalStatus.CANCELLED: "ToolApprovalCancelled",
        }[status]
        payload = {
            "approval_id": str(approval.id),
            "tool_call_id": str(call.id),
            "tool": f"{call.tool_name}@{call.tool_version}",
            "risk_level": approval.risk_level,
            "status": status.value,
            "allowed_scope": scope.value,
            "decided_by": actor,
            "revision": approval.revision,
        }
        session.add(
            Event(
                tenant_id=context.tenant_id,
                event_type=event_type,
                aggregate_type="tool_approval_request",
                aggregate_id=approval.id,
                run_id=approval.run_id,
                actor_id=actor,
                payload=payload,
                correlation_id=context.correlation_id,
            )
        )
        session.add(
            AuditRecord(
                tenant_id=context.tenant_id,
                action=f"tool.approval.{status.value}",
                resource_type="tool_approval_request",
                resource_id=approval.id,
                actor_id=actor,
                details=payload,
                correlation_id=context.correlation_id,
            )
        )

    @staticmethod
    def _view(approval: ToolApprovalRequest, call: ToolCall) -> ToolApprovalRead:
        return ToolApprovalRead(
            id=approval.id,
            run_id=approval.run_id,
            run_step_id=approval.run_step_id,
            tool_call_id=approval.tool_call_id,
            tool_definition_id=approval.tool_definition_id,
            tool_name=call.tool_name,
            tool_version=call.tool_version,
            risk_level=approval.risk_level,
            status=approval.status,
            allowed_scope=approval.allowed_scope,
            requester=approval.requester,
            arguments=approval.arguments_redacted,
            arguments_hash=approval.arguments_hash,
            decided_by=approval.decided_by,
            decision=approval.decision,
            expires_at=approval.expires_at,
            decided_at=approval.decided_at,
            revision=approval.revision,
            created_at=approval.created_at,
            updated_at=approval.updated_at,
        )

    @staticmethod
    async def _approval(
        session: AsyncSession,
        context: TenantContext,
        approval_id: UUID,
        *,
        lock: bool,
    ) -> ToolApprovalRequest:
        statement = select(ToolApprovalRequest).where(
            ToolApprovalRequest.tenant_id == context.tenant_id,
            ToolApprovalRequest.id == approval_id,
        )
        if lock:
            statement = statement.with_for_update()
        value = await session.scalar(statement)
        if value is None:
            raise ResourceNotFound("tool_approval_request", str(approval_id))
        return value

    @staticmethod
    async def _call(
        session: AsyncSession,
        context: TenantContext,
        tool_call_id: UUID,
        *,
        lock: bool,
    ) -> ToolCall:
        statement = select(ToolCall).where(
            ToolCall.tenant_id == context.tenant_id,
            ToolCall.id == tool_call_id,
        )
        if lock:
            statement = statement.with_for_update()
        value = await session.scalar(statement)
        if value is None:
            raise ResourceNotFound("tool_call", str(tool_call_id))
        return value

    @staticmethod
    async def _run(
        session: AsyncSession,
        context: TenantContext,
        run_id: UUID,
        *,
        lock: bool,
    ) -> Run:
        statement = select(Run).where(
            Run.tenant_id == context.tenant_id,
            Run.id == run_id,
        )
        if lock:
            statement = statement.with_for_update()
        value = await session.scalar(statement)
        if value is None:
            raise ResourceNotFound("run", str(run_id))
        return value
