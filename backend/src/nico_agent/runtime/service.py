"""Tenant-scoped persistence boundary for runtime execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.conversations.context import select_conversation_context
from nico_agent.coordination.policy import (
    build_coordination_policy_snapshot,
    narrow_coordination_policy,
)
from nico_agent.database import Database, RunClaim, TenantContext
from nico_agent.domain.models import (
    Agent,
    AgentMessage,
    AgentVersion,
    Artifact,
    AuditRecord,
    ContextSnapshot,
    Conversation,
    ConversationTurn,
    Delegation,
    Event,
    ModelCall,
    ModelEndpoint,
    Plan,
    PlanStep,
    Project,
    ProjectMember,
    ProjectSession,
    Run,
    RunBudgetLedger,
    RunStep,
    RuntimeEvaluation,
    RuntimeKnowledgeUsage,
    RuntimeSession,
    Task,
    Tenant,
    ToolApprovalRequest,
)
from nico_agent.domain.states import ModelCallStatus, RunStatus, RunStepStatus, TaskStatus
from nico_agent.projects.interventions import ProjectInterventionService
from nico_agent.projects.metadata import is_managed_project
from nico_agent.projects.orchestration import ProjectOrchestrationService
from nico_agent.runtime.contracts import (
    RuntimeCapability,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeExecutionMode,
    RuntimeProviderDescriptor,
    RuntimeResult,
    RuntimeSessionHandle,
    RuntimeSessionRequest,
    RuntimeSessionStatus,
    RuntimeTrajectory,
)
from nico_agent.runtime.errors import RuntimeLeaseLost, RuntimeRecoveryUnsupported
from nico_agent.runtime.preparation import (
    RuntimePreparationService,
    build_knowledge_policy_snapshot,
)
from nico_agent.runtime.registry import RuntimeProviderRegistry
from nico_agent.tools.policy import build_tool_policy_snapshot, narrow_tool_policy_snapshot

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
LEGACY_PROVIDER_RESOLVER_REMOVAL_VERSION = "0.4.0"


@dataclass(frozen=True, slots=True)
class RuntimeProviderResolution:
    name: str
    source: str
    legacy: bool

    @property
    def telemetry(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "source": self.source,
            "legacy": self.legacy,
            "removal_version": (LEGACY_PROVIDER_RESOLVER_REMOVAL_VERSION if self.legacy else None),
        }


def resolve_runtime_provider(
    *,
    runtime_provider: str | None,
    run_config: dict[str, Any],
    model_config: dict[str, Any],
) -> RuntimeProviderResolution:
    """Resolve one provider while making every legacy branch observable."""

    if runtime_provider is not None:
        candidate = runtime_provider
        source = "agent_version"
        legacy = False
    elif run_config.get("runtime_provider") is not None:
        candidate = run_config["runtime_provider"]
        source = "legacy_run_config"
        legacy = True
    elif model_config.get("runtime_provider") is not None:
        candidate = model_config["runtime_provider"]
        source = "legacy_model_config"
        legacy = True
    else:
        candidate = "mock"
        source = "legacy_default_mock"
        legacy = True
    if not isinstance(candidate, str) or not candidate:
        raise ValueError("runtime_provider must be a non-empty string")
    return RuntimeProviderResolution(candidate, source, legacy)


def resolve_runtime_provider_name(
    *,
    runtime_provider: str | None,
    run_config: dict[str, Any],
    model_config: dict[str, Any],
) -> str:
    """Resolve additive provider configuration without rewriting legacy rows."""

    return resolve_runtime_provider(
        runtime_provider=runtime_provider,
        run_config=run_config,
        model_config=model_config,
    ).name


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
        self.preparation = RuntimePreparationService(database)

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

            resolution = self._provider_resolution(version)
            execution_mode = self._execution_mode(version)
            model_endpoint = None
            if version.model_endpoint_id is not None:
                model_endpoint = await session.scalar(
                    select(ModelEndpoint).where(
                        ModelEndpoint.tenant_id == claim.tenant_id,
                        ModelEndpoint.id == version.model_endpoint_id,
                    )
                )
                if model_endpoint is None:
                    raise ValueError("configured model endpoint is unavailable")
            model_endpoint_snapshot = self._model_endpoint_snapshot(model_endpoint, version)
            runtime_session = await session.scalar(
                select(RuntimeSession)
                .where(
                    RuntimeSession.tenant_id == claim.tenant_id,
                    RuntimeSession.run_id == run.id,
                )
                .with_for_update()
            )
            provider_name = (
                runtime_session.provider_name if runtime_session is not None else resolution.name
            )
            provider = registry.get(provider_name)
            descriptor = provider.descriptor
            inbound_delegation = await session.scalar(
                select(Delegation).where(
                    Delegation.tenant_id == claim.tenant_id,
                    Delegation.child_run_id == run.id,
                )
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
                if not self._descriptor_can_resume(runtime_session, descriptor):
                    raise RuntimeRecoveryUnsupported(
                        provider_name,
                        "the registered provider is incompatible with the persisted "
                        "provider/protocol version",
                    )

            if runtime_session is None:
                project_member_versions = await self._project_member_version_ids(
                    session,
                    task,
                    run,
                )
                coordination_snapshot = build_coordination_policy_snapshot(
                    tenant.settings,
                    version.coordination_policy,
                    project_member_version_ids=project_member_versions,
                )
                tool_snapshot = build_tool_policy_snapshot(
                    tenant.settings,
                    version.tool_policy,
                    plugin_refs=version.plugin_refs,
                )
                knowledge_snapshot = build_knowledge_policy_snapshot(
                    tenant.settings,
                    version.memory_policy,
                    version.skill_policy,
                )
                if inbound_delegation is not None:
                    coordination_snapshot = narrow_coordination_policy(
                        inbound_delegation.policy_snapshot, coordination_snapshot
                    )
                    tool_snapshot = inbound_delegation.permission_snapshot
                    inherited_knowledge = inbound_delegation.permission_snapshot.get(
                        "knowledge_policy"
                    )
                    knowledge_snapshot = (
                        inherited_knowledge
                        if isinstance(inherited_knowledge, dict)
                        else build_knowledge_policy_snapshot({}, {}, {})
                    )
                if "tool_allow" in run.budgets:
                    tool_snapshot = narrow_tool_policy_snapshot(
                        tool_snapshot,
                        run.budgets.get("tool_allow"),
                    )
                runtime_session = RuntimeSession(
                    tenant_id=claim.tenant_id,
                    run_id=run.id,
                    provider_name=descriptor.name,
                    provider_version=descriptor.version,
                    protocol_version=descriptor.protocol_version,
                    provider_resolution_source=resolution.source,
                    legacy_resolver_used=resolution.legacy,
                    provider_compatibility={
                        "implementation": descriptor.implementation,
                        **descriptor.compatibility,
                    },
                    capabilities=sorted(capability.value for capability in descriptor.capabilities),
                    execution_mode=execution_mode.value,
                    loop_state="initializing",
                    execution_manifest=self._execution_manifest(version),
                    model_endpoint_snapshot=model_endpoint_snapshot,
                    tool_policy_snapshot=tool_snapshot,
                    coordination_policy_snapshot=coordination_snapshot,
                    knowledge_policy_snapshot=knowledge_snapshot,
                )
                session.add(runtime_session)
                await session.flush()
                self._record(
                    session,
                    context,
                    event_type=(
                        "LegacyRuntimeProviderResolved"
                        if resolution.legacy
                        else "RuntimeProviderResolved"
                    ),
                    aggregate_type="runtime_session",
                    aggregate_id=runtime_session.id,
                    action="runtime.provider.resolve",
                    payload=resolution.telemetry,
                    run_id=run.id,
                )
            elif not runtime_session.tool_policy_snapshot:
                runtime_session.tool_policy_snapshot = build_tool_policy_snapshot(
                    tenant.settings,
                    version.tool_policy,
                    plugin_refs=version.plugin_refs,
                )
                if "tool_allow" in run.budgets:
                    runtime_session.tool_policy_snapshot = narrow_tool_policy_snapshot(
                        runtime_session.tool_policy_snapshot,
                        run.budgets.get("tool_allow"),
                    )
                runtime_session.revision += 1
            if not runtime_session.coordination_policy_snapshot:
                project_member_versions = await self._project_member_version_ids(
                    session,
                    task,
                    run,
                )
                runtime_session.coordination_policy_snapshot = build_coordination_policy_snapshot(
                    tenant.settings,
                    version.coordination_policy,
                    project_member_version_ids=project_member_versions,
                )
                runtime_session.revision += 1
            if not runtime_session.knowledge_policy_snapshot:
                runtime_session.knowledge_policy_snapshot = build_knowledge_policy_snapshot(
                    tenant.settings,
                    version.memory_policy,
                    version.skill_policy,
                )
                runtime_session.revision += 1

            prepared_knowledge = await self.preparation.prepare_in_session(
                session,
                run,
                task,
                version,
                runtime_session.knowledge_policy_snapshot,
                frozen_selection=runtime_session.knowledge_selection_snapshot or None,
                allow_recall=not recovering,
            )
            if not runtime_session.knowledge_selection_snapshot:
                runtime_session.knowledge_selection_snapshot = prepared_knowledge.selection_snapshot
                runtime_session.revision += 1
            await self._ensure_knowledge_usages(
                session,
                run,
                runtime_session,
                prepared_knowledge.selection_snapshot,
            )
            preliminary_budgets = {**version.budgets, **run.budgets}
            max_context_chars = preliminary_budgets.get("context_max_chars", 64_000)
            if not isinstance(max_context_chars, int):
                max_context_chars = 64_000
            frozen_conversation_context = runtime_session.execution_manifest.get(
                "conversation_context"
            )
            if frozen_conversation_context is not None and not isinstance(
                frozen_conversation_context, dict
            ):
                raise ValueError("frozen conversation context must be an object")
            conversation_context = frozen_conversation_context
            if conversation_context is None and not recovering:
                conversation_context = await select_conversation_context(
                    session,
                    run,
                    max_chars=max_context_chars,
                )
                if conversation_context is not None:
                    runtime_session.execution_manifest = {
                        **runtime_session.execution_manifest,
                        "conversation_context": conversation_context,
                    }
                    runtime_session.revision += 1
            context_seed = prepared_knowledge.context_seed
            if conversation_context is not None:
                context_seed = context_seed.model_copy(
                    update={
                        "source_refs": context_seed.source_refs
                        + tuple(conversation_context.get("source_refs", [])),
                        "untrusted_context": context_seed.untrusted_context
                        + tuple(conversation_context.get("untrusted_context", [])),
                        "effect_metadata": {
                            **context_seed.effect_metadata,
                            "conversation_context": conversation_context,
                        },
                    }
                )

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
                if inbound_delegation is not None and inbound_delegation.status == "accepted":
                    inbound_delegation.status = "running"
                    inbound_delegation.started_at = datetime.now(UTC)
                    inbound_delegation.revision += 1

            checkpoint = runtime_session.checkpoint or run.checkpoint
            recovery_state: dict[str, Any] = {}
            if recovering:
                model_calls = list(
                    await session.scalars(
                        select(ModelCall)
                        .where(
                            ModelCall.tenant_id == claim.tenant_id,
                            ModelCall.run_id == run.id,
                        )
                        .order_by(ModelCall.started_at, ModelCall.id)
                        .with_for_update()
                    )
                )
                now = datetime.now(UTC)
                interrupted_call_keys: list[str] = []
                for call in model_calls:
                    if call.status in {
                        ModelCallStatus.PENDING.value,
                        ModelCallStatus.STREAMING.value,
                    }:
                        call.status = ModelCallStatus.INTERRUPTED.value
                        call.error = {
                            "code": "WORKER_LEASE_LOST",
                            "message": "model call was interrupted before a terminal event",
                        }
                        call.ended_at = now
                        interrupted_call_keys.append(call.call_key)
                recovery_state = {
                    "model_call_keys": [call.call_key for call in model_calls],
                    "interrupted_model_call_keys": interrupted_call_keys,
                    "last_context_version": await session.scalar(
                        select(func.max(ContextSnapshot.version)).where(
                            ContextSnapshot.tenant_id == claim.tenant_id,
                            ContextSnapshot.run_id == run.id,
                        )
                    )
                    or 0,
                    "usage": self._recovered_usage(model_calls),
                }
                latest_plan = await session.scalar(
                    select(Plan)
                    .where(Plan.tenant_id == claim.tenant_id, Plan.run_id == run.id)
                    .order_by(Plan.revision.desc())
                    .limit(1)
                )
                if latest_plan is not None:
                    plan_steps = list(
                        await session.scalars(
                            select(PlanStep)
                            .where(
                                PlanStep.tenant_id == claim.tenant_id,
                                PlanStep.run_id == run.id,
                                PlanStep.plan_id == latest_plan.id,
                            )
                            .order_by(PlanStep.position)
                        )
                    )
                    reflection_evaluations = list(
                        await session.scalars(
                            select(RuntimeEvaluation)
                            .where(
                                RuntimeEvaluation.tenant_id == claim.tenant_id,
                                RuntimeEvaluation.run_id == run.id,
                                RuntimeEvaluation.plan_id == latest_plan.id,
                                RuntimeEvaluation.evaluation_type == "reflection",
                            )
                            .order_by(RuntimeEvaluation.sequence)
                        )
                    )
                    completed_steps = [step for step in plan_steps if step.status == "completed"]
                    latest_reflection = (
                        reflection_evaluations[-1] if reflection_evaluations else None
                    )
                    recovery_state["planning"] = {
                        "active_plan": {
                            "revision": latest_plan.revision,
                            "status": latest_plan.status,
                            "objective": latest_plan.objective,
                            "content_hash": latest_plan.content_hash,
                            "steps": [
                                {
                                    "key": step.step_key,
                                    "title": step.title,
                                    "instruction": step.instruction,
                                    "acceptance": step.acceptance,
                                    "depends_on": step.dependencies,
                                }
                                for step in plan_steps
                            ],
                            "completed_step_keys": [step.step_key for step in completed_steps],
                            "latest_output": completed_steps[-1].output
                            if completed_steps
                            else None,
                            "reflection_count": len(reflection_evaluations),
                            "latest_reflection": latest_reflection.result
                            if latest_reflection is not None
                            else None,
                        }
                    }
                coordination_messages = await self._coordination_recovery(
                    session,
                    run,
                    runtime_session,
                )
                if coordination_messages:
                    recovery_state["coordination_messages"] = coordination_messages
            budgets = {**version.budgets, **run.budgets}
            budgets.setdefault("max_iterations", run.max_steps)
            if run.token_budget is not None:
                budgets.setdefault("token_limit", run.token_budget)
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
                budgets=budgets,
                execution_mode=execution_mode,
                execution_manifest=runtime_session.execution_manifest,
                context_seed=context_seed,
                model_endpoint_snapshot=runtime_session.model_endpoint_snapshot,
                coordination_policy_snapshot=runtime_session.coordination_policy_snapshot,
                recovery_state=recovery_state,
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
            public_payload = await self._project_native_event(
                session, context, run, runtime_session, event
            )
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
                await self._step_started(session, run, runtime_session, event)
            elif event.type is RuntimeEventType.STEP_COMPLETED:
                await self._step_completed(session, run, runtime_session, event)
            elif event.type is RuntimeEventType.CHECKPOINT_SAVED:
                runtime_session.checkpoint = event.payload
                runtime_session.checkpoint_schema_version = int(
                    event.payload.get("schema_version", 1)
                )
                runtime_session.checkpoint_hash = self._optional_string(
                    event.payload.get("checkpoint_hash"), 64
                )
                runtime_session.checkpoint_revision += 1
                runtime_session.loop_state = str(
                    event.payload.get("loop_state", runtime_session.loop_state)
                )[:32]
                run.checkpoint = event.payload
                run.checkpoint_schema_version = runtime_session.checkpoint_schema_version
                run.checkpoint_hash = runtime_session.checkpoint_hash
                run.checkpoint_revision += 1
                run.revision += 1
                await self._ack_coordination_messages(
                    session,
                    run,
                    event.payload.get("consumed_message_ids", []),
                )

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
                    "payload": public_payload,
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
            runtime_session.loop_state = (
                "timed_out"
                if run_status is RunStatus.TIMED_OUT
                else {
                    RuntimeSessionStatus.COMPLETED: "completed",
                    RuntimeSessionStatus.CANCELLED: "cancelled",
                }.get(result.status, "failed")
            )
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

            await self._finalize_knowledge_usages(
                session,
                run,
                target,
                result=result.output,
                error=result.error,
                usage=result.usage,
                ended_at=now,
            )

            delegated_child = await self._complete_child_coordination(
                session,
                context,
                run,
                target,
                result.usage,
            )

            task = await session.scalar(
                select(Task)
                .where(Task.tenant_id == prepared.claim.tenant_id, Task.id == run.task_id)
                .with_for_update()
            )
            compaction = (
                task.acceptance.get("conversation_compaction", {})
                if task is not None and isinstance(task.acceptance, dict)
                else {}
            )
            if not isinstance(compaction, dict):
                compaction = {}
            if compaction and target is RunStatus.COMPLETED:
                conversation = await session.scalar(
                    select(Conversation)
                    .where(
                        Conversation.tenant_id == run.tenant_id,
                        Conversation.id == UUID(str(compaction["conversation_id"])),
                    )
                    .with_for_update()
                )
                if conversation is None:
                    raise ValueError("compaction Run references an unavailable Conversation")
                summary = self._result_text(result.output)
                if not summary:
                    raise ValueError("compaction Run completed without summary text")
                through_sequence = int(compaction["through_sequence"])
                if through_sequence >= conversation.summary_through_sequence:
                    conversation.summary = summary
                    conversation.summary_through_sequence = through_sequence
                    conversation.summary_input_hash = str(compaction["input_hash"])
                    conversation.summary_model_call_id = runtime_session.last_model_call_id
                    conversation.revision += 1
                    conversation.updated_at = now
                    self._record(
                        session,
                        context,
                        event_type="ConversationCompacted",
                        aggregate_type="conversation",
                        aggregate_id=conversation.id,
                        action="conversation.compact.complete",
                        payload={
                            "through_sequence": through_sequence,
                            "input_hash": conversation.summary_input_hash,
                            "model_call_id": (
                                str(runtime_session.last_model_call_id)
                                if runtime_session.last_model_call_id
                                else None
                            ),
                        },
                        run_id=run.id,
                    )
            if task is not None and TaskStatus(task.status) is TaskStatus.RUNNING:
                if delegated_child:
                    task.status = {
                        RunStatus.COMPLETED: TaskStatus.COMPLETED,
                        RunStatus.CANCELLED: TaskStatus.CANCELLED,
                    }.get(target, TaskStatus.FAILED).value
                else:
                    task.status = (
                        TaskStatus.COMPLETED.value
                        if compaction and target is RunStatus.COMPLETED
                        else {
                            RunStatus.COMPLETED: TaskStatus.WAITING_FOR_REVIEW,
                            RunStatus.CANCELLED: TaskStatus.CANCELLED,
                        }.get(target, TaskStatus.FAILED).value
                    )
                task.revision += 1
            await ProjectOrchestrationService.finalize_in_session(
                session,
                context,
                run,
                narrative_output=(result.output if target is RunStatus.COMPLETED else None),
            )
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

    @staticmethod
    def _result_text(output: dict[str, Any] | None) -> str:
        if not isinstance(output, dict):
            return ""
        content = output.get("content")
        if isinstance(content, str):
            return content.strip()
        result = output.get("result")
        if isinstance(result, dict) and isinstance(result.get("content"), str):
            return result["content"].strip()
        return json.dumps(output, sort_keys=True, ensure_ascii=False, default=str)

    async def suspend_claim(
        self,
        prepared: PreparedRuntime,
        *,
        worker_id: str,
        checkpoint: dict[str, Any],
        wake_condition: dict[str, Any],
        usage: dict[str, Any],
        trajectory: RuntimeTrajectory,
    ) -> bool:
        """Persist a v2 suspension and release the lease without terminating the Run."""

        context = self._context(prepared.claim, worker_id)
        async with self.database.tenant_transaction(context) as session:
            run = await session.scalar(
                select(Run)
                .where(
                    Run.tenant_id == prepared.claim.tenant_id,
                    Run.id == prepared.claim.run_id,
                )
                .with_for_update()
            )
            if not self._owns(run, prepared.claim, worker_id):
                return False
            runtime_session = await self._runtime_session(session, prepared.claim, for_update=True)
            runtime_session.status = RuntimeSessionStatus.SUSPENDED.value
            runtime_session.checkpoint = checkpoint
            runtime_session.usage = usage
            runtime_session.trajectory = trajectory.model_dump(mode="json")
            runtime_session.provider_state = {
                **runtime_session.provider_state,
                "wake_condition": wake_condition,
            }
            runtime_session.revision += 1
            wake_type = wake_condition.get("type")
            if wake_type == "tool_approval":
                approval_id = UUID(str(wake_condition["approval_id"]))
                approval = await session.scalar(
                    select(ToolApprovalRequest).where(
                        ToolApprovalRequest.tenant_id == run.tenant_id,
                        ToolApprovalRequest.run_id == run.id,
                        ToolApprovalRequest.id == approval_id,
                    )
                )
                if approval is None:
                    raise ValueError("tool approval suspension has no durable request")
                # A fast operator decision may race this final suspension write.
                # Preserve that wake-up by leaving an already-decided Run claimable.
                run.status = (
                    RunStatus.WAITING_FOR_APPROVAL.value
                    if approval.status == "requested"
                    else RunStatus.RUNNING.value
                )
                runtime_session.loop_state = "waiting_for_approval"
            else:
                run.status = RunStatus.WAITING_FOR_SUBAGENT.value
            run.checkpoint = checkpoint
            run.cost = usage
            run.revision += 1
            self._clear_lease(run)
            self._record(
                session,
                context,
                event_type=(
                    "ToolApprovalWaitSuspended"
                    if wake_type == "tool_approval"
                    else "RuntimeSuspended"
                ),
                aggregate_type="runtime_session",
                aggregate_id=runtime_session.id,
                action="runtime.suspend",
                payload={"wake_condition": wake_condition, "revision": runtime_session.revision},
                run_id=run.id,
            )
            await session.flush()
            return True

    async def _coordination_recovery(
        self,
        session: AsyncSession,
        run: Run,
        runtime_session: RuntimeSession,
    ) -> list[dict[str, Any]]:
        consumed = {
            str(value)
            for value in (runtime_session.checkpoint or {}).get("consumed_message_ids", [])
            if isinstance(value, str)
        }
        messages = list(
            await session.scalars(
                select(AgentMessage)
                .where(
                    AgentMessage.tenant_id == run.tenant_id,
                    AgentMessage.receiver_run_id == run.id,
                    AgentMessage.message_type == "result",
                )
                .order_by(AgentMessage.created_at, AgentMessage.id)
                .with_for_update()
            )
        )
        now = datetime.now(UTC)
        recovered: list[dict[str, Any]] = []
        for message in messages:
            if str(message.id) in consumed:
                if message.status != "acknowledged":
                    message.status = "acknowledged"
                    message.acknowledged_at = now
                continue
            if message.status == "queued":
                message.status = "delivered"
                message.delivered_at = now
            recovered.append(
                {
                    "message_id": str(message.id),
                    "delegation_id": str(message.delegation_id),
                    "child_run_id": str(message.sender_run_id),
                    **message.content,
                }
            )
        return recovered

    @staticmethod
    async def _ack_coordination_messages(
        session: AsyncSession,
        run: Run,
        message_ids: Any,
    ) -> None:
        if not isinstance(message_ids, list) or not message_ids:
            return
        parsed: list[UUID] = []
        for value in message_ids:
            try:
                parsed.append(UUID(str(value)))
            except (TypeError, ValueError):
                continue
        if not parsed:
            return
        messages = list(
            await session.scalars(
                select(AgentMessage)
                .where(
                    AgentMessage.tenant_id == run.tenant_id,
                    AgentMessage.receiver_run_id == run.id,
                    AgentMessage.id.in_(parsed),
                )
                .with_for_update()
            )
        )
        now = datetime.now(UTC)
        for message in messages:
            message.status = "acknowledged"
            message.delivered_at = message.delivered_at or now
            message.acknowledged_at = now

    async def _complete_child_coordination(
        self,
        session: AsyncSession,
        context: TenantContext,
        run: Run,
        target: RunStatus,
        usage: dict[str, Any],
    ) -> bool:
        delegation = await session.scalar(
            select(Delegation)
            .where(
                Delegation.tenant_id == run.tenant_id,
                Delegation.child_run_id == run.id,
            )
            .with_for_update()
        )
        if delegation is None:
            return False
        if delegation.status in {"completed", "failed", "cancelled", "rejected"}:
            return True
        delegation.status = {
            RunStatus.COMPLETED: "completed",
            RunStatus.CANCELLED: "cancelled",
        }.get(target, "failed")
        delegation.result = run.result
        delegation.error = run.error
        delegation.ended_at = datetime.now(UTC)
        delegation.revision += 1

        grant = delegation.budget_grant
        tokens = min(_usage_int(usage, "total_tokens"), int(grant.get("token_limit") or 0))
        cost = min(
            _usage_int(usage, "cost_microunits"),
            int(grant.get("cost_limit_microunits") or 0),
        )
        tool_calls = min(_usage_int(usage, "tool_calls"), int(grant.get("tool_call_limit") or 0))
        child_ledger = await session.scalar(
            select(RunBudgetLedger)
            .where(
                RunBudgetLedger.tenant_id == run.tenant_id,
                RunBudgetLedger.run_id == run.id,
            )
            .with_for_update()
        )
        if child_ledger is not None:
            child_ledger.token_direct_consumed = tokens
            child_ledger.cost_direct_consumed_microunits = cost
            child_ledger.tool_calls_direct_consumed = tool_calls
            child_ledger.revision += 1
        parent_ledger = await session.scalar(
            select(RunBudgetLedger)
            .where(
                RunBudgetLedger.tenant_id == run.tenant_id,
                RunBudgetLedger.run_id == delegation.parent_run_id,
            )
            .with_for_update()
        )
        if parent_ledger is not None:
            parent_ledger.token_child_reserved -= int(grant.get("token_limit") or 0)
            parent_ledger.cost_child_reserved_microunits -= int(
                grant.get("cost_limit_microunits") or 0
            )
            parent_ledger.tool_calls_child_reserved -= int(grant.get("tool_call_limit") or 0)
            parent_ledger.token_child_consumed += tokens
            parent_ledger.cost_child_consumed_microunits += cost
            parent_ledger.tool_calls_child_consumed += tool_calls
            parent_ledger.revision += 1

        existing_message = await session.scalar(
            select(AgentMessage.id).where(
                AgentMessage.tenant_id == run.tenant_id,
                AgentMessage.sender_run_id == run.id,
                AgentMessage.idempotency_key == f"result:{delegation.id}",
            )
        )
        artifact_refs = [
            f"artifact:{artifact_id}"
            for artifact_id in await session.scalars(
                select(Artifact.id).where(
                    Artifact.tenant_id == run.tenant_id,
                    Artifact.owner_run_id == run.id,
                    Artifact.status == "available",
                )
            )
        ]
        if existing_message is None:
            session.add(
                AgentMessage(
                    tenant_id=run.tenant_id,
                    delegation_id=delegation.id,
                    sender_run_id=run.id,
                    receiver_run_id=delegation.parent_run_id,
                    message_type="result",
                    content={
                        "status": delegation.status,
                        "result": run.result,
                        "error": run.error,
                        "artifact_refs": artifact_refs,
                    },
                    refs=artifact_refs,
                    visibility="parent_only",
                    status="queued",
                    idempotency_key=f"result:{delegation.id}",
                    correlation_id=context.correlation_id,
                )
            )
        await session.flush()
        active_children = (
            await session.scalar(
                select(func.count())
                .select_from(Delegation)
                .where(
                    Delegation.tenant_id == run.tenant_id,
                    Delegation.parent_run_id == delegation.parent_run_id,
                    Delegation.status.in_(["proposed", "accepted", "running"]),
                )
            )
            or 0
        )
        parent = await session.scalar(
            select(Run)
            .where(
                Run.tenant_id == run.tenant_id,
                Run.id == delegation.parent_run_id,
            )
            .with_for_update()
        )
        if (
            active_children == 0
            and parent is not None
            and RunStatus(parent.status) is RunStatus.WAITING_FOR_SUBAGENT
        ):
            parent.status = RunStatus.RUNNING.value
            parent.revision += 1
            self._clear_lease(parent)
            parent_runtime = await session.scalar(
                select(RuntimeSession)
                .where(
                    RuntimeSession.tenant_id == run.tenant_id,
                    RuntimeSession.run_id == parent.id,
                )
                .with_for_update()
            )
            if parent_runtime is not None:
                parent_runtime.status = RuntimeSessionStatus.PAUSED.value
                parent_runtime.provider_state = {
                    **parent_runtime.provider_state,
                    "coordination_woken_at": datetime.now(UTC).isoformat(),
                }
                parent_runtime.revision += 1
        self._record(
            session,
            context,
            event_type="DelegationTerminal",
            aggregate_type="delegation",
            aggregate_id=delegation.id,
            action="coordination.complete",
            payload={
                "status": delegation.status,
                "parent_run_id": str(delegation.parent_run_id),
                "child_run_id": str(run.id),
            },
            run_id=run.id,
        )
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
            await ProjectOrchestrationService.finalize_in_session(
                session,
                context,
                run,
                narrative_output=None,
            )
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
    async def _project_member_version_ids(
        session: AsyncSession,
        task: Task,
        run: Run,
    ) -> list[str] | None:
        if task.project_session_id is None:
            return None
        project = await session.scalar(
            select(Project).where(
                Project.tenant_id == task.tenant_id,
                Project.id == task.project_id,
            )
        )
        if project is None:
            raise ValueError("Project-scoped Run references an unavailable Project")
        if not is_managed_project(project):
            return None
        if project.status != "active":
            raise ValueError("archived Projects cannot initialize a RuntimeSession")
        lead = await session.scalar(
            select(ProjectMember)
            .join(
                ProjectSession,
                (ProjectSession.tenant_id == ProjectMember.tenant_id)
                & (ProjectSession.project_member_id == ProjectMember.id),
            )
            .where(
                ProjectMember.tenant_id == task.tenant_id,
                ProjectMember.project_id == task.project_id,
                ProjectMember.agent_id == run.agent_id,
                ProjectMember.role == "lead",
                ProjectMember.status == "active",
                ProjectSession.id == task.project_session_id,
                ProjectSession.status == "active",
            )
        )
        if lead is None:
            return None
        values = await session.scalars(
            select(AgentVersion.id)
            .join(
                Agent,
                (Agent.tenant_id == AgentVersion.tenant_id)
                & (Agent.id == AgentVersion.agent_id)
                & (Agent.current_version_id == AgentVersion.id),
            )
            .join(
                ProjectMember,
                (ProjectMember.tenant_id == Agent.tenant_id) & (ProjectMember.agent_id == Agent.id),
            )
            .join(
                ProjectSession,
                (ProjectSession.tenant_id == ProjectMember.tenant_id)
                & (ProjectSession.project_member_id == ProjectMember.id),
            )
            .where(
                ProjectMember.tenant_id == task.tenant_id,
                ProjectMember.project_id == task.project_id,
                ProjectMember.status == "active",
                ProjectSession.status == "active",
                Agent.status == "ready",
                AgentVersion.status == "published",
            )
            .order_by(AgentVersion.id)
        )
        return [str(value) for value in values]

    @staticmethod
    async def _ensure_knowledge_usages(
        session: AsyncSession,
        run: Run,
        runtime_session: RuntimeSession,
        selection_snapshot: dict[str, Any],
    ) -> None:
        existing = list(
            await session.scalars(
                select(RuntimeKnowledgeUsage).where(
                    RuntimeKnowledgeUsage.tenant_id == run.tenant_id,
                    RuntimeKnowledgeUsage.run_id == run.id,
                )
            )
        )
        existing_memories = {item.memory_id for item in existing if item.memory_id is not None}
        existing_skills = {
            item.skill_version_id for item in existing if item.skill_version_id is not None
        }
        common = {
            "policy_hash": selection_snapshot.get("policy_hash"),
            "query_hash": selection_snapshot.get("query_hash"),
        }
        for item in selection_snapshot.get("memory", []):
            memory_id = UUID(str(item["memory_id"]))
            if memory_id in existing_memories:
                continue
            session.add(
                RuntimeKnowledgeUsage(
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    runtime_session_id=runtime_session.id,
                    source_type="memory",
                    memory_id=memory_id,
                    source_version=int(item["version"]),
                    content_hash=str(item["content_hash"]),
                    scope_type=str(item["scope_type"]),
                    selection={
                        **common,
                        "memory_type": item.get("memory_type"),
                        "similarity": item.get("similarity"),
                        "confidence": item.get("confidence"),
                        "content_truncated": item.get("content_truncated", False),
                        "source_hashes": item.get("source_hashes", []),
                    },
                )
            )
        for item in selection_snapshot.get("skills", []):
            skill_version_id = UUID(str(item["skill_version_id"]))
            if skill_version_id in existing_skills:
                continue
            session.add(
                RuntimeKnowledgeUsage(
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    runtime_session_id=runtime_session.id,
                    source_type="skill_version",
                    skill_id=UUID(str(item["skill_id"])),
                    skill_version_id=skill_version_id,
                    source_version=int(item["version"]),
                    content_hash=str(item["content_hash"]),
                    scope_type=str(item["scope_type"]),
                    selection={
                        **common,
                        "name": item.get("name"),
                        "selection": item.get("selection"),
                        "deployment_id": item.get("deployment_id"),
                        "rollout_percentage": item.get("rollout_percentage"),
                        "bucket": item.get("bucket"),
                        "content_format": item.get("content_format"),
                    },
                )
            )
        await session.flush()

    @classmethod
    async def _finalize_knowledge_usages(
        cls,
        session: AsyncSession,
        run: Run,
        target: RunStatus,
        *,
        result: dict[str, Any] | None,
        error: dict[str, Any] | None,
        usage: dict[str, Any],
        ended_at: datetime,
    ) -> None:
        facts = list(
            await session.scalars(
                select(RuntimeKnowledgeUsage)
                .where(
                    RuntimeKnowledgeUsage.tenant_id == run.tenant_id,
                    RuntimeKnowledgeUsage.run_id == run.id,
                    RuntimeKnowledgeUsage.status.in_(["selected", "consumed"]),
                )
                .with_for_update()
            )
        )
        if not facts:
            return
        status = {
            RunStatus.COMPLETED: "succeeded",
            RunStatus.CANCELLED: "cancelled",
            RunStatus.TIMED_OUT: "timed_out",
        }.get(target, "failed")
        result_hash = cls._hash_json({"result": result, "error": error})
        for fact in facts:
            fact.status = status
            fact.outcome_status = target.value
            fact.result_hash = result_hash
            fact.effect_metadata = {
                "run_status": target.value,
                "consumed": fact.context_count > 0 and fact.model_call_count > 0,
                "context_count": fact.context_count,
                "model_call_count": fact.model_call_count,
                "usage": usage,
                "error_code": error.get("code") if isinstance(error, dict) else None,
            }
            fact.ended_at = ended_at
            fact.revision += 1

    @staticmethod
    def _provider_name(version: AgentVersion) -> str:
        return RuntimeExecutionService._provider_resolution(version).name

    @staticmethod
    def _provider_resolution(version: AgentVersion) -> RuntimeProviderResolution:
        return resolve_runtime_provider(
            runtime_provider=getattr(version, "runtime_provider", None),
            run_config=version.run_config,
            model_config=version.model_config_json,
        )

    @staticmethod
    def _descriptor_can_resume(
        runtime_session: RuntimeSession,
        descriptor: RuntimeProviderDescriptor,
    ) -> bool:
        provider_versions = descriptor.compatibility.get("resume_provider_versions", [])
        protocol_versions = descriptor.compatibility.get("resume_protocol_versions", [])
        return (
            descriptor.name == runtime_session.provider_name
            and runtime_session.provider_version in provider_versions
            and runtime_session.protocol_version in protocol_versions
        )

    @staticmethod
    def _execution_mode(version: AgentVersion) -> RuntimeExecutionMode:
        candidate = version.execution_mode or version.run_config.get("execution_mode", "direct")
        return RuntimeExecutionMode(candidate)

    @staticmethod
    def _execution_manifest(version: AgentVersion) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "agent_version_id": str(version.id),
            "runtime_provider": resolve_runtime_provider_name(
                runtime_provider=version.runtime_provider,
                run_config=version.run_config,
                model_config=version.model_config_json,
            ),
            "execution_mode": (
                version.execution_mode or version.run_config.get("execution_mode", "direct")
            ),
            "model_endpoint_id": (
                str(version.model_endpoint_id) if version.model_endpoint_id else None
            ),
            "model": version.model_name or version.model_config_json.get("model"),
        }

    @staticmethod
    def _model_endpoint_snapshot(
        endpoint: ModelEndpoint | None, version: AgentVersion
    ) -> dict[str, Any] | None:
        if endpoint is None:
            return None
        if not endpoint.enabled or endpoint.status != "active":
            raise ValueError("configured model endpoint is disabled")
        model = version.model_name or version.model_config_json.get("model")
        if endpoint.allowed_models and model not in endpoint.allowed_models:
            raise ValueError("configured model is not allowed by the endpoint revision")
        return {
            "id": str(endpoint.id),
            "stable_key": endpoint.stable_key,
            "revision": endpoint.revision,
            "protocol": endpoint.protocol,
            "base_url": endpoint.base_url,
            "credential_ref": endpoint.credential_ref,
            "allowed_models": endpoint.allowed_models,
            "capabilities": endpoint.capabilities,
            "rate_limit": endpoint.rate_limit,
            "tls_policy": endpoint.tls_policy,
            "model": model,
        }

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
    async def _step_started(
        session: AsyncSession,
        run: Run,
        runtime_session: RuntimeSession,
        event: RuntimeEvent,
    ) -> None:
        step_key = RuntimeExecutionService._optional_string(event.payload.get("step_key"), 200)
        sequence = int(event.payload.get("index", event.sequence))
        if step_key:
            existing_sequence = await session.scalar(
                select(RunStep.sequence).where(
                    RunStep.tenant_id == run.tenant_id,
                    RunStep.run_id == run.id,
                    RunStep.step_key == step_key,
                )
            )
            if existing_sequence is None:
                sequence = (
                    await session.scalar(
                        select(func.max(RunStep.sequence)).where(
                            RunStep.tenant_id == run.tenant_id,
                            RunStep.run_id == run.id,
                        )
                    )
                    or 0
                ) + 1
            else:
                sequence = existing_sequence
        step = await session.scalar(
            select(RunStep).where(
                RunStep.tenant_id == run.tenant_id,
                RunStep.run_id == run.id,
                RunStep.sequence == sequence,
            )
        )
        if step is None:
            step_type = RuntimeExecutionService._optional_string(event.payload.get("step_type"), 32)
            step = RunStep(
                tenant_id=run.tenant_id,
                run_id=run.id,
                sequence=sequence,
                step_key=step_key,
                step_type=step_type,
                iteration=RuntimeExecutionService._positive_int(event.payload.get("iteration")),
                # A reasoning step starts before its context/model facts are projected.
                # Bind those references when the step completes instead of retaining
                # the previous iteration's facts.
                context_snapshot_id=(
                    None
                    if step_type == "reasoning"
                    else runtime_session.current_context_snapshot_id
                ),
                model_call_id=(
                    None if step_type == "reasoning" else runtime_session.last_model_call_id
                ),
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
    async def _step_completed(
        session: AsyncSession,
        run: Run,
        runtime_session: RuntimeSession,
        event: RuntimeEvent,
    ) -> None:
        step_key = RuntimeExecutionService._optional_string(event.payload.get("step_key"), 200)
        sequence = int(event.payload.get("index", event.sequence))
        if step_key:
            existing_sequence = await session.scalar(
                select(RunStep.sequence).where(
                    RunStep.tenant_id == run.tenant_id,
                    RunStep.run_id == run.id,
                    RunStep.step_key == step_key,
                )
            )
            if existing_sequence is None:
                sequence = (
                    await session.scalar(
                        select(func.max(RunStep.sequence)).where(
                            RunStep.tenant_id == run.tenant_id,
                            RunStep.run_id == run.id,
                        )
                    )
                    or 0
                ) + 1
            else:
                sequence = existing_sequence
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
                step_key=step_key,
                step_type=RuntimeExecutionService._optional_string(
                    event.payload.get("step_type"), 32
                ),
                iteration=RuntimeExecutionService._positive_int(event.payload.get("iteration")),
                context_snapshot_id=runtime_session.current_context_snapshot_id,
                model_call_id=runtime_session.last_model_call_id,
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
            if step.step_type == "reasoning":
                step.context_snapshot_id = runtime_session.current_context_snapshot_id
                step.model_call_id = runtime_session.last_model_call_id
            else:
                step.context_snapshot_id = (
                    step.context_snapshot_id or runtime_session.current_context_snapshot_id
                )
                step.model_call_id = step.model_call_id or runtime_session.last_model_call_id
            step.ended_at = event.occurred_at
            step.revision += 1

    @classmethod
    async def _project_native_event(
        cls,
        session: AsyncSession,
        context: TenantContext,
        run: Run,
        runtime_session: RuntimeSession,
        event: RuntimeEvent,
    ) -> dict[str, Any]:
        payload = event.payload
        if event.type is RuntimeEventType.CONTEXT_SNAPSHOT_CREATED:
            rendered = payload.get("rendered_messages", [])
            sources = payload.get("source_refs", [])
            memory_refs = payload.get("memory_refs", [])
            skill_refs = payload.get("skill_refs", [])
            effect_metadata = payload.get("effect_metadata", {})
            await ProjectInterventionService.consume_context_in_session(
                session,
                context,
                run,
                effect_metadata,
            )
            conversation_context = (
                effect_metadata.get("conversation_context", {})
                if isinstance(effect_metadata, dict)
                else {}
            )
            if not isinstance(conversation_context, dict):
                conversation_context = {}
            native_truncation = payload.get("truncation", {})
            if not isinstance(native_truncation, dict):
                native_truncation = {}
            snapshot_truncation = (
                {
                    "runtime": native_truncation,
                    "conversation": conversation_context.get("truncation", {}),
                }
                if conversation_context
                else native_truncation
            )
            version = int(payload.get("version", 1))
            content_hash = str(payload.get("content_hash") or cls._hash_json(rendered))
            conversation_turn = await session.scalar(
                select(ConversationTurn).where(
                    ConversationTurn.tenant_id == run.tenant_id,
                    ConversationTurn.run_id == run.id,
                )
            )
            snapshot = await session.scalar(
                select(ContextSnapshot)
                .where(
                    ContextSnapshot.tenant_id == run.tenant_id,
                    ContextSnapshot.run_id == run.id,
                    ContextSnapshot.version == version,
                )
                .with_for_update()
            )
            if snapshot is not None:
                if snapshot.content_hash != content_hash:
                    raise ValueError("replayed context version has different content")
                if snapshot.memory_refs != memory_refs or snapshot.skill_refs != skill_refs:
                    raise ValueError("replayed context version has different knowledge refs")
                runtime_session.current_context_snapshot_id = snapshot.id
                runtime_session.loop_state = "reasoning"
                return {
                    "context_snapshot_id": str(snapshot.id),
                    "version": snapshot.version,
                    "content_hash": snapshot.content_hash,
                    "reason": snapshot.reason,
                    "token_estimate": snapshot.token_estimate,
                    "replayed": True,
                }
            snapshot = ContextSnapshot(
                tenant_id=run.tenant_id,
                run_id=run.id,
                runtime_session_id=runtime_session.id,
                parent_snapshot_id=runtime_session.current_context_snapshot_id,
                conversation_id=(
                    conversation_turn.conversation_id if conversation_turn is not None else None
                ),
                conversation_turn_id=(
                    conversation_turn.id if conversation_turn is not None else None
                ),
                schema_version=int(payload.get("schema_version", 1)),
                version=version,
                reason=str(payload.get("reason", "initial"))[:100],
                source_refs=sources if isinstance(sources, list) else [],
                memory_refs=memory_refs if isinstance(memory_refs, list) else [],
                skill_refs=skill_refs if isinstance(skill_refs, list) else [],
                effect_metadata=(effect_metadata if isinstance(effect_metadata, dict) else {}),
                rendered_messages=rendered if isinstance(rendered, list) else [],
                token_estimate=max(0, int(payload.get("token_estimate", 0))),
                truncation=snapshot_truncation,
                selected_turn_ids=(
                    conversation_context.get("selected_turn_ids", [])
                    if conversation_context
                    else ([str(conversation_turn.id)] if conversation_turn is not None else [])
                ),
                conversation_summary_hash=conversation_context.get("conversation_summary_hash"),
                artifact_refs=conversation_context.get("artifact_refs", []),
                token_budget=conversation_context.get("token_budget", run.token_budget),
                content_hash=content_hash,
            )
            session.add(snapshot)
            await session.flush()
            await cls._mark_knowledge_context(session, run, snapshot, event.occurred_at)
            runtime_session.current_context_snapshot_id = snapshot.id
            runtime_session.loop_state = "reasoning"
            return {
                "context_snapshot_id": str(snapshot.id),
                "version": snapshot.version,
                "content_hash": snapshot.content_hash,
                "reason": snapshot.reason,
                "token_estimate": snapshot.token_estimate,
            }
        if event.type is RuntimeEventType.MODEL_CALL_STARTED:
            if runtime_session.current_context_snapshot_id is None:
                raise ValueError("model call requires a persisted context snapshot")
            call_key = str(payload.get("call_key", ""))
            if not call_key:
                raise ValueError("model call requires call_key")
            request_redacted = payload.get("request_redacted", {})
            if not isinstance(request_redacted, dict):
                raise ValueError("model request projection must be an object")
            endpoint_id = payload.get("model_endpoint_id")
            replay_of_id = None
            replay_of_call_key = payload.get("replay_of_call_key")
            if replay_of_call_key:
                replay_of_id = await session.scalar(
                    select(ModelCall.id).where(
                        ModelCall.tenant_id == run.tenant_id,
                        ModelCall.run_id == run.id,
                        ModelCall.call_key == str(replay_of_call_key),
                    )
                )
                if replay_of_id is None:
                    raise ValueError("replayed model call has no base call")
            call = ModelCall(
                tenant_id=run.tenant_id,
                run_id=run.id,
                runtime_session_id=runtime_session.id,
                context_snapshot_id=runtime_session.current_context_snapshot_id,
                model_endpoint_id=UUID(str(endpoint_id)) if endpoint_id else None,
                replay_of_model_call_id=replay_of_id,
                call_key=call_key[:200],
                provider=str(payload.get("provider", "unknown"))[:100],
                model=str(payload.get("model", "unknown"))[:200],
                status="streaming",
                request_redacted=request_redacted,
                request_hash=str(payload.get("request_hash") or cls._hash_json(request_redacted)),
                started_at=event.occurred_at,
            )
            session.add(call)
            await session.flush()
            await cls._mark_knowledge_model_call(session, run, call)
            runtime_session.last_model_call_id = call.id
            return {
                "model_call_id": str(call.id),
                "context_snapshot_id": str(call.context_snapshot_id),
                "call_key": call.call_key,
                "provider": call.provider,
                "model": call.model,
                "status": call.status,
                "request_hash": call.request_hash,
            }
        if event.type in {
            RuntimeEventType.MODEL_CALL_COMPLETED,
            RuntimeEventType.MODEL_CALL_FAILED,
        }:
            call = await cls._model_call_for_event(session, run, payload)
            response = payload.get("response_redacted")
            if response is not None and not isinstance(response, dict):
                raise ValueError("model response projection must be an object")
            call.status = (
                "completed"
                if event.type is RuntimeEventType.MODEL_CALL_COMPLETED
                else str(payload.get("status", "failed"))
            )
            call.response_redacted = response
            call.response_hash = (
                str(payload.get("response_hash") or cls._hash_json(response))
                if response is not None
                else None
            )
            call.provider_request_id = cls._optional_string(payload.get("provider_request_id"), 300)
            usage = payload.get("usage", {})
            call.usage = usage if isinstance(usage, dict) else {}
            call.usage_status = str(payload.get("usage_status", "missing"))
            call.cost = payload.get("cost", {}) if isinstance(payload.get("cost", {}), dict) else {}
            call.cost_status = str(payload.get("cost_status", "unknown"))
            call.pricing_revision = cls._optional_string(payload.get("pricing_revision"), 100)
            call.error = payload.get("error") if isinstance(payload.get("error"), dict) else None
            call.ended_at = event.occurred_at
            runtime_session.loop_state = "finalizing" if call.status == "completed" else "failed"
            return {
                "model_call_id": str(call.id),
                "call_key": call.call_key,
                "status": call.status,
                "response_hash": call.response_hash,
                "usage": call.usage,
                "usage_status": call.usage_status,
                "cost": call.cost,
                "cost_status": call.cost_status,
                "provider_request_id": call.provider_request_id,
                "error": call.error,
            }
        if event.type is RuntimeEventType.MODEL_OUTPUT_DELTA:
            delta = payload.get("delta", "")
            return {
                "call_key": str(payload.get("call_key", ""))[:200],
                "delta": str(delta)[:4096],
            }
        if event.type is RuntimeEventType.TOOL_CALL_COMPLETED:
            run_step_id = payload.get("run_step_id")
            if run_step_id:
                step = await session.scalar(
                    select(RunStep)
                    .where(
                        RunStep.tenant_id == run.tenant_id,
                        RunStep.run_id == run.id,
                        RunStep.id == UUID(str(run_step_id)),
                    )
                    .with_for_update()
                )
                if step is None:
                    raise ValueError("tool completion has no matching RunStep")
                # ToolGateway creates the step before the native event projector has
                # necessarily persisted this iteration's context/model facts.
                step.context_snapshot_id = runtime_session.current_context_snapshot_id
                step.model_call_id = runtime_session.last_model_call_id
                iteration = cls._positive_int(payload.get("iteration"))
                if step.parent_step_id is None and iteration is not None:
                    step.parent_step_id = await session.scalar(
                        select(RunStep.id).where(
                            RunStep.tenant_id == run.tenant_id,
                            RunStep.run_id == run.id,
                            RunStep.step_key == f"reasoning:{iteration}",
                        )
                    )
                plan_revision = cls._positive_int(payload.get("plan_revision"))
                plan_step_key = cls._optional_string(payload.get("plan_step_key"), 64)
                attempt = cls._positive_int(payload.get("attempt"))
                if (
                    step.parent_step_id is None
                    and plan_revision is not None
                    and plan_step_key is not None
                    and attempt is not None
                ):
                    step.parent_step_id = await session.scalar(
                        select(RunStep.id).where(
                            RunStep.tenant_id == run.tenant_id,
                            RunStep.run_id == run.id,
                            RunStep.step_key
                            == f"plan:{plan_revision}:{plan_step_key}:attempt:{attempt}",
                        )
                    )
                step.revision += 1
            runtime_session.loop_state = "observing"
            return payload
        if event.type is RuntimeEventType.TOOL_CALL_STARTED:
            runtime_session.loop_state = "waiting_for_tool"
            return payload
        if event.type is RuntimeEventType.PLAN_CREATED:
            return await cls._project_plan_created(session, run, runtime_session, event)
        if event.type is RuntimeEventType.PLAN_STATUS_CHANGED:
            return await cls._project_plan_status(session, run, runtime_session, event)
        if event.type is RuntimeEventType.PLAN_STEP_STARTED:
            return await cls._project_plan_step_started(session, run, runtime_session, event)
        if event.type in {
            RuntimeEventType.PLAN_STEP_COMPLETED,
            RuntimeEventType.PLAN_STEP_FAILED,
        }:
            return await cls._project_plan_step_finished(session, run, runtime_session, event)
        if event.type is RuntimeEventType.REFLECTION_COMPLETED:
            runtime_session.loop_state = "reflecting"
            return payload
        if event.type is RuntimeEventType.EVALUATION_COMPLETED:
            return await cls._project_runtime_evaluation(session, run, event)
        return payload

    @staticmethod
    async def _mark_knowledge_context(
        session: AsyncSession,
        run: Run,
        snapshot: ContextSnapshot,
        occurred_at: datetime,
    ) -> None:
        facts = list(
            await session.scalars(
                select(RuntimeKnowledgeUsage)
                .where(
                    RuntimeKnowledgeUsage.tenant_id == run.tenant_id,
                    RuntimeKnowledgeUsage.run_id == run.id,
                )
                .with_for_update()
            )
        )
        by_memory = {str(item.memory_id): item for item in facts if item.memory_id is not None}
        by_skill = {
            str(item.skill_version_id): item for item in facts if item.skill_version_id is not None
        }
        referenced: list[RuntimeKnowledgeUsage] = []
        for reference in snapshot.memory_refs:
            if not isinstance(reference, dict):
                raise ValueError("context Memory reference must be an object")
            fact = by_memory.get(str(reference.get("memory_id")))
            if fact is None:
                raise ValueError("context references a Memory not selected for this Run")
            RuntimeExecutionService._require_knowledge_reference(fact, reference)
            referenced.append(fact)
        for reference in snapshot.skill_refs:
            if not isinstance(reference, dict):
                raise ValueError("context Skill reference must be an object")
            fact = by_skill.get(str(reference.get("skill_version_id")))
            if fact is None:
                raise ValueError("context references a SkillVersion not selected for this Run")
            RuntimeExecutionService._require_knowledge_reference(fact, reference)
            referenced.append(fact)
        for fact in referenced:
            if fact.status == "selected":
                fact.status = "consumed"
            fact.first_context_snapshot_id = fact.first_context_snapshot_id or snapshot.id
            fact.first_consumed_at = fact.first_consumed_at or occurred_at
            fact.context_count += 1
            fact.revision += 1

    @staticmethod
    async def _mark_knowledge_model_call(
        session: AsyncSession,
        run: Run,
        call: ModelCall,
    ) -> None:
        snapshot = await session.get(ContextSnapshot, call.context_snapshot_id)
        if snapshot is None:
            raise ValueError("model call context snapshot is unavailable")
        memory_ids = {
            UUID(str(item["memory_id"]))
            for item in snapshot.memory_refs
            if isinstance(item, dict) and item.get("memory_id")
        }
        skill_version_ids = {
            UUID(str(item["skill_version_id"]))
            for item in snapshot.skill_refs
            if isinstance(item, dict) and item.get("skill_version_id")
        }
        if not memory_ids and not skill_version_ids:
            return
        facts = list(
            await session.scalars(
                select(RuntimeKnowledgeUsage)
                .where(
                    RuntimeKnowledgeUsage.tenant_id == run.tenant_id,
                    RuntimeKnowledgeUsage.run_id == run.id,
                    (
                        RuntimeKnowledgeUsage.memory_id.in_(memory_ids)
                        | RuntimeKnowledgeUsage.skill_version_id.in_(skill_version_ids)
                    ),
                )
                .with_for_update()
            )
        )
        if len(facts) != len(memory_ids) + len(skill_version_ids):
            raise ValueError("model context knowledge usage facts are incomplete")
        for fact in facts:
            if fact.status == "selected":
                fact.status = "consumed"
            fact.first_model_call_id = fact.first_model_call_id or call.id
            fact.model_call_count += 1
            fact.revision += 1

    @staticmethod
    def _require_knowledge_reference(
        fact: RuntimeKnowledgeUsage,
        reference: dict[str, Any],
    ) -> None:
        if int(reference.get("version", 0)) != fact.source_version:
            raise ValueError("context knowledge version differs from frozen selection")
        if str(reference.get("content_hash", "")) != fact.content_hash:
            raise ValueError("context knowledge hash differs from frozen selection")

    @classmethod
    async def _project_plan_created(
        cls,
        session: AsyncSession,
        run: Run,
        runtime_session: RuntimeSession,
        event: RuntimeEvent,
    ) -> dict[str, Any]:
        payload = event.payload
        revision = cls._positive_int(payload.get("revision"))
        if revision is None:
            raise ValueError("Plan creation requires a positive revision")
        content_hash = str(payload.get("content_hash", ""))
        model_call = await cls._model_call_for_key(
            session, run, str(payload.get("model_call_key", ""))
        )
        existing = await session.scalar(
            select(Plan).where(
                Plan.tenant_id == run.tenant_id,
                Plan.run_id == run.id,
                Plan.revision == revision,
            )
        )
        if existing is not None:
            if existing.content_hash != content_hash:
                raise ValueError("replayed Plan revision has different content")
            return {
                "plan_id": str(existing.id),
                "revision": revision,
                "content_hash": content_hash,
                "replayed": True,
            }
        supersedes_revision = cls._positive_int(payload.get("supersedes_revision"))
        supersedes = None
        if supersedes_revision is not None:
            supersedes = await session.scalar(
                select(Plan).where(
                    Plan.tenant_id == run.tenant_id,
                    Plan.run_id == run.id,
                    Plan.revision == supersedes_revision,
                )
            )
            if supersedes is None:
                raise ValueError("replanned revision has no superseded Plan")
        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("Plan creation requires steps")
        plan = Plan(
            tenant_id=run.tenant_id,
            run_id=run.id,
            runtime_session_id=runtime_session.id,
            revision=revision,
            status="active",
            reason=str(payload.get("reason", "initial")),
            objective=str(payload.get("objective", "")),
            supersedes_plan_id=supersedes.id if supersedes is not None else None,
            created_by_model_call_id=model_call.id,
            content_hash=content_hash,
        )
        session.add(plan)
        await session.flush()
        for position, raw in enumerate(raw_steps, start=1):
            if not isinstance(raw, dict):
                raise ValueError("Plan step must be an object")
            session.add(
                PlanStep(
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    plan_id=plan.id,
                    step_key=str(raw.get("key", "")),
                    position=position,
                    title=str(raw.get("title", "")),
                    instruction=str(raw.get("instruction", "")),
                    acceptance=(
                        raw.get("acceptance", {})
                        if isinstance(raw.get("acceptance", {}), dict)
                        else {}
                    ),
                    dependencies=(
                        raw.get("depends_on", [])
                        if isinstance(raw.get("depends_on", []), list)
                        else []
                    ),
                )
            )
        runtime_session.loop_state = "planning"
        await session.flush()
        return {
            "plan_id": str(plan.id),
            "revision": plan.revision,
            "reason": plan.reason,
            "objective": plan.objective,
            "step_count": len(raw_steps),
            "content_hash": plan.content_hash,
            "supersedes_plan_id": str(plan.supersedes_plan_id) if plan.supersedes_plan_id else None,
        }

    @classmethod
    async def _project_plan_status(
        cls,
        session: AsyncSession,
        run: Run,
        runtime_session: RuntimeSession,
        event: RuntimeEvent,
    ) -> dict[str, Any]:
        plan = await cls._plan_for_revision(session, run, event.payload.get("revision"), True)
        status = str(event.payload.get("status", ""))
        if status not in {"superseded", "completed", "failed"}:
            raise ValueError("invalid Plan terminal status")
        if plan.status == status:
            return {"plan_id": str(plan.id), "revision": plan.revision, "status": plan.status}
        plan.status = status
        if status in {"superseded", "failed"}:
            steps = list(
                await session.scalars(
                    select(PlanStep)
                    .where(
                        PlanStep.tenant_id == run.tenant_id,
                        PlanStep.run_id == run.id,
                        PlanStep.plan_id == plan.id,
                        PlanStep.status.in_(["pending", "running"]),
                    )
                    .with_for_update()
                )
            )
            for step in steps:
                step.status = "failed"
                step.error = {
                    "code": "PLAN_SUPERSEDED" if status == "superseded" else "PLAN_FAILED"
                }
                step.ended_at = event.occurred_at
        runtime_session.loop_state = "completed" if status == "completed" else status
        return {"plan_id": str(plan.id), "revision": plan.revision, "status": plan.status}

    @classmethod
    async def _project_plan_step_started(
        cls,
        session: AsyncSession,
        run: Run,
        runtime_session: RuntimeSession,
        event: RuntimeEvent,
    ) -> dict[str, Any]:
        plan = await cls._plan_for_revision(session, run, event.payload.get("plan_revision"), False)
        step_key = str(event.payload.get("step_key", ""))
        step = await cls._plan_step_for_key(session, run, plan, step_key, True)
        attempt = cls._positive_int(event.payload.get("attempt")) or 1
        run_step_key = f"plan:{plan.revision}:{step_key}:attempt:{attempt}"
        run_step = await session.scalar(
            select(RunStep).where(
                RunStep.tenant_id == run.tenant_id,
                RunStep.run_id == run.id,
                RunStep.step_key == run_step_key,
            )
        )
        if run_step is None:
            sequence = (
                await session.scalar(
                    select(func.coalesce(func.max(RunStep.sequence), 0)).where(
                        RunStep.tenant_id == run.tenant_id,
                        RunStep.run_id == run.id,
                    )
                )
            ) + 1
            run_step = RunStep(
                tenant_id=run.tenant_id,
                run_id=run.id,
                sequence=sequence,
                step_key=run_step_key,
                step_type="reasoning",
                iteration=attempt,
                kind="plan.execute",
                status="running",
                input={"plan_revision": plan.revision, "plan_step_key": step_key},
                started_at=event.occurred_at,
            )
            session.add(run_step)
            await session.flush()
        step.status = "running"
        step.attempt = max(step.attempt, attempt)
        step.run_step_id = run_step.id
        step.started_at = step.started_at or event.occurred_at
        step.error = None
        runtime_session.loop_state = "reasoning"
        return {
            "plan_id": str(plan.id),
            "plan_step_id": str(step.id),
            "run_step_id": str(run_step.id),
            "revision": plan.revision,
            "step_key": step.step_key,
            "attempt": attempt,
        }

    @classmethod
    async def _project_plan_step_finished(
        cls,
        session: AsyncSession,
        run: Run,
        runtime_session: RuntimeSession,
        event: RuntimeEvent,
    ) -> dict[str, Any]:
        plan = await cls._plan_for_revision(session, run, event.payload.get("plan_revision"), False)
        step_key = str(event.payload.get("step_key", ""))
        step = await cls._plan_step_for_key(session, run, plan, step_key, True)
        attempt = cls._positive_int(event.payload.get("attempt")) or step.attempt or 1
        run_step_key = f"plan:{plan.revision}:{step_key}:attempt:{attempt}"
        run_step = await session.scalar(
            select(RunStep)
            .where(
                RunStep.tenant_id == run.tenant_id,
                RunStep.run_id == run.id,
                RunStep.step_key == run_step_key,
            )
            .with_for_update()
        )
        if run_step is None:
            raise ValueError("Plan step completion has no matching RunStep")
        model_call = await cls._model_call_for_key(
            session, run, str(event.payload.get("model_call_key", ""))
        )
        succeeded = event.type is RuntimeEventType.PLAN_STEP_COMPLETED
        run_step.status = "completed" if succeeded else "failed"
        run_step.output = event.payload.get("output") if succeeded else None
        run_step.error = event.payload.get("error") if not succeeded else None
        run_step.context_snapshot_id = runtime_session.current_context_snapshot_id
        run_step.model_call_id = model_call.id
        run_step.ended_at = event.occurred_at
        run_step.revision += 1
        step.status = "completed" if succeeded else "pending"
        step.output = event.payload.get("output") if succeeded else None
        step.output_hash = cls._optional_string(event.payload.get("output_hash"), 64)
        step.evidence_refs = (
            event.payload.get("evidence_refs", [])
            if isinstance(event.payload.get("evidence_refs", []), list)
            else []
        )
        step.error = event.payload.get("error") if not succeeded else None
        step.ended_at = event.occurred_at if succeeded else None
        runtime_session.loop_state = "reasoning" if succeeded else "reflecting"
        return {
            "plan_id": str(plan.id),
            "plan_step_id": str(step.id),
            "run_step_id": str(run_step.id),
            "revision": plan.revision,
            "step_key": step.step_key,
            "status": run_step.status,
            "output_hash": step.output_hash,
        }

    @classmethod
    async def _project_runtime_evaluation(
        cls, session: AsyncSession, run: Run, event: RuntimeEvent
    ) -> dict[str, Any]:
        payload = event.payload
        plan = await cls._plan_for_revision(session, run, payload.get("plan_revision"), False)
        plan_step = None
        step_key = payload.get("plan_step_key")
        if step_key:
            plan_step = await cls._plan_step_for_key(session, run, plan, str(step_key), False)
        model_call = None
        model_call_key = payload.get("model_call_key")
        if model_call_key:
            model_call = await cls._model_call_for_key(session, run, str(model_call_key))
        sequence = (
            await session.scalar(
                select(func.coalesce(func.max(RuntimeEvaluation.sequence), 0)).where(
                    RuntimeEvaluation.tenant_id == run.tenant_id,
                    RuntimeEvaluation.run_id == run.id,
                )
            )
        ) + 1
        verdict = str(payload.get("verdict", "failed"))
        if verdict == "fail":
            verdict = "failed"
        evaluation = RuntimeEvaluation(
            tenant_id=run.tenant_id,
            run_id=run.id,
            plan_id=plan.id,
            plan_step_id=plan_step.id if plan_step is not None else None,
            model_call_id=model_call.id if model_call is not None else None,
            sequence=sequence,
            evaluation_type=str(payload.get("evaluation_type", "step_validation")),
            method=str(payload.get("method", "deterministic")),
            status="completed",
            verdict=verdict,
            input_hash=str(payload.get("input_hash") or plan.content_hash),
            output_hash=cls._optional_string(payload.get("output_hash"), 64),
            evidence_refs=(
                payload.get("evidence_refs", [])
                if isinstance(payload.get("evidence_refs", []), list)
                else []
            ),
            result=payload.get("result", {}) if isinstance(payload.get("result", {}), dict) else {},
        )
        session.add(evaluation)
        await session.flush()
        return {
            "runtime_evaluation_id": str(evaluation.id),
            "sequence": evaluation.sequence,
            "evaluation_type": evaluation.evaluation_type,
            "method": evaluation.method,
            "verdict": evaluation.verdict,
            "plan_id": str(plan.id),
            "plan_step_id": str(plan_step.id) if plan_step is not None else None,
            "model_call_id": str(model_call.id) if model_call is not None else None,
            "output_hash": evaluation.output_hash,
        }

    @staticmethod
    async def _plan_for_revision(
        session: AsyncSession, run: Run, revision_value: Any, for_update: bool
    ) -> Plan:
        revision = RuntimeExecutionService._positive_int(revision_value)
        if revision is None:
            raise ValueError("Plan event requires a positive revision")
        query = select(Plan).where(
            Plan.tenant_id == run.tenant_id,
            Plan.run_id == run.id,
            Plan.revision == revision,
        )
        if for_update:
            query = query.with_for_update()
        plan = await session.scalar(query)
        if plan is None:
            raise ValueError("Plan event has no matching revision")
        return plan

    @staticmethod
    async def _plan_step_for_key(
        session: AsyncSession, run: Run, plan: Plan, step_key: str, for_update: bool
    ) -> PlanStep:
        query = select(PlanStep).where(
            PlanStep.tenant_id == run.tenant_id,
            PlanStep.run_id == run.id,
            PlanStep.plan_id == plan.id,
            PlanStep.step_key == step_key,
        )
        if for_update:
            query = query.with_for_update()
        step = await session.scalar(query)
        if step is None:
            raise ValueError("Plan event has no matching step")
        return step

    @staticmethod
    async def _model_call_for_key(session: AsyncSession, run: Run, call_key: str) -> ModelCall:
        if not call_key:
            raise ValueError("runtime fact requires model_call_key")
        call = await session.scalar(
            select(ModelCall).where(
                ModelCall.tenant_id == run.tenant_id,
                ModelCall.run_id == run.id,
                ModelCall.call_key == call_key,
            )
        )
        if call is None:
            raise ValueError("runtime fact has no matching ModelCall")
        return call

    @staticmethod
    async def _model_call_for_event(
        session: AsyncSession, run: Run, payload: dict[str, Any]
    ) -> ModelCall:
        call_key = str(payload.get("call_key", ""))
        call = await session.scalar(
            select(ModelCall)
            .where(
                ModelCall.tenant_id == run.tenant_id,
                ModelCall.run_id == run.id,
                ModelCall.call_key == call_key,
            )
            .with_for_update()
        )
        if call is None:
            raise ValueError("model call completion has no matching start")
        return call

    @staticmethod
    def _hash_json(value: Any) -> str:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _optional_string(value: Any, limit: int) -> str | None:
        return str(value)[:limit] if value is not None else None

    @staticmethod
    def _positive_int(value: Any) -> int | None:
        return value if type(value) is int and value > 0 else None

    @staticmethod
    def _recovered_usage(model_calls: list[ModelCall]) -> dict[str, Any]:
        completed = [call for call in model_calls if call.status == ModelCallStatus.COMPLETED.value]
        result: dict[str, Any] = {"model_calls": len(completed)}
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            values = [call.usage.get(key) for call in completed]
            exact = [value for value in values if isinstance(value, int)]
            if exact:
                result[key] = sum(exact)
        return result

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


def _usage_int(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
