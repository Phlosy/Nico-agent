"""Tenant-scoped persistence boundary for runtime execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.database import Database, RunClaim, TenantContext
from nico_agent.domain.models import (
    AgentVersion,
    AuditRecord,
    Event,
    Run,
    RunStep,
    RuntimeSession,
    Task,
    Tenant,
)
from nico_agent.domain.states import RunStatus, RunStepStatus, TaskStatus
from nico_agent.runtime.contracts import (
    RuntimeCapability,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeProviderDescriptor,
    RuntimeResult,
    RuntimeSessionHandle,
    RuntimeSessionRequest,
    RuntimeSessionStatus,
    RuntimeTrajectory,
)
from nico_agent.runtime.errors import RuntimeLeaseLost, RuntimeRecoveryUnsupported
from nico_agent.runtime.registry import RuntimeProviderRegistry
from nico_agent.tools.policy import build_tool_policy_snapshot

_ACTIVE_RUN_STATUSES = {
    RunStatus.PENDING,
    RunStatus.PLANNING,
    RunStatus.RUNNING,
    RunStatus.PAUSED,
    RunStatus.WAITING_FOR_TOOL,
    RunStatus.WAITING_FOR_APPROVAL,
}
_TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.TIMED_OUT,
}


@dataclass(frozen=True, slots=True)
class PreparedRuntime:
    claim: RunClaim
    provider_name: str
    descriptor: RuntimeProviderDescriptor
    request: RuntimeSessionRequest
    external_session_id: str | None
    recovering: bool
    timeout_seconds: int | None


class RuntimeExecutionService:
    """Owns all ORM writes; providers receive immutable DTOs only."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def prepare_claim(
        self,
        claim: RunClaim,
        *,
        worker_id: str,
        registry: RuntimeProviderRegistry,
    ) -> PreparedRuntime:
        context = self._context(claim, worker_id)
        async with self.database.tenant_transaction(context) as session:
            run = await self._owned_run(session, claim, worker_id)
            task = await session.scalar(
                select(Task).where(Task.tenant_id == claim.tenant_id, Task.id == run.task_id)
            )
            version = await session.scalar(
                select(AgentVersion).where(
                    AgentVersion.tenant_id == claim.tenant_id,
                    AgentVersion.id == run.agent_version_id,
                )
            )
            tenant = await session.scalar(select(Tenant).where(Tenant.id == claim.tenant_id))
            if task is None or version is None or tenant is None:
                raise RuntimeLeaseLost(str(claim.run_id))

            provider_name = self._provider_name(version)
            provider = registry.get(provider_name)
            descriptor = provider.descriptor
            runtime_session = await session.scalar(
                select(RuntimeSession)
                .where(
                    RuntimeSession.tenant_id == claim.tenant_id,
                    RuntimeSession.run_id == run.id,
                )
                .with_for_update()
            )
            recovering = claim.previous_status != RunStatus.PENDING.value
            if recovering:
                if runtime_session is None:
                    raise RuntimeRecoveryUnsupported(
                        provider_name, "an active run has no persisted runtime session"
                    )
                if RuntimeCapability.RESUME not in descriptor.capabilities:
                    raise RuntimeRecoveryUnsupported(
                        provider_name, "the selected provider does not support recovery"
                    )
                if runtime_session.provider_name != provider_name:
                    raise RuntimeRecoveryUnsupported(
                        provider_name, "the persisted runtime session belongs to another provider"
                    )

            if runtime_session is None:
                runtime_session = RuntimeSession(
                    tenant_id=claim.tenant_id,
                    run_id=run.id,
                    provider_name=descriptor.name,
                    provider_version=descriptor.version,
                    protocol_version=descriptor.protocol_version,
                    capabilities=sorted(capability.value for capability in descriptor.capabilities),
                    tool_policy_snapshot=build_tool_policy_snapshot(
                        tenant.settings,
                        version.tool_policy,
                        plugin_refs=version.plugin_refs,
                    ),
                )
                session.add(runtime_session)
                await session.flush()
            elif not runtime_session.tool_policy_snapshot:
                runtime_session.tool_policy_snapshot = build_tool_policy_snapshot(
                    tenant.settings,
                    version.tool_policy,
                    plugin_refs=version.plugin_refs,
                )
                runtime_session.revision += 1

            if RunStatus(run.status) is RunStatus.PENDING:
                run.status = RunStatus.PLANNING.value
                run.started_at = datetime.now(UTC)
                run.revision += 1
                self._record(
                    session,
                    context,
                    event_type="RunPlanningStarted",
                    aggregate_type="run",
                    aggregate_id=run.id,
                    action="runtime.claim",
                    payload={"worker_id": worker_id, "revision": run.revision},
                    run_id=run.id,
                )

            checkpoint = runtime_session.checkpoint or run.checkpoint
            request = RuntimeSessionRequest(
                tenant_id=claim.tenant_id,
                run_id=run.id,
                task_id=task.id,
                agent_id=run.agent_id,
                agent_version_id=version.id,
                task_title=task.title,
                task_input=task.input,
                acceptance=task.acceptance,
                role=version.role,
                mandate=version.mandate,
                boundaries=version.boundaries,
                long_term_goal=version.long_term_goal,
                current_goal=version.current_goal,
                model_config_data=version.model_config_json,
                run_config=version.run_config,
                budgets={**version.budgets, **run.budgets},
                checkpoint=checkpoint,
                event_sequence=runtime_session.last_event_sequence,
                resume_session_id=runtime_session.external_session_id if recovering else None,
            )
            await session.flush()
            return PreparedRuntime(
                claim=claim,
                provider_name=provider_name,
                descriptor=descriptor,
                request=request,
                external_session_id=runtime_session.external_session_id,
                recovering=recovering,
                timeout_seconds=run.timeout_seconds,
            )

    async def bind_session(
        self,
        prepared: PreparedRuntime,
        *,
        worker_id: str,
        handle: RuntimeSessionHandle,
    ) -> None:
        context = self._context(prepared.claim, worker_id)
        async with self.database.tenant_transaction(context) as session:
            run = await self._owned_run(session, prepared.claim, worker_id)
            runtime_session = await self._runtime_session(session, prepared.claim, for_update=True)
            runtime_session.external_session_id = handle.external_session_id
            runtime_session.status = handle.status.value
            runtime_session.capabilities = sorted(item.value for item in handle.capabilities)
            runtime_session.provider_state = handle.metadata
            runtime_session.revision += 1
            self._record(
                session,
                context,
                event_type="RuntimeSessionBound",
                aggregate_type="runtime_session",
                aggregate_id=runtime_session.id,
                action="runtime.session.bind",
                payload={
                    "provider": runtime_session.provider_name,
                    "external_session_id": handle.external_session_id,
                },
                run_id=run.id,
            )
            await session.flush()

    async def record_event(
        self,
        prepared: PreparedRuntime,
        *,
        worker_id: str,
        event: RuntimeEvent,
    ) -> bool:
        context = self._context(prepared.claim, worker_id)
        async with self.database.tenant_transaction(context) as session:
            run = await self._owned_run(session, prepared.claim, worker_id)
            runtime_session = await self._runtime_session(session, prepared.claim, for_update=True)
            if event.sequence <= runtime_session.last_event_sequence:
                return False
            if event.sequence != runtime_session.last_event_sequence + 1:
                raise ValueError("runtime event sequence must be contiguous")

            runtime_session.last_event_sequence = event.sequence
            runtime_session.revision += 1
            external_session_id = event.payload.get("external_session_id")
            if isinstance(external_session_id, str) and external_session_id:
                runtime_session.external_session_id = external_session_id[:500]
            if event.type in {RuntimeEventType.RUN_STARTED, RuntimeEventType.RUN_RESUMED}:
                runtime_session.status = RuntimeSessionStatus.RUNNING.value
                runtime_session.started_at = runtime_session.started_at or event.occurred_at
                if RunStatus(run.status) in {RunStatus.PLANNING, RunStatus.PAUSED}:
                    run.status = RunStatus.RUNNING.value
                    run.revision += 1
            elif event.type is RuntimeEventType.RUN_PAUSED:
                runtime_session.status = RuntimeSessionStatus.PAUSED.value
                if RunStatus(run.status) is RunStatus.RUNNING:
                    run.status = RunStatus.PAUSED.value
                    run.revision += 1
            elif event.type is RuntimeEventType.STEP_STARTED:
                await self._step_started(session, run, event)
            elif event.type is RuntimeEventType.STEP_COMPLETED:
                await self._step_completed(session, run, event)
            elif event.type is RuntimeEventType.CHECKPOINT_SAVED:
                runtime_session.checkpoint = event.payload
                run.checkpoint = event.payload
                run.revision += 1

            self._record(
                session,
                context,
                event_type=self._event_name(event.type),
                aggregate_type="runtime_session",
                aggregate_id=runtime_session.id,
                action="runtime.event.record",
                payload={
                    "provider_sequence": event.sequence,
                    "type": event.type.value,
                    "message": event.message,
                    "payload": event.payload,
                    "occurred_at": event.occurred_at.isoformat(),
                },
                run_id=run.id,
            )
            await session.flush()
            return True

    async def heartbeat(
        self,
        claim: RunClaim,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        context = self._context(claim, worker_id)
        async with self.database.tenant_transaction(context) as session:
            run = await session.scalar(
                select(Run)
                .where(Run.tenant_id == claim.tenant_id, Run.id == claim.run_id)
                .with_for_update()
            )
            if not self._owns(run, claim, worker_id):
                return False
            if RunStatus(run.status) not in _ACTIVE_RUN_STATUSES:
                return False
            now = datetime.now(UTC)
            run.heartbeat_at = now
            run.lease_expires_at = now + timedelta(seconds=lease_seconds)
            return True

    async def complete_claim(
        self,
        prepared: PreparedRuntime,
        *,
        worker_id: str,
        result: RuntimeResult,
        trajectory: RuntimeTrajectory,
        run_status: RunStatus | None = None,
    ) -> bool:
        context = self._context(prepared.claim, worker_id)
        async with self.database.tenant_transaction(context) as session:
            run = await session.scalar(
                select(Run)
                .where(Run.tenant_id == prepared.claim.tenant_id, Run.id == prepared.claim.run_id)
                .with_for_update()
            )
            if not self._owns(run, prepared.claim, worker_id):
                return False
            if RunStatus(run.status) in _TERMINAL_RUN_STATUSES:
                return False
            runtime_session = await self._runtime_session(session, prepared.claim, for_update=True)
            now = datetime.now(UTC)
            target = run_status or {
                RuntimeSessionStatus.COMPLETED: RunStatus.COMPLETED,
                RuntimeSessionStatus.CANCELLED: RunStatus.CANCELLED,
            }.get(result.status, RunStatus.FAILED)
            runtime_session.status = result.status.value
            runtime_session.checkpoint = result.checkpoint
            runtime_session.usage = result.usage
            runtime_session.trajectory = trajectory.model_dump(mode="json")
            runtime_session.external_session_id = (
                result.external_session_id or trajectory.external_session_id
            )
            runtime_session.ended_at = now
            runtime_session.revision += 1
            run.status = target.value
            run.result = result.output
            run.error = result.error
            run.cost = result.usage
            run.checkpoint = result.checkpoint
            run.ended_at = now
            run.revision += 1
            self._clear_lease(run)

            task = await session.scalar(
                select(Task)
                .where(Task.tenant_id == prepared.claim.tenant_id, Task.id == run.task_id)
                .with_for_update()
            )
            if task is not None and TaskStatus(task.status) is TaskStatus.RUNNING:
                task.status = {
                    RunStatus.COMPLETED: TaskStatus.WAITING_FOR_REVIEW,
                    RunStatus.CANCELLED: TaskStatus.CANCELLED,
                }.get(target, TaskStatus.FAILED).value
                task.revision += 1
            self._record(
                session,
                context,
                event_type={
                    RunStatus.COMPLETED: "RunCompleted",
                    RunStatus.CANCELLED: "RunCancelled",
                    RunStatus.TIMED_OUT: "RunTimedOut",
                }.get(target, "RunFailed"),
                aggregate_type="run",
                aggregate_id=run.id,
                action="runtime.complete",
                payload={"status": run.status, "revision": run.revision},
                run_id=run.id,
            )
            await session.flush()
            return True

    async def fail_claim(
        self,
        prepared: PreparedRuntime,
        *,
        worker_id: str,
        code: str,
        message: str,
        timed_out: bool = False,
    ) -> bool:
        result = RuntimeResult(
            status=RuntimeSessionStatus.FAILED,
            error={"code": code, "message": message},
            checkpoint=prepared.request.checkpoint,
        )
        trajectory = RuntimeTrajectory(
            provider=prepared.descriptor.name,
            provider_version=prepared.descriptor.version,
            external_session_id=prepared.external_session_id or f"unbound:{prepared.claim.run_id}",
            status=RuntimeSessionStatus.FAILED,
            events=[],
            metadata={"failure_before_export": True},
        )
        committed = await self.complete_claim(
            prepared,
            worker_id=worker_id,
            result=result,
            trajectory=trajectory,
            run_status=RunStatus.TIMED_OUT if timed_out else None,
        )
        return committed

    async def fail_unprepared_claim(
        self,
        claim: RunClaim,
        *,
        worker_id: str,
        code: str,
        message: str,
    ) -> bool:
        """Make configuration/recovery failures terminal even before a session exists."""

        context = self._context(claim, worker_id)
        async with self.database.tenant_transaction(context) as session:
            run = await session.scalar(
                select(Run)
                .where(Run.tenant_id == claim.tenant_id, Run.id == claim.run_id)
                .with_for_update()
            )
            if not self._owns(run, claim, worker_id):
                return False
            if RunStatus(run.status) in _TERMINAL_RUN_STATUSES:
                return False
            run.status = RunStatus.FAILED.value
            run.error = {"code": code, "message": message}
            run.ended_at = datetime.now(UTC)
            run.revision += 1
            self._clear_lease(run)
            task = await session.scalar(
                select(Task)
                .where(Task.tenant_id == claim.tenant_id, Task.id == run.task_id)
                .with_for_update()
            )
            if task is not None and TaskStatus(task.status) is TaskStatus.RUNNING:
                task.status = TaskStatus.FAILED.value
                task.revision += 1
            self._record(
                session,
                context,
                event_type="RunFailed",
                aggregate_type="run",
                aggregate_id=run.id,
                action="runtime.prepare.fail",
                payload={"status": run.status, "code": code, "revision": run.revision},
                run_id=run.id,
            )
            await session.flush()
            return True

    @staticmethod
    def _provider_name(version: AgentVersion) -> str:
        candidate = version.run_config.get("runtime_provider")
        if candidate is None:
            candidate = version.model_config_json.get("runtime_provider", "mock")
        if not isinstance(candidate, str) or not candidate:
            raise ValueError("runtime_provider must be a non-empty string")
        return candidate

    @staticmethod
    def _context(claim: RunClaim, worker_id: str) -> TenantContext:
        return TenantContext(claim.tenant_id, f"worker:{worker_id}", uuid4())

    @staticmethod
    def _owns(run: Run | None, claim: RunClaim, worker_id: str) -> bool:
        return bool(
            run is not None
            and run.lease_owner == worker_id
            and run.lease_token == claim.lease_token
            and run.lease_expires_at is not None
            and run.lease_expires_at > datetime.now(UTC)
        )

    async def _owned_run(self, session: AsyncSession, claim: RunClaim, worker_id: str) -> Run:
        run = await session.scalar(
            select(Run)
            .where(Run.tenant_id == claim.tenant_id, Run.id == claim.run_id)
            .with_for_update()
        )
        if not self._owns(run, claim, worker_id):
            raise RuntimeLeaseLost(str(claim.run_id))
        if RunStatus(run.status) not in _ACTIVE_RUN_STATUSES:
            raise RuntimeLeaseLost(str(claim.run_id))
        return run

    @staticmethod
    async def _runtime_session(
        session: AsyncSession, claim: RunClaim, *, for_update: bool
    ) -> RuntimeSession:
        statement = select(RuntimeSession).where(
            RuntimeSession.tenant_id == claim.tenant_id,
            RuntimeSession.run_id == claim.run_id,
        )
        if for_update:
            statement = statement.with_for_update()
        runtime_session = await session.scalar(statement)
        if runtime_session is None:
            raise RuntimeLeaseLost(str(claim.run_id))
        return runtime_session

    @staticmethod
    async def _step_started(session: AsyncSession, run: Run, event: RuntimeEvent) -> None:
        sequence = int(event.payload.get("index", event.sequence))
        step = await session.scalar(
            select(RunStep).where(
                RunStep.tenant_id == run.tenant_id,
                RunStep.run_id == run.id,
                RunStep.sequence == sequence,
            )
        )
        if step is None:
            step = RunStep(
                tenant_id=run.tenant_id,
                run_id=run.id,
                sequence=sequence,
                kind=str(event.payload.get("name", "runtime"))[:100],
                status=RunStepStatus.RUNNING.value,
                input=event.payload,
                started_at=event.occurred_at,
            )
            session.add(step)
        elif RunStepStatus(step.status) is RunStepStatus.PENDING:
            step.status = RunStepStatus.RUNNING.value
            step.started_at = event.occurred_at
            step.revision += 1

    @staticmethod
    async def _step_completed(session: AsyncSession, run: Run, event: RuntimeEvent) -> None:
        sequence = int(event.payload.get("index", event.sequence))
        step = await session.scalar(
            select(RunStep)
            .where(
                RunStep.tenant_id == run.tenant_id,
                RunStep.run_id == run.id,
                RunStep.sequence == sequence,
            )
            .with_for_update()
        )
        if step is None:
            step = RunStep(
                tenant_id=run.tenant_id,
                run_id=run.id,
                sequence=sequence,
                kind=str(event.payload.get("name", "runtime"))[:100],
                status=RunStepStatus.COMPLETED.value,
                output=event.payload,
                started_at=event.occurred_at,
                ended_at=event.occurred_at,
            )
            session.add(step)
        elif RunStepStatus(step.status) is RunStepStatus.RUNNING:
            step.status = RunStepStatus.COMPLETED.value
            step.output = event.payload
            step.ended_at = event.occurred_at
            step.revision += 1

    @staticmethod
    def _event_name(event_type: RuntimeEventType) -> str:
        return "Runtime" + "".join(part.title() for part in event_type.value.split("."))

    @staticmethod
    def _clear_lease(run: Run) -> None:
        run.lease_owner = None
        run.lease_token = None
        run.lease_expires_at = None
        run.heartbeat_at = None

    @staticmethod
    def _record(
        session: AsyncSession,
        context: TenantContext,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: UUID,
        action: str,
        payload: dict[str, Any],
        run_id: UUID,
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
