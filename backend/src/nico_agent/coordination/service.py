"""Transactional dynamic delegation service with bounded budgets and closure checks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.coordination.contracts import (
    AgentMessageStatus,
    AgentMessageType,
    AgentMessageVisibility,
    DelegationIntent,
    DelegationResult,
    DelegationStatus,
)
from nico_agent.coordination.policy import delegation_fingerprint, narrow_child_permissions
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainConflict, ResourceNotFound
from nico_agent.domain.models import (
    Agent,
    AgentMessage,
    AgentRunRelation,
    AgentVersion,
    AuditRecord,
    Delegation,
    Event,
    Project,
    ProjectMember,
    ProjectSession,
    Run,
    RunBudgetLedger,
    RunStep,
    RuntimeSession,
    Task,
    ToolApprovalRequest,
    ToolCall,
)
from nico_agent.domain.states import (
    RunStatus,
    RunStepStatus,
    TaskStatus,
    ToolApprovalScope,
    ToolApprovalStatus,
    ToolCallStatus,
    require_revision,
)
from nico_agent.runtime.preparation import narrow_knowledge_policy

_DELEGATABLE_RUN_STATUSES = {
    RunStatus.PLANNING,
    RunStatus.RUNNING,
    RunStatus.WAITING_FOR_TOOL,
}
_ACTIVE_DELEGATION_STATUSES = {
    DelegationStatus.PROPOSED.value,
    DelegationStatus.ACCEPTED.value,
    DelegationStatus.RUNNING.value,
}


class CoordinationService:
    """Owns coordination ORM writes; runtime providers receive only a narrow handler."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def delegate(
        self,
        context: TenantContext,
        parent_run_id: UUID,
        intent: DelegationIntent,
        *,
        worker_id: str,
        lease_token: UUID,
        parent_run_step_id: UUID | None = None,
    ) -> DelegationResult:
        async with self.database.tenant_transaction(context) as session:
            parent = await session.scalar(
                select(Run)
                .where(Run.tenant_id == context.tenant_id, Run.id == parent_run_id)
                .with_for_update()
            )
            if parent is None:
                raise ResourceNotFound("run", str(parent_run_id))
            if parent.lease_owner != worker_id or parent.lease_token != lease_token:
                raise DomainConflict(
                    "RUN_LEASE_LOST", "only the current Run lease owner may create a delegation"
                )
            if RunStatus(parent.status) not in _DELEGATABLE_RUN_STATUSES:
                raise DomainConflict(
                    "RUN_NOT_DELEGATABLE", "the parent Run is not in a delegatable state"
                )

            existing = await session.scalar(
                select(Delegation).where(
                    Delegation.tenant_id == context.tenant_id,
                    Delegation.parent_run_id == parent.id,
                    Delegation.idempotency_key == intent.idempotency_key,
                )
            )
            if existing is not None:
                return self._result(existing, replay=True)

            runtime_session = await session.scalar(
                select(RuntimeSession).where(
                    RuntimeSession.tenant_id == context.tenant_id,
                    RuntimeSession.run_id == parent.id,
                )
            )
            if runtime_session is None:
                raise DomainConflict(
                    "COORDINATION_SESSION_REQUIRED",
                    "the parent needs an initialized RuntimeSession before delegating",
                )
            policy = runtime_session.coordination_policy_snapshot
            self._require_enabled_policy(policy, intent)
            fingerprint = delegation_fingerprint(intent)
            duplicate = await session.scalar(
                select(Delegation.id).where(
                    Delegation.tenant_id == context.tenant_id,
                    Delegation.parent_run_id == parent.id,
                    Delegation.task_fingerprint == fingerprint,
                )
            )
            if duplicate is not None:
                raise DomainConflict(
                    "DUPLICATE_DELEGATION",
                    "an equivalent delegation already exists under this parent Run",
                )

            target = await session.scalar(
                select(AgentVersion).where(
                    AgentVersion.tenant_id == context.tenant_id,
                    AgentVersion.id == intent.target_agent_version_id,
                    AgentVersion.status == "published",
                )
            )
            if target is None:
                raise ResourceNotFound("agent_version", str(intent.target_agent_version_id))
            target_agent = await session.scalar(
                select(Agent).where(
                    Agent.tenant_id == context.tenant_id,
                    Agent.id == target.agent_id,
                    Agent.current_version_id == target.id,
                    Agent.status == "ready",
                )
            )
            if target_agent is None:
                raise DomainConflict(
                    "TARGET_AGENT_NOT_READY",
                    "delegation target must be a ready published AgentVersion",
                )
            await self._validate_tree(session, context, parent, target, policy)

            parent_task = await session.scalar(
                select(Task).where(Task.tenant_id == context.tenant_id, Task.id == parent.task_id)
            )
            if parent_task is None:
                raise ResourceNotFound("task", str(parent.task_id))
            target_project_session_id = await self._project_member_session_id(
                session,
                context,
                parent,
                parent_task,
                target,
                policy,
            )
            ledger = await self._locked_ledger(session, parent)
            self._reserve(ledger, intent, policy)
            permission_snapshot = self._permission_snapshot(runtime_session, target, intent, policy)

            delegation_id, child_task_id, child_run_id = uuid4(), uuid4(), uuid4()
            now = datetime.now(UTC)
            child_task = Task(
                id=child_task_id,
                tenant_id=context.tenant_id,
                project_id=parent_task.project_id,
                project_session_id=target_project_session_id,
                assignee_agent_id=target.agent_id,
                parent_task_id=parent_task.id,
                title=intent.objective[:300],
                input={
                    "objective": intent.objective,
                    "context_refs": list(intent.context_refs),
                    "delegation_id": str(delegation_id),
                },
                acceptance=intent.acceptance,
                status="running",
                priority=parent_task.priority,
            )
            child_run = Run(
                id=child_run_id,
                tenant_id=context.tenant_id,
                task_id=child_task_id,
                agent_id=target.agent_id,
                agent_version_id=target.id,
                attempt=1,
                max_steps=min(parent.max_steps, _positive_int(target.budgets.get("max_steps"), 64)),
                token_budget=intent.budget.token_limit,
                timeout_seconds=intent.budget.wall_time_seconds,
                budgets={
                    "cost_limit_microunits": intent.budget.cost_limit_microunits,
                    "tool_call_limit": intent.budget.tool_call_limit,
                },
            )
            delegation = Delegation(
                id=delegation_id,
                tenant_id=context.tenant_id,
                parent_run_id=parent.id,
                parent_run_step_id=parent_run_step_id,
                child_task_id=child_task_id,
                child_run_id=child_run_id,
                target_agent_id=target.agent_id,
                target_agent_version_id=target.id,
                objective=intent.objective,
                acceptance=intent.acceptance,
                context_refs=list(intent.context_refs),
                execution_mode=intent.execution_mode,
                budget_grant=intent.budget.model_dump(mode="json"),
                policy_snapshot=policy,
                permission_snapshot=permission_snapshot,
                idempotency_key=intent.idempotency_key,
                task_fingerprint=fingerprint,
                status=DelegationStatus.ACCEPTED.value,
            )
            child_ledger = RunBudgetLedger(
                tenant_id=context.tenant_id,
                run_id=child_run_id,
                token_limit=intent.budget.token_limit,
                cost_limit_microunits=intent.budget.cost_limit_microunits,
                tool_call_limit=intent.budget.tool_call_limit,
                wall_deadline=(
                    now + timedelta(seconds=intent.budget.wall_time_seconds)
                    if intent.budget.wall_time_seconds
                    else None
                ),
            )
            # Explicit flush boundaries make the cross-tenant FK order visible; the
            # ORM models intentionally have no persistence relationships to leak to providers.
            session.add(child_task)
            await session.flush()
            session.add(child_run)
            await session.flush()
            session.add(delegation)
            await session.flush()
            session.add(child_ledger)
            await session.flush()
            await self._insert_closure(session, context, parent.id, child_run_id, delegation_id)
            session.add(
                AgentMessage(
                    tenant_id=context.tenant_id,
                    delegation_id=delegation_id,
                    sender_run_id=parent.id,
                    receiver_run_id=child_run_id,
                    message_type=AgentMessageType.TASK_ASSIGNMENT.value,
                    content={
                        "objective": intent.objective,
                        "acceptance": intent.acceptance,
                        "budget": intent.budget.model_dump(mode="json"),
                    },
                    refs=list(intent.context_refs),
                    visibility=AgentMessageVisibility.SENDER_RECEIVER.value,
                    status=AgentMessageStatus.QUEUED.value,
                    idempotency_key=f"assignment:{intent.idempotency_key}",
                    correlation_id=context.correlation_id,
                )
            )
            self._record(session, context, delegation)
            await session.flush()
            return self._result(delegation)

    async def list_delegations(
        self, context: TenantContext, parent_run_id: UUID
    ) -> list[Delegation]:
        async with self.database.tenant_transaction(context) as session:
            await self._required_run(session, context, parent_run_id)
            return list(
                await session.scalars(
                    select(Delegation)
                    .where(
                        Delegation.tenant_id == context.tenant_id,
                        Delegation.parent_run_id == parent_run_id,
                    )
                    .order_by(Delegation.created_at, Delegation.id)
                )
            )

    async def list_children(self, context: TenantContext, parent_run_id: UUID) -> list[Run]:
        async with self.database.tenant_transaction(context) as session:
            await self._required_run(session, context, parent_run_id)
            return list(
                await session.scalars(
                    select(Run)
                    .join(
                        AgentRunRelation,
                        (AgentRunRelation.tenant_id == Run.tenant_id)
                        & (AgentRunRelation.descendant_run_id == Run.id),
                    )
                    .where(
                        AgentRunRelation.tenant_id == context.tenant_id,
                        AgentRunRelation.ancestor_run_id == parent_run_id,
                        AgentRunRelation.is_direct.is_(True),
                    )
                    .order_by(Run.created_at, Run.id)
                )
            )

    async def list_messages(self, context: TenantContext, run_id: UUID) -> list[AgentMessage]:
        async with self.database.tenant_transaction(context) as session:
            await self._required_run(session, context, run_id)
            return list(
                await session.scalars(
                    select(AgentMessage)
                    .where(
                        AgentMessage.tenant_id == context.tenant_id,
                        (
                            (AgentMessage.sender_run_id == run_id)
                            | (AgentMessage.receiver_run_id == run_id)
                        ),
                    )
                    .order_by(AgentMessage.created_at, AgentMessage.id)
                )
            )

    async def request_retry(
        self,
        context: TenantContext,
        delegation_id: UUID,
        *,
        reason: str,
        idempotency_key: str,
    ) -> AgentMessage:
        async with self.database.tenant_transaction(context) as session:
            delegation = await session.scalar(
                select(Delegation).where(
                    Delegation.tenant_id == context.tenant_id,
                    Delegation.id == delegation_id,
                )
            )
            if delegation is None:
                raise ResourceNotFound("delegation", str(delegation_id))
            existing = await session.scalar(
                select(AgentMessage).where(
                    AgentMessage.tenant_id == context.tenant_id,
                    AgentMessage.sender_run_id == delegation.child_run_id,
                    AgentMessage.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                return existing
            message = AgentMessage(
                tenant_id=context.tenant_id,
                delegation_id=delegation.id,
                sender_run_id=delegation.child_run_id,
                receiver_run_id=delegation.parent_run_id,
                message_type=AgentMessageType.RETRY_REQUEST.value,
                content={"reason": reason},
                refs=[],
                visibility=AgentMessageVisibility.PARENT_ONLY.value,
                status=AgentMessageStatus.QUEUED.value,
                idempotency_key=idempotency_key,
                correlation_id=context.correlation_id,
            )
            session.add(message)
            await session.flush()
            return message

    async def cancel_tree(
        self,
        context: TenantContext,
        root_run_id: UUID,
        *,
        expected_revision: int,
    ) -> Run:
        async with self.database.tenant_transaction(context) as session:
            root = await self._required_run(session, context, root_run_id, for_update=True)
            if RunStatus(root.status) is RunStatus.CANCELLED:
                return root
            require_revision("run", expected=expected_revision, actual=root.revision)
            relations = list(
                await session.scalars(
                    select(AgentRunRelation)
                    .where(
                        AgentRunRelation.tenant_id == context.tenant_id,
                        AgentRunRelation.ancestor_run_id == root.id,
                    )
                    .order_by(AgentRunRelation.depth, AgentRunRelation.descendant_run_id)
                )
            )
            ordered_ids = [root.id]
            ordered_ids.extend(
                relation.descendant_run_id
                for relation in relations
                if relation.descendant_run_id not in ordered_ids
            )
            runs: list[Run] = [root]
            for run_id in ordered_ids[1:]:
                run = await session.scalar(
                    select(Run)
                    .where(Run.tenant_id == context.tenant_id, Run.id == run_id)
                    .with_for_update()
                )
                if run is not None:
                    runs.append(run)
            now = datetime.now(UTC)
            active_delegations = list(
                await session.scalars(
                    select(Delegation)
                    .where(
                        Delegation.tenant_id == context.tenant_id,
                        Delegation.child_run_id.in_(ordered_ids),
                        Delegation.status.in_(_ACTIVE_DELEGATION_STATUSES),
                    )
                    .with_for_update()
                )
            )
            for delegation in active_delegations:
                await self._release_reserved_budget(session, delegation)
                delegation.status = DelegationStatus.CANCELLED.value
                delegation.error = {
                    "code": "DELEGATION_TREE_CANCELLED",
                    "message": "an ancestor Run cancelled the delegation tree",
                }
                delegation.ended_at = now
                delegation.revision += 1
                existing = await session.scalar(
                    select(AgentMessage.id).where(
                        AgentMessage.tenant_id == context.tenant_id,
                        AgentMessage.sender_run_id == delegation.parent_run_id,
                        AgentMessage.idempotency_key == f"cancel:{root.id}:{delegation.id}",
                    )
                )
                if existing is None:
                    session.add(
                        AgentMessage(
                            tenant_id=context.tenant_id,
                            delegation_id=delegation.id,
                            sender_run_id=delegation.parent_run_id,
                            receiver_run_id=delegation.child_run_id,
                            message_type=AgentMessageType.CANCEL.value,
                            content={"root_run_id": str(root.id)},
                            refs=[],
                            visibility=AgentMessageVisibility.DELEGATION_TREE.value,
                            status=AgentMessageStatus.DELIVERED.value,
                            idempotency_key=f"cancel:{root.id}:{delegation.id}",
                            correlation_id=context.correlation_id,
                            delivered_at=now,
                        )
                    )
            terminal = {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.TIMED_OUT,
            }
            for run in runs:
                if RunStatus(run.status) in terminal:
                    continue
                run.status = RunStatus.CANCELLED.value
                run.error = {
                    "code": "RUN_TREE_CANCELLED",
                    "message": "the Run or one of its ancestors was cancelled",
                }
                run.ended_at = now
                run.revision += 1
                run.lease_owner = None
                run.lease_token = None
                run.lease_expires_at = None
                run.heartbeat_at = None
                runtime = await session.scalar(
                    select(RuntimeSession)
                    .where(
                        RuntimeSession.tenant_id == context.tenant_id,
                        RuntimeSession.run_id == run.id,
                    )
                    .with_for_update()
                )
                if runtime is not None and runtime.status not in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    runtime.status = "cancelled"
                    runtime.ended_at = now
                    runtime.revision += 1
                task = await session.scalar(
                    select(Task)
                    .where(Task.tenant_id == context.tenant_id, Task.id == run.task_id)
                    .with_for_update()
                )
                if task is not None and TaskStatus(task.status) not in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                }:
                    task.status = TaskStatus.CANCELLED.value
                    task.revision += 1
            tool_calls = list(
                await session.scalars(
                    select(ToolCall)
                    .where(
                        ToolCall.tenant_id == context.tenant_id,
                        ToolCall.run_id.in_(ordered_ids),
                        ToolCall.status.in_(["pending", "running"]),
                    )
                    .with_for_update()
                )
            )
            for tool_call in tool_calls:
                tool_call.status = ToolCallStatus.CANCELLED.value
                tool_call.error = {
                    "code": "TOOL_CANCELLED",
                    "message": "the authoritative Run tree was cancelled",
                }
                tool_call.ended_at = now
                tool_call.revision += 1
                step = await session.scalar(
                    select(RunStep)
                    .where(
                        RunStep.tenant_id == context.tenant_id,
                        RunStep.id == tool_call.run_step_id,
                    )
                    .with_for_update()
                )
                if step is not None and RunStepStatus(step.status) in {
                    RunStepStatus.PENDING,
                    RunStepStatus.RUNNING,
                    RunStepStatus.WAITING,
                }:
                    step.status = RunStepStatus.CANCELLED.value
                    step.error = tool_call.error
                    step.ended_at = now
                    step.revision += 1
            approvals = list(
                await session.scalars(
                    select(ToolApprovalRequest)
                    .where(
                        ToolApprovalRequest.tenant_id == context.tenant_id,
                        ToolApprovalRequest.run_id.in_(ordered_ids),
                        ToolApprovalRequest.status == ToolApprovalStatus.REQUESTED.value,
                    )
                    .with_for_update()
                )
            )
            for approval in approvals:
                approval.status = ToolApprovalStatus.CANCELLED.value
                approval.allowed_scope = ToolApprovalScope.NONE.value
                approval.decided_by = context.actor_id
                approval.decision = {
                    "status": ToolApprovalStatus.CANCELLED.value,
                    "scope": ToolApprovalScope.NONE.value,
                    "reason": "the authoritative Run tree was cancelled",
                }
                approval.decision_idempotency_key = f"run-cancel:{root.id}:{approval.id}"
                approval.decided_at = now
                approval.revision += 1
                payload = {
                    "approval_id": str(approval.id),
                    "tool_call_id": str(approval.tool_call_id),
                    "risk_level": approval.risk_level,
                    "status": approval.status,
                    "allowed_scope": approval.allowed_scope,
                    "decided_by": context.actor_id,
                    "revision": approval.revision,
                }
                session.add(
                    Event(
                        tenant_id=context.tenant_id,
                        event_type="ToolApprovalCancelled",
                        aggregate_type="tool_approval_request",
                        aggregate_id=approval.id,
                        run_id=approval.run_id,
                        actor_id=context.actor_id,
                        payload=payload,
                        correlation_id=context.correlation_id,
                    )
                )
                session.add(
                    AuditRecord(
                        tenant_id=context.tenant_id,
                        action="tool.approval.cancelled",
                        resource_type="tool_approval_request",
                        resource_id=approval.id,
                        actor_id=context.actor_id,
                        details=payload,
                        correlation_id=context.correlation_id,
                    )
                )
            self._record_tree_cancel(session, context, root, ordered_ids)
            await session.flush()
            return root

    @staticmethod
    async def _project_member_session_id(
        session: AsyncSession,
        context: TenantContext,
        parent: Run,
        parent_task: Task,
        target: AgentVersion,
        policy: dict[str, Any],
    ) -> UUID | None:
        if policy.get("target_scope") != "project_members":
            return None
        project = await session.scalar(
            select(Project).where(
                Project.tenant_id == context.tenant_id,
                Project.id == parent_task.project_id,
            )
        )
        if project is None:
            raise ResourceNotFound("project", str(parent_task.project_id))
        if project.status != "active":
            raise DomainConflict("PROJECT_ARCHIVED", "archived Projects cannot delegate work")
        managed = project.metadata_json.get("_nico_collaboration", {})
        if not isinstance(managed, dict) or managed.get("managed") is not True:
            raise DomainConflict(
                "PROJECT_NOT_MANAGED",
                "project_members coordination requires a managed Project",
            )
        parent_membership = await session.scalar(
            select(ProjectMember.id)
            .join(
                ProjectSession,
                (ProjectSession.tenant_id == ProjectMember.tenant_id)
                & (ProjectSession.project_member_id == ProjectMember.id),
            )
            .where(
                ProjectMember.tenant_id == context.tenant_id,
                ProjectMember.project_id == project.id,
                ProjectMember.agent_id == parent.agent_id,
                ProjectMember.role == "lead",
                ProjectMember.status == "active",
                ProjectSession.id == parent_task.project_session_id,
                ProjectSession.status == "active",
            )
        )
        if parent_membership is None:
            raise DomainConflict(
                "PROJECT_LEAD_REQUIRED",
                "only the active Project Lead Session may delegate to Project members",
            )
        target_session_id = await session.scalar(
            select(ProjectSession.id)
            .join(
                ProjectMember,
                (ProjectMember.tenant_id == ProjectSession.tenant_id)
                & (ProjectMember.id == ProjectSession.project_member_id),
            )
            .where(
                ProjectSession.tenant_id == context.tenant_id,
                ProjectSession.project_id == project.id,
                ProjectSession.agent_id == target.agent_id,
                ProjectSession.status == "active",
                ProjectMember.status == "active",
            )
        )
        if target_session_id is None:
            raise DomainConflict(
                "PROJECT_MEMBER_INACTIVE",
                "delegation target must be an active member of the Project",
            )
        return target_session_id

    @staticmethod
    def _require_enabled_policy(policy: dict[str, Any], intent: DelegationIntent) -> None:
        if policy.get("version") != 1 or policy.get("enabled") is not True:
            raise DomainConflict("COORDINATION_DISABLED", "coordination is disabled by policy")
        allowed = policy.get("allowed_agent_version_ids", [])
        if str(intent.target_agent_version_id) not in allowed:
            raise DomainConflict(
                "DELEGATION_TARGET_DENIED", "the target AgentVersion is not allowed by policy"
            )

    @staticmethod
    async def _required_run(
        session: AsyncSession,
        context: TenantContext,
        run_id: UUID,
        *,
        for_update: bool = False,
    ) -> Run:
        statement = select(Run).where(
            Run.tenant_id == context.tenant_id,
            Run.id == run_id,
        )
        if for_update:
            statement = statement.with_for_update()
        run = await session.scalar(statement)
        if run is None:
            raise ResourceNotFound("run", str(run_id))
        return run

    @staticmethod
    async def _release_reserved_budget(session: AsyncSession, delegation: Delegation) -> None:
        ledger = await session.scalar(
            select(RunBudgetLedger)
            .where(
                RunBudgetLedger.tenant_id == delegation.tenant_id,
                RunBudgetLedger.run_id == delegation.parent_run_id,
            )
            .with_for_update()
        )
        if ledger is None:
            return
        grant = delegation.budget_grant
        ledger.token_child_reserved = max(
            0, ledger.token_child_reserved - int(grant.get("token_limit") or 0)
        )
        ledger.cost_child_reserved_microunits = max(
            0,
            ledger.cost_child_reserved_microunits - int(grant.get("cost_limit_microunits") or 0),
        )
        ledger.tool_calls_child_reserved = max(
            0,
            ledger.tool_calls_child_reserved - int(grant.get("tool_call_limit") or 0),
        )
        ledger.revision += 1

    @staticmethod
    async def _validate_tree(
        session: AsyncSession,
        context: TenantContext,
        parent: Run,
        target: AgentVersion,
        policy: dict[str, Any],
    ) -> None:
        ancestors = list(
            await session.scalars(
                select(AgentRunRelation.ancestor_run_id).where(
                    AgentRunRelation.tenant_id == context.tenant_id,
                    AgentRunRelation.descendant_run_id == parent.id,
                )
            )
        )
        ancestry = [*ancestors, parent.id]
        ancestor_versions = set(
            await session.scalars(
                select(Run.agent_version_id).where(
                    Run.tenant_id == context.tenant_id, Run.id.in_(ancestry)
                )
            )
        )
        if target.id in ancestor_versions:
            raise DomainConflict(
                "DELEGATION_CYCLE", "an AgentVersion cannot delegate back into its ancestry"
            )
        current_depth = (
            await session.scalar(
                select(func.max(AgentRunRelation.depth)).where(
                    AgentRunRelation.tenant_id == context.tenant_id,
                    AgentRunRelation.descendant_run_id == parent.id,
                )
            )
            or 0
        )
        if current_depth + 1 > _positive_int(policy.get("max_depth"), 0):
            raise DomainConflict("DELEGATION_DEPTH_EXCEEDED", "delegation depth limit exceeded")
        child_count = (
            await session.scalar(
                select(func.count())
                .select_from(Delegation)
                .where(
                    Delegation.tenant_id == context.tenant_id,
                    Delegation.parent_run_id == parent.id,
                )
            )
            or 0
        )
        if child_count >= _positive_int(policy.get("max_children"), 0):
            raise DomainConflict("DELEGATION_COUNT_EXCEEDED", "child Run count limit exceeded")
        active_count = (
            await session.scalar(
                select(func.count())
                .select_from(Delegation)
                .where(
                    Delegation.tenant_id == context.tenant_id,
                    Delegation.parent_run_id == parent.id,
                    Delegation.status.in_(_ACTIVE_DELEGATION_STATUSES),
                )
            )
            or 0
        )
        if active_count >= _positive_int(policy.get("max_parallelism"), 0):
            raise DomainConflict(
                "DELEGATION_PARALLELISM_EXCEEDED", "active child Run limit exceeded"
            )

    @staticmethod
    async def _locked_ledger(session: AsyncSession, parent: Run) -> RunBudgetLedger:
        ledger = await session.scalar(
            select(RunBudgetLedger)
            .where(
                RunBudgetLedger.tenant_id == parent.tenant_id,
                RunBudgetLedger.run_id == parent.id,
            )
            .with_for_update()
        )
        if ledger is None:
            ledger = RunBudgetLedger(
                tenant_id=parent.tenant_id,
                run_id=parent.id,
                token_limit=parent.token_budget or 0,
                cost_limit_microunits=_nonnegative_int(
                    parent.budgets.get("cost_limit_microunits"), 0
                ),
                tool_call_limit=_nonnegative_int(parent.budgets.get("tool_call_limit"), 0),
                wall_deadline=(
                    parent.started_at + timedelta(seconds=parent.timeout_seconds)
                    if parent.started_at and parent.timeout_seconds
                    else None
                ),
            )
            session.add(ledger)
            await session.flush()
        return ledger

    @staticmethod
    def _reserve(ledger: RunBudgetLedger, intent: DelegationIntent, policy: dict[str, Any]) -> None:
        budget = intent.budget
        if ledger.child_count >= _positive_int(policy.get("max_children"), 0):
            raise DomainConflict("DELEGATION_COUNT_EXCEEDED", "child Run count limit exceeded")
        if (
            ledger.token_direct_consumed
            + ledger.token_child_consumed
            + ledger.token_child_reserved
            + budget.token_limit
            > ledger.token_limit
        ):
            raise DomainConflict("DELEGATION_BUDGET_EXCEEDED", "token budget reservation exceeded")
        if (
            ledger.cost_direct_consumed_microunits
            + ledger.cost_child_consumed_microunits
            + ledger.cost_child_reserved_microunits
            + budget.cost_limit_microunits
            > ledger.cost_limit_microunits
        ):
            raise DomainConflict("DELEGATION_BUDGET_EXCEEDED", "cost budget reservation exceeded")
        if (
            ledger.tool_calls_direct_consumed
            + ledger.tool_calls_child_consumed
            + ledger.tool_calls_child_reserved
            + budget.tool_call_limit
            > ledger.tool_call_limit
        ):
            raise DomainConflict(
                "DELEGATION_BUDGET_EXCEEDED", "tool-call budget reservation exceeded"
            )
        ledger.token_child_reserved += budget.token_limit
        ledger.cost_child_reserved_microunits += budget.cost_limit_microunits
        ledger.tool_calls_child_reserved += budget.tool_call_limit
        ledger.child_count += 1
        ledger.revision += 1

    @staticmethod
    def _permission_snapshot(
        runtime_session: RuntimeSession,
        target: AgentVersion,
        intent: DelegationIntent,
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        endpoint = runtime_session.model_endpoint_snapshot or {}
        parent = {
            **runtime_session.tool_policy_snapshot,
            "model_endpoint_id": endpoint.get("id"),
            "model": endpoint.get("model"),
        }
        child = {
            **target.tool_policy,
            "model_endpoint_id": (
                str(target.model_endpoint_id) if target.model_endpoint_id else None
            ),
            "model": target.model_name or target.model_config_json.get("model"),
        }
        restrictions = dict(intent.permission_restrictions)
        allowed_secrets = set(policy.get("allowed_secret_refs", []))
        requested_secrets = set(restrictions.get("secrets", allowed_secrets))
        restrictions["secrets"] = sorted(allowed_secrets & requested_secrets)
        permissions = narrow_child_permissions(
            parent=parent, child=child, restrictions=restrictions
        )
        permissions["knowledge_policy"] = narrow_knowledge_policy(
            runtime_session.knowledge_policy_snapshot,
            target.memory_policy,
            target.skill_policy,
            restrictions,
        )
        return permissions

    @staticmethod
    async def _insert_closure(
        session: AsyncSession,
        context: TenantContext,
        parent_run_id: UUID,
        child_run_id: UUID,
        delegation_id: UUID,
    ) -> None:
        ancestors = list(
            await session.scalars(
                select(AgentRunRelation).where(
                    AgentRunRelation.tenant_id == context.tenant_id,
                    AgentRunRelation.descendant_run_id == parent_run_id,
                )
            )
        )
        session.add(
            AgentRunRelation(
                tenant_id=context.tenant_id,
                ancestor_run_id=parent_run_id,
                descendant_run_id=child_run_id,
                delegation_id=delegation_id,
                depth=1,
                is_direct=True,
            )
        )
        for relation in ancestors:
            session.add(
                AgentRunRelation(
                    tenant_id=context.tenant_id,
                    ancestor_run_id=relation.ancestor_run_id,
                    descendant_run_id=child_run_id,
                    delegation_id=delegation_id,
                    depth=relation.depth + 1,
                    is_direct=False,
                )
            )

    @staticmethod
    def _record(session: AsyncSession, context: TenantContext, delegation: Delegation) -> None:
        payload = {
            "parent_run_id": str(delegation.parent_run_id),
            "child_run_id": str(delegation.child_run_id),
            "target_agent_version_id": str(delegation.target_agent_version_id),
            "status": delegation.status,
        }
        session.add(
            Event(
                tenant_id=context.tenant_id,
                event_type="DelegationAccepted",
                aggregate_type="delegation",
                aggregate_id=delegation.id,
                run_id=delegation.parent_run_id,
                actor_id=context.actor_id,
                payload=payload,
                correlation_id=context.correlation_id,
            )
        )
        session.add(
            AuditRecord(
                tenant_id=context.tenant_id,
                action="coordination.delegate",
                resource_type="delegation",
                resource_id=delegation.id,
                actor_id=context.actor_id,
                details=payload,
                correlation_id=context.correlation_id,
            )
        )

    @staticmethod
    def _record_tree_cancel(
        session: AsyncSession,
        context: TenantContext,
        root: Run,
        run_ids: list[UUID],
    ) -> None:
        payload = {"root_run_id": str(root.id), "run_ids": [str(value) for value in run_ids]}
        session.add(
            Event(
                tenant_id=context.tenant_id,
                event_type="RunTreeCancelled",
                aggregate_type="run",
                aggregate_id=root.id,
                run_id=root.id,
                actor_id=context.actor_id,
                payload=payload,
                correlation_id=context.correlation_id,
            )
        )
        session.add(
            AuditRecord(
                tenant_id=context.tenant_id,
                action="coordination.tree.cancel",
                resource_type="run",
                resource_id=root.id,
                actor_id=context.actor_id,
                details=payload,
                correlation_id=context.correlation_id,
            )
        )

    @staticmethod
    def _result(delegation: Delegation, *, replay: bool = False) -> DelegationResult:
        return DelegationResult(
            delegation_id=delegation.id,
            child_task_id=delegation.child_task_id,
            child_run_id=delegation.child_run_id,
            status=DelegationStatus(delegation.status),
            idempotent_replay=replay,
        )


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def _nonnegative_int(value: Any, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return default
