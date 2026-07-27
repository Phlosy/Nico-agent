"""Lease-aware Tool Gateway with authorization, idempotency and audit persistence."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.database import Database, RunClaim, TenantContext
from nico_agent.domain.models import (
    AuditRecord,
    Event,
    Run,
    RunStep,
    RuntimeSession,
    RunToolBinding,
    Task,
    ToolApprovalRequest,
    ToolCall,
    ToolDefinition,
)
from nico_agent.domain.states import (
    ConversationApprovalMode,
    RunStatus,
    RunStepStatus,
    RunToolBindingStatus,
    ToolApprovalStatus,
    ToolCallStatus,
    ToolProviderCancelStatus,
    conversation_auto_approved_risks,
)
from nico_agent.runtime.contracts import RuntimeLoopState
from nico_agent.runtime.lifecycle import RunLifecycleAuthority
from nico_agent.tool_approvals.service import ToolApprovalService
from nico_agent.tool_providers.contracts import ToolBindingApprovalMode, canonical_digest
from nico_agent.tool_providers.errors import (
    ToolProviderError,
    ToolProviderErrorCode,
    provider_error,
)
from nico_agent.tool_providers.executor import ExternalToolExecutor, ExternalToolResolver
from nico_agent.tools.contracts import (
    ToolDefinitionSpec,
    ToolExecutionContext,
    ToolExecutor,
    canonical_hash,
    canonical_json,
    executor_required_secret_names,
)
from nico_agent.tools.errors import (
    ToolAccessDenied,
    ToolApprovalCheckpointRequired,
    ToolApprovalRequired,
    ToolCallInProgress,
    ToolDisabled,
    ToolError,
    ToolExecutorFailure,
    ToolIdempotencyConflict,
    ToolImplementationMismatch,
    ToolLeaseLost,
    ToolSchemaViolation,
)
from nico_agent.tools.policy import ToolAuthorization, authorize_tool
from nico_agent.tools.registry import ToolRegistry
from nico_agent.tools.secrets import (
    EnvironmentSecretResolver,
    SecretResolver,
    redact_value,
    resolve_secrets,
)

_ACTIVE_RUN_STATUSES = {
    RunStatus.PLANNING,
    RunStatus.RUNNING,
    RunStatus.WAITING_FOR_TOOL,
}
_TERMINAL_CALL_STATUSES = {
    ToolCallStatus.SUCCEEDED,
    ToolCallStatus.FAILED,
    ToolCallStatus.TIMED_OUT,
    ToolCallStatus.CANCELLED,
}
_MAX_INPUT_BYTES = 1_048_576


class ToolGatewayRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool_name: str = Field(min_length=1, max_length=120)
    tool_version: str = Field(min_length=1, max_length=50)
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=200)
    caller: str = Field(min_length=1, max_length=200)
    checkpoint: dict[str, Any] | None = None
    correlation_id: UUID = Field(default_factory=uuid4)


class ToolGatewayResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool_call_id: UUID
    run_step_id: UUID
    status: ToolCallStatus
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    attempts: int = Field(ge=0)
    cached: bool = False


@dataclass(frozen=True, slots=True)
class _PreparedCall:
    tool_call_id: UUID
    run_step_id: UUID
    executor: ToolExecutor
    authorization: ToolAuthorization
    context: ToolExecutionContext
    previous_attempts: tuple[dict[str, Any], ...]
    secrets: dict[str, str] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _ApprovalPending:
    approval_id: UUID
    tool_call_id: UUID
    run_step_id: UUID
    risk_level: str


class ToolGateway:
    def __init__(
        self,
        database: Database,
        registry: ToolRegistry,
        *,
        secret_resolver: SecretResolver | None = None,
        approval_required_risks: frozenset[str] = frozenset({"medium", "high"}),
        approval_ttl_seconds: int = 900,
        external_resolver: ExternalToolResolver | None = None,
    ) -> None:
        if not approval_required_risks <= {"medium", "high"}:
            raise ValueError("approval_required_risks may contain only medium and high")
        if approval_ttl_seconds < 1 or approval_ttl_seconds > 86_400:
            raise ValueError("approval_ttl_seconds must be between 1 and 86400")
        self.database = database
        self.registry = registry
        self.secret_resolver = secret_resolver or EnvironmentSecretResolver()
        self.approval_required_risks = approval_required_risks
        self.approval_ttl_seconds = approval_ttl_seconds
        self.external_resolver = external_resolver

    async def list_authorized(
        self,
        claim: RunClaim,
        *,
        worker_id: str,
    ) -> tuple[ToolDefinitionSpec, ...]:
        """Return only exact tool versions currently executable under the Run lease."""

        context = TenantContext(claim.tenant_id, f"worker:{worker_id}", uuid4())
        authorized: list[ToolDefinitionSpec] = []
        external_executors = (
            await self.external_resolver.list_for_run(claim, worker_id=worker_id)
            if self.external_resolver is not None
            else ()
        )
        externally_bound = {
            (executor.spec.name, executor.spec.version) for executor in external_executors
        }
        async with self.database.tenant_transaction(context) as session:
            run = await self._owned_run(session, claim, worker_id)
            runtime_session = await session.scalar(
                select(RuntimeSession).where(
                    RuntimeSession.tenant_id == claim.tenant_id,
                    RuntimeSession.run_id == claim.run_id,
                )
            )
            if runtime_session is None:
                return ()
            for executor in external_executors:
                try:
                    authorization = self._authorize_snapshot(
                        runtime_session.tool_policy_snapshot,
                        executor,
                    )
                    executor.client.secret_resolver.resolve(
                        "tool_provider",
                        executor.endpoint.credential_ref,
                    )
                    await self._authorization(
                        session,
                        claim,
                        executor.spec,
                        await self._definition(session, context, executor, run.id),
                        executor,
                    )
                    if authorization.secret_refs:
                        raise ToolAccessDenied(
                            executor.spec.name,
                            executor.spec.version,
                            "external Tool Providers cannot receive Nico Tool Secrets",
                        )
                except (ToolError, ToolProviderError):
                    continue
                authorized.append(executor.spec)
            for spec in self.registry.definitions():
                if (spec.name, spec.version) in externally_bound:
                    continue
                executor = self.registry.get(spec.name, spec.version)
                try:
                    self._authorize_snapshot(runtime_session.tool_policy_snapshot, executor)
                except ToolError:
                    continue
                definition = await self._definition(session, context, executor, run.id)
                try:
                    authorization = await self._authorization(
                        session,
                        claim,
                        spec,
                        definition,
                        executor,
                    )
                    resolve_secrets(self.secret_resolver, authorization.secret_refs)
                except ToolError:
                    continue
                authorized.append(spec)
        return tuple(authorized)

    async def execute(
        self,
        claim: RunClaim,
        *,
        worker_id: str,
        request: ToolGatewayRequest,
    ) -> ToolGatewayResult:
        executor = (
            await self.external_resolver.resolve(
                claim,
                worker_id=worker_id,
                tool_name=request.tool_name,
                tool_version=request.tool_version,
            )
            if self.external_resolver is not None
            else None
        )
        if executor is None:
            executor = self.registry.get(request.tool_name, request.tool_version)
        try:
            serialized_arguments = canonical_json(request.arguments)
        except (TypeError, ValueError) as exc:
            raise ToolSchemaViolation(
                code="TOOL_INPUT_INVALID",
                message="tool input must be finite JSON data",
                path=[],
                validator="json",
            ) from exc
        arguments_hash = canonical_hash(request.arguments)
        if len(serialized_arguments.encode()) > _MAX_INPUT_BYTES:
            raise ToolSchemaViolation(
                code="TOOL_INPUT_TOO_LARGE",
                message="tool input exceeds the platform byte limit",
                path=[],
                validator="max_input_bytes",
            )

        prepared_or_result = await self._begin_call(
            claim,
            worker_id=worker_id,
            request=request,
            executor=executor,
            arguments_hash=arguments_hash,
        )
        if isinstance(prepared_or_result, ToolGatewayResult):
            return prepared_or_result
        if isinstance(prepared_or_result, _ApprovalPending):
            raise ToolApprovalRequired(
                approval_id=prepared_or_result.approval_id,
                tool_call_id=prepared_or_result.tool_call_id,
                run_step_id=prepared_or_result.run_step_id,
                risk_level=prepared_or_result.risk_level,
            )
        prepared = prepared_or_result
        secrets = prepared.secrets
        attempts = list(prepared.previous_attempts)
        final_status = ToolCallStatus.FAILED
        final_output: dict[str, Any] | None = None
        final_error: dict[str, Any] | None = None
        final_usage: dict[str, Any] = {}
        max_attempts = int(
            getattr(
                prepared.executor,
                "effective_max_attempts",
                prepared.executor.spec.retry_policy.max_attempts,
            )
        )
        timeout_seconds = float(
            getattr(
                prepared.executor,
                "effective_timeout_seconds",
                prepared.executor.spec.timeout_seconds,
            )
        )

        try:
            for attempt_number in range(
                len(attempts) + 1,
                max_attempts + 1,
            ):
                started = time.monotonic()
                retry_after_seconds = prepared.executor.spec.retry_policy.backoff_seconds
                execution_context = prepared.context.model_copy(update={"attempt": attempt_number})
                try:
                    remaining_seconds = timeout_seconds
                    if execution_context.deadline is not None:
                        remaining_seconds = min(
                            remaining_seconds,
                            (execution_context.deadline - datetime.now(UTC)).total_seconds(),
                        )
                    if remaining_seconds <= 0:
                        raise TimeoutError
                    async with asyncio.timeout(remaining_seconds):
                        execution = await prepared.executor.execute(
                            execution_context,
                            request.arguments,
                            secrets,
                        )
                    prepared.executor.spec.validate_output(execution.output)
                    final_output = redact_value(execution.output, secrets)
                    final_usage = {
                        **final_usage,
                        **redact_value(execution.usage, secrets),
                    }
                    attempts.append(self._attempt(attempt_number, started, status="succeeded"))
                    final_status = ToolCallStatus.SUCCEEDED
                    break
                except TimeoutError:
                    code = (
                        "TOOL_PROVIDER_TIMEOUT"
                        if isinstance(prepared.executor, ExternalToolExecutor)
                        else "TOOL_TIMEOUT"
                    )
                    cancel_confirmed: bool | None = None
                    if isinstance(prepared.executor, ExternalToolExecutor):
                        cancel_confirmed = await self._cancel_external(
                            prepared.executor,
                            execution_context,
                            reason="tool_timed_out",
                        )
                        final_usage = self._cancellation_usage(
                            final_usage,
                            confirmed=cancel_confirmed,
                        )
                    attempts.append(
                        self._attempt(attempt_number, started, status="timed_out", code=code)
                    )
                    if cancel_confirmed is False or not self._should_retry(
                        prepared.executor.spec,
                        code,
                        attempt_number,
                        retryable=isinstance(prepared.executor, ExternalToolExecutor),
                        max_attempts=max_attempts,
                        allow_undeclared=isinstance(prepared.executor, ExternalToolExecutor),
                    ):
                        final_status = ToolCallStatus.TIMED_OUT
                        final_error = {
                            "code": code,
                            "message": "tool execution exceeded its timeout",
                        }
                        break
                    if not await self._checkpoint_attempts(
                        claim,
                        worker_id=worker_id,
                        prepared=prepared,
                        attempts=attempts,
                    ):
                        raise ToolLeaseLost(str(claim.run_id)) from None
                except ToolExecutorFailure as exc:
                    safe_message = str(redact_value(exc.message, secrets))[:1000]
                    attempts.append(
                        self._attempt(attempt_number, started, status="failed", code=exc.code)
                    )
                    if not self._should_retry(
                        prepared.executor.spec,
                        exc.code,
                        attempt_number,
                        retryable=exc.retryable,
                    ):
                        final_error = {"code": exc.code, "message": safe_message}
                        break
                    if exc.retry_after_seconds is not None:
                        retry_after_seconds = max(
                            retry_after_seconds,
                            min(
                                exc.retry_after_seconds,
                                timeout_seconds,
                            ),
                        )
                    if not await self._checkpoint_attempts(
                        claim,
                        worker_id=worker_id,
                        prepared=prepared,
                        attempts=attempts,
                    ):
                        raise ToolLeaseLost(str(claim.run_id)) from None
                except ToolProviderError as exc:
                    cancel_confirmed = None
                    if (
                        isinstance(prepared.executor, ExternalToolExecutor)
                        and exc.code == ToolProviderErrorCode.TIMEOUT.value
                    ):
                        cancel_confirmed = await self._cancel_external(
                            prepared.executor,
                            execution_context,
                            reason="tool_timed_out",
                        )
                        final_usage = self._cancellation_usage(
                            final_usage,
                            confirmed=cancel_confirmed,
                        )
                    attempts.append(
                        self._attempt(attempt_number, started, status="failed", code=exc.code)
                    )
                    if cancel_confirmed is not False and self._should_retry(
                        prepared.executor.spec,
                        exc.code,
                        attempt_number,
                        retryable=exc.retryable,
                        max_attempts=max_attempts,
                        allow_undeclared=True,
                    ):
                        if not await self._checkpoint_attempts(
                            claim,
                            worker_id=worker_id,
                            prepared=prepared,
                            attempts=attempts,
                        ):
                            raise ToolLeaseLost(str(claim.run_id)) from None
                    else:
                        final_error = {
                            "code": exc.code,
                            "message": str(redact_value(exc.message, secrets))[:1000],
                            "details": exc.context.model_dump(mode="json"),
                        }
                        break
                except ToolError as exc:
                    attempts.append(
                        self._attempt(attempt_number, started, status="failed", code=exc.code)
                    )
                    final_error = {
                        "code": exc.code,
                        "message": str(redact_value(exc.message, secrets))[:1000],
                    }
                    break
                except Exception:
                    attempts.append(
                        self._attempt(
                            attempt_number,
                            started,
                            status="failed",
                            code="TOOL_EXECUTOR_FAILED",
                        )
                    )
                    final_error = {
                        "code": "TOOL_EXECUTOR_FAILED",
                        "message": "tool executor raised an unexpected error",
                    }
                    break

                if (
                    isinstance(prepared.executor, ExternalToolExecutor)
                    and prepared.context.deadline is not None
                    and (prepared.context.deadline - datetime.now(UTC)).total_seconds()
                    <= retry_after_seconds
                ):
                    final_status = ToolCallStatus.TIMED_OUT
                    final_error = {
                        "code": ToolProviderErrorCode.TIMEOUT.value,
                        "message": "Tool Binding duration budget cannot accommodate a retry",
                    }
                    break
                if retry_after_seconds:
                    await asyncio.sleep(retry_after_seconds)
            if final_status is ToolCallStatus.FAILED and final_error is None:
                final_error = {
                    "code": "TOOL_RETRY_EXHAUSTED",
                    "message": "the tool retry budget was exhausted",
                }
        except asyncio.CancelledError:
            if isinstance(prepared.executor, ExternalToolExecutor):
                confirmed = await self._cancel_external(
                    prepared.executor,
                    prepared.context,
                    reason="run_cancelled",
                )
                final_usage = self._cancellation_usage(
                    final_usage,
                    confirmed=confirmed,
                )
            attempts.append(
                {
                    "number": len(attempts) + 1,
                    "status": "cancelled",
                    "duration_ms": 0,
                    "code": "TOOL_CANCELLED",
                }
            )
            await self._finish_call(
                claim,
                worker_id=worker_id,
                prepared=prepared,
                status=ToolCallStatus.CANCELLED,
                attempts=attempts,
                output=None,
                error={"code": "TOOL_CANCELLED", "message": "tool execution was cancelled"},
                usage=final_usage,
            )
            raise

        committed = await self._finish_call(
            claim,
            worker_id=worker_id,
            prepared=prepared,
            status=final_status,
            attempts=attempts,
            output=final_output,
            error=final_error,
            usage=final_usage,
        )
        if not committed:
            raise ToolLeaseLost(str(claim.run_id))
        return ToolGatewayResult(
            tool_call_id=prepared.tool_call_id,
            run_step_id=prepared.run_step_id,
            status=final_status,
            output=final_output,
            error=final_error,
            usage=final_usage,
            attempts=len(attempts),
        )

    async def _begin_call(
        self,
        claim: RunClaim,
        *,
        worker_id: str,
        request: ToolGatewayRequest,
        executor: ToolExecutor,
        arguments_hash: str,
    ) -> _PreparedCall | _ApprovalPending | ToolGatewayResult:
        context = TenantContext(claim.tenant_id, f"worker:{worker_id}", request.correlation_id)
        async with self.database.tenant_transaction(context) as session:
            run = await self._owned_run(session, claim, worker_id)
            if RunStatus(run.status) is RunStatus.PLANNING:
                await RunLifecycleAuthority.transition(
                    session,
                    context,
                    run,
                    target=RunStatus.RUNNING,
                    reason="tool_dispatch_started",
                    metadata={"tool": f"{request.tool_name}@{request.tool_version}"},
                    loop_state=RuntimeLoopState.REASONING,
                    lease_owner=worker_id,
                    lease_token=claim.lease_token,
                    event_type="RunStarted",
                    action="tool.lifecycle.start",
                    clear_lease=False,
                )
            runtime_projection = await session.scalar(
                select(RuntimeSession).where(
                    RuntimeSession.tenant_id == claim.tenant_id,
                    RuntimeSession.run_id == claim.run_id,
                )
            )
            if request.checkpoint is not None:
                await self._persist_pre_action_checkpoint(session, run, request.checkpoint)
            definition = await self._definition(session, context, executor, run.id)
            task = await session.scalar(
                select(Task).where(
                    Task.tenant_id == claim.tenant_id,
                    Task.id == run.task_id,
                )
            )
            if task is None:
                raise ToolLeaseLost(str(run.id))
            external_binding = (
                await session.scalar(
                    select(RunToolBinding)
                    .where(
                        RunToolBinding.tenant_id == claim.tenant_id,
                        RunToolBinding.run_id == run.id,
                        RunToolBinding.id == executor.provider_binding_id,
                        RunToolBinding.provider_id == executor.provider_id,
                        RunToolBinding.tool_definition_id == definition.id,
                    )
                    .with_for_update()
                )
                if isinstance(executor, ExternalToolExecutor)
                else None
            )
            if isinstance(executor, ExternalToolExecutor) and external_binding is None:
                raise ToolAccessDenied(
                    executor.spec.name,
                    executor.spec.version,
                    "frozen external Tool Binding is unavailable",
                )
            approval_mode = (
                executor.snapshot.policy.approval
                if isinstance(executor, ExternalToolExecutor)
                else None
            )
            approval_risk_level = (
                "medium"
                if approval_mode is ToolBindingApprovalMode.ALWAYS
                and executor.spec.risk.value == "low"
                else executor.spec.risk.value
            )
            existing = await session.scalar(
                select(ToolCall)
                .where(
                    ToolCall.tenant_id == claim.tenant_id,
                    ToolCall.run_id == claim.run_id,
                    ToolCall.tool_definition_id == definition.id,
                    ToolCall.idempotency_key == request.idempotency_key,
                )
                .with_for_update()
            )
            if existing is not None:
                if existing.arguments_hash != arguments_hash:
                    raise ToolIdempotencyConflict(request.idempotency_key)
                status = ToolCallStatus(existing.status)
                if status in _TERMINAL_CALL_STATUSES:
                    return self._result(existing, cached=True)
                approval = await session.scalar(
                    select(ToolApprovalRequest).where(
                        ToolApprovalRequest.tenant_id == claim.tenant_id,
                        ToolApprovalRequest.tool_call_id == existing.id,
                    )
                )
                if approval is not None and approval.status == ToolApprovalStatus.REQUESTED.value:
                    if RunStatus(run.status) is not RunStatus.WAITING_FOR_APPROVAL:
                        await RunLifecycleAuthority.transition(
                            session,
                            context,
                            run,
                            target=RunStatus.WAITING_FOR_APPROVAL,
                            reason="tool_approval_pending",
                            metadata={
                                "approval_id": str(approval.id),
                                "tool_call_id": str(existing.id),
                            },
                            loop_state=RuntimeLoopState.WAITING_FOR_APPROVAL,
                            lease_owner=worker_id,
                            lease_token=claim.lease_token,
                            event_type="RunWaitingForApproval",
                            action="tool.lifecycle.wait_approval",
                            clear_lease=False,
                        )
                    return _ApprovalPending(
                        approval_id=approval.id,
                        tool_call_id=existing.id,
                        run_step_id=existing.run_step_id,
                        risk_level=approval.risk_level,
                    )
                if (
                    (
                        executor.spec.risk.value in self.approval_required_risks
                        or approval_mode is ToolBindingApprovalMode.ALWAYS
                    )
                    and approval is None
                    and (
                        approval_mode is ToolBindingApprovalMode.ALWAYS
                        or not await self._run_scope_grant(session, claim, definition.id)
                    )
                ):
                    raise ToolAccessDenied(
                        executor.spec.name,
                        executor.spec.version,
                        "sensitive tool call has no durable approval",
                    )
                if existing.execution_lease_token == claim.lease_token:
                    raise ToolCallInProgress(str(existing.id))
                existing.execution_owner = worker_id
                existing.execution_lease_token = claim.lease_token
                existing.status = ToolCallStatus.RUNNING.value
                existing.started_at = existing.started_at or datetime.now(UTC)
                existing.revision += 1
                step = await session.scalar(
                    select(RunStep)
                    .where(
                        RunStep.tenant_id == claim.tenant_id,
                        RunStep.id == existing.run_step_id,
                    )
                    .with_for_update()
                )
                if step is None:
                    raise ToolLeaseLost(str(run.id))
                step.status = RunStepStatus.RUNNING.value
                step.ended_at = None
                step.revision += 1
                authorization = await self._authorization(
                    session,
                    claim,
                    executor.spec,
                    definition,
                    executor,
                )
                secrets = resolve_secrets(self.secret_resolver, authorization.secret_refs)
                if external_binding is not None and existing.provider_status in {None, "pending"}:
                    self._activate_external_binding(
                        external_binding,
                        existing,
                        run=run,
                        request=request,
                        arguments_hash=arguments_hash,
                        executor=executor,
                    )
                    self._record(
                        session,
                        context,
                        event_type="tool.provider.requested",
                        call=existing,
                        action="tool.provider.request",
                        payload=self._provider_event_payload(
                            existing,
                            external_binding,
                            task_id=task.id,
                            attempt=len(existing.attempts) + 1,
                            status="requested",
                        ),
                    )
                    self._record(
                        session,
                        context,
                        event_type="tool.provider.started",
                        call=existing,
                        action="tool.provider.start",
                        payload=self._provider_event_payload(
                            existing,
                            external_binding,
                            task_id=task.id,
                            attempt=len(existing.attempts) + 1,
                            status="running",
                        ),
                    )
                if RunStatus(run.status) is not RunStatus.WAITING_FOR_TOOL:
                    await RunLifecycleAuthority.transition(
                        session,
                        context,
                        run,
                        target=RunStatus.WAITING_FOR_TOOL,
                        reason="tool_execution_started",
                        metadata={"tool_call_id": str(existing.id)},
                        loop_state=RuntimeLoopState.WAITING_FOR_TOOL,
                        lease_owner=worker_id,
                        lease_token=claim.lease_token,
                        event_type="RunWaitingForTool",
                        action="tool.lifecycle.wait_tool",
                    )
                return self._prepared(
                    existing,
                    executor,
                    authorization,
                    request,
                    context,
                    secrets,
                    run,
                    task,
                )

            rejected: ToolError | None = None
            authorization: ToolAuthorization | None = None
            secrets: dict[str, str] = {}
            approval_required = False
            policy_auto_approval: dict[str, Any] | None = None
            run_scope_grant: ToolApprovalRequest | None = None
            try:
                authorization = await self._authorization(
                    session,
                    claim,
                    executor.spec,
                    definition,
                    executor,
                )
                executor.spec.validate_input(request.arguments)
                if (
                    executor.spec.risk.value in self.approval_required_risks
                    or approval_mode is ToolBindingApprovalMode.ALWAYS
                ):
                    if request.checkpoint is None:
                        raise ToolApprovalCheckpointRequired(
                            executor.spec.name, executor.spec.version
                        )
                    run_scope_grant = (
                        None
                        if approval_mode is ToolBindingApprovalMode.ALWAYS
                        else await self._run_scope_grant(session, claim, definition.id)
                    )
                    if run_scope_grant is None:
                        policy = self._tool_approval_policy(runtime_projection)
                        if approval_mode is ToolBindingApprovalMode.ALWAYS:
                            approval_required = True
                        elif self._policy_auto_approves(policy, approval_risk_level):
                            policy_auto_approval = policy
                        else:
                            approval_required = True
                if not approval_required:
                    secrets = resolve_secrets(self.secret_resolver, authorization.secret_refs)
            except ToolError as exc:
                rejected = exc

            sequence = (
                await session.scalar(
                    select(func.coalesce(func.max(RunStep.sequence), 0)).where(
                        RunStep.tenant_id == claim.tenant_id,
                        RunStep.run_id == claim.run_id,
                    )
                )
            ) + 1
            checkpoint_iteration = (
                int(request.checkpoint["iteration"])
                if request.checkpoint is not None
                and type(request.checkpoint.get("iteration")) is int
                and request.checkpoint["iteration"] > 0
                else None
            )
            plan_step_state = (
                request.checkpoint.get("step_state")
                if request.checkpoint is not None
                and request.checkpoint.get("execution_mode") == "plan_and_execute"
                and isinstance(request.checkpoint.get("step_state"), dict)
                else None
            )
            plan_revision = (
                request.checkpoint.get("plan_revision")
                if request.checkpoint is not None
                and type(request.checkpoint.get("plan_revision")) is int
                else None
            )
            plan_step_key = plan_step_state.get("step_key") if plan_step_state is not None else None
            plan_attempt = (
                plan_step_state.get("attempt")
                if plan_step_state is not None and type(plan_step_state.get("attempt")) is int
                else None
            )
            tool_step_key = (
                f"tool:{request.idempotency_key}"
                if len(request.idempotency_key) <= 195
                else f"tool:{canonical_hash(request.idempotency_key)}"
            )
            parent_step_id = None
            if checkpoint_iteration is not None:
                parent_step_id = await session.scalar(
                    select(RunStep.id).where(
                        RunStep.tenant_id == claim.tenant_id,
                        RunStep.run_id == claim.run_id,
                        RunStep.step_key == f"reasoning:{checkpoint_iteration}",
                    )
                )
            elif (
                plan_revision is not None
                and isinstance(plan_step_key, str)
                and plan_step_key
                and plan_attempt is not None
            ):
                parent_step_id = await session.scalar(
                    select(RunStep.id).where(
                        RunStep.tenant_id == claim.tenant_id,
                        RunStep.run_id == claim.run_id,
                        RunStep.step_key
                        == f"plan:{plan_revision}:{plan_step_key}:attempt:{plan_attempt}",
                    )
                )
            now = datetime.now(UTC)
            step = RunStep(
                tenant_id=claim.tenant_id,
                run_id=claim.run_id,
                sequence=sequence,
                step_key=tool_step_key,
                step_type="tool",
                iteration=checkpoint_iteration or plan_attempt,
                parent_step_id=parent_step_id,
                context_snapshot_id=(
                    runtime_projection.current_context_snapshot_id
                    if runtime_projection is not None
                    else None
                ),
                model_call_id=(
                    runtime_projection.last_model_call_id
                    if runtime_projection is not None
                    else None
                ),
                kind=f"tool:{executor.spec.name}"[:100],
                status=(
                    RunStepStatus.FAILED.value
                    if rejected is not None
                    else (
                        RunStepStatus.WAITING.value
                        if approval_required
                        else RunStepStatus.RUNNING.value
                    )
                ),
                input={
                    "tool": executor.spec.reference,
                    "arguments": redact_value(request.arguments),
                },
                error=(
                    {"code": rejected.code, "message": rejected.message}
                    if rejected is not None
                    else None
                ),
                started_at=now,
                ended_at=now if rejected is not None else None,
            )
            session.add(step)
            await session.flush()
            call_id = uuid4()
            provider_request_id = f"provider-{call_id}" if external_binding is not None else None
            provider_deadline = (
                self._external_deadline(run, external_binding, executor, now)
                if isinstance(executor, ExternalToolExecutor)
                else None
            )
            call = ToolCall(
                id=call_id,
                tenant_id=claim.tenant_id,
                run_id=claim.run_id,
                run_step_id=step.id,
                tool_definition_id=definition.id,
                run_tool_binding_id=(external_binding.id if external_binding is not None else None),
                provider_id=(
                    external_binding.provider_id if external_binding is not None else None
                ),
                tool_name=executor.spec.name,
                tool_version=executor.spec.version,
                idempotency_key=request.idempotency_key,
                arguments_hash=arguments_hash,
                caller=request.caller,
                execution_owner=worker_id,
                execution_lease_token=claim.lease_token,
                arguments=redact_value(request.arguments),
                provider_request_id=provider_request_id,
                provider_request_digest=(
                    canonical_digest(
                        {
                            "binding_digest": external_binding.binding_digest,
                            "idempotency_key": request.idempotency_key,
                            "arguments_hash": arguments_hash,
                            "run_id": str(run.id),
                            "tool_call_id": str(call_id),
                        }
                    )
                    if external_binding is not None
                    else None
                ),
                provider_deadline_at=provider_deadline,
                provider_trace_id=(
                    str(request.correlation_id) if external_binding is not None else None
                ),
                provider_status=("pending" if external_binding is not None else None),
                status=(
                    ToolCallStatus.FAILED.value
                    if rejected is not None
                    else (
                        ToolCallStatus.PENDING.value
                        if approval_required
                        else ToolCallStatus.RUNNING.value
                    )
                ),
                error=(
                    {"code": rejected.code, "message": rejected.message}
                    if rejected is not None
                    else None
                ),
                started_at=None if approval_required else now,
                ended_at=now if rejected is not None else None,
            )
            session.add(call)
            if external_binding is not None and rejected is None and not approval_required:
                self._activate_external_binding(
                    external_binding,
                    call,
                    run=run,
                    request=request,
                    arguments_hash=arguments_hash,
                    executor=executor,
                )
            await session.flush()
            self._record(
                session,
                context,
                event_type=(
                    "ToolCallRejected"
                    if rejected is not None
                    else ("ToolCallPendingApproval" if approval_required else "ToolCallStarted")
                ),
                call=call,
                action=(
                    "tool.call.reject"
                    if rejected is not None
                    else ("tool.call.await_approval" if approval_required else "tool.call.start")
                ),
                payload={
                    "tool": executor.spec.reference,
                    "status": call.status,
                    "code": rejected.code if rejected is not None else None,
                    "run_step_id": str(step.id),
                    "provider_id": str(call.provider_id) if call.provider_id else None,
                    "binding_digest": (
                        external_binding.binding_digest if external_binding is not None else None
                    ),
                    "request_id": call.provider_request_id,
                },
            )
            if external_binding is not None and rejected is None and not approval_required:
                self._record(
                    session,
                    context,
                    event_type="tool.provider.requested",
                    call=call,
                    action="tool.provider.request",
                    payload=self._provider_event_payload(
                        call,
                        external_binding,
                        task_id=task.id,
                        attempt=1,
                        status="requested",
                    ),
                )
                self._record(
                    session,
                    context,
                    event_type="tool.provider.started",
                    call=call,
                    action="tool.provider.start",
                    payload=self._provider_event_payload(
                        call,
                        external_binding,
                        task_id=task.id,
                        attempt=1,
                        status="running",
                    ),
                )
            if rejected is not None:
                return self._result(call)
            assert authorization is not None
            if approval_required:
                approval = ToolApprovalRequest(
                    tenant_id=claim.tenant_id,
                    run_id=claim.run_id,
                    run_step_id=step.id,
                    tool_call_id=call.id,
                    tool_definition_id=definition.id,
                    risk_level=approval_risk_level,
                    requester=request.caller,
                    arguments_redacted=redact_value(request.arguments),
                    arguments_hash=arguments_hash,
                    expires_at=now + timedelta(seconds=self.approval_ttl_seconds),
                )
                session.add(approval)
                await session.flush()
                await RunLifecycleAuthority.transition(
                    session,
                    context,
                    run,
                    target=RunStatus.WAITING_FOR_APPROVAL,
                    reason="tool_approval_requested",
                    metadata={
                        "approval_id": str(approval.id),
                        "tool_call_id": str(call.id),
                        "risk_level": approval.risk_level,
                    },
                    loop_state=RuntimeLoopState.WAITING_FOR_APPROVAL,
                    lease_owner=worker_id,
                    lease_token=claim.lease_token,
                    event_type="RunWaitingForApproval",
                    action="tool.lifecycle.wait_approval",
                    clear_lease=False,
                )
                if runtime_projection is not None:
                    runtime_projection.loop_state = "waiting_for_approval"
                    runtime_projection.revision += 1
                self._record_approval_requested(session, context, approval, call)
                return _ApprovalPending(
                    approval_id=approval.id,
                    tool_call_id=call.id,
                    run_step_id=step.id,
                    risk_level=approval.risk_level,
                )
            if policy_auto_approval is not None:
                await ToolApprovalService.create_policy_approval(
                    session,
                    context,
                    call=call,
                    risk_level=approval_risk_level,
                    requester=request.caller,
                    arguments_redacted=redact_value(request.arguments),
                    arguments_hash=arguments_hash,
                    ttl_seconds=self.approval_ttl_seconds,
                    policy=policy_auto_approval,
                )
            if run_scope_grant is not None:
                self._record_approval_grant_reused(session, context, run_scope_grant, call)
            await RunLifecycleAuthority.transition(
                session,
                context,
                run,
                target=RunStatus.WAITING_FOR_TOOL,
                reason="tool_execution_started",
                metadata={"tool_call_id": str(call.id)},
                loop_state=RuntimeLoopState.WAITING_FOR_TOOL,
                lease_owner=worker_id,
                lease_token=claim.lease_token,
                event_type="RunWaitingForTool",
                action="tool.lifecycle.wait_tool",
            )
            return self._prepared(
                call,
                executor,
                authorization,
                request,
                context,
                secrets,
                run,
                task,
            )

    @staticmethod
    def _tool_approval_policy(runtime_session: RuntimeSession | None) -> dict[str, Any]:
        if runtime_session is None:
            return {
                "mode": "ask",
                "source": "deployment_default",
                "conversation_id": None,
            }
        candidate = runtime_session.execution_manifest.get("tool_approval_policy")
        if not isinstance(candidate, dict):
            return {
                "mode": "ask",
                "source": "deployment_default",
                "conversation_id": None,
            }
        try:
            mode = ConversationApprovalMode(candidate.get("mode"))
        except (TypeError, ValueError):
            return {
                "mode": "ask",
                "source": "deployment_default",
                "conversation_id": None,
            }
        source = candidate.get("source")
        return {
            "mode": mode.value,
            "source": source
            if source in {"conversation", "deployment_default"}
            else "deployment_default",
            "conversation_id": candidate.get("conversation_id"),
        }

    @staticmethod
    def _policy_auto_approves(policy: dict[str, Any], risk_level: str) -> bool:
        mode = ConversationApprovalMode(policy["mode"])
        return risk_level in conversation_auto_approved_risks(mode)

    @staticmethod
    async def _run_scope_grant(
        session: AsyncSession,
        claim: RunClaim,
        tool_definition_id: UUID,
    ) -> ToolApprovalRequest | None:
        return await session.scalar(
            select(ToolApprovalRequest)
            .where(
                ToolApprovalRequest.tenant_id == claim.tenant_id,
                ToolApprovalRequest.run_id == claim.run_id,
                ToolApprovalRequest.tool_definition_id == tool_definition_id,
                ToolApprovalRequest.status == ToolApprovalStatus.APPROVED.value,
                ToolApprovalRequest.allowed_scope == "run",
            )
            .order_by(ToolApprovalRequest.decided_at.desc())
            .limit(1)
        )

    @staticmethod
    async def _persist_pre_action_checkpoint(
        session: AsyncSession,
        run: Run,
        checkpoint: dict[str, Any],
    ) -> None:
        execution_mode = checkpoint.get("execution_mode")
        schema_version = checkpoint.get("schema_version")
        step_state = checkpoint.get("step_state")
        valid_react_boundary = (
            execution_mode == "react"
            and schema_version == 2
            and checkpoint.get("loop_state") == "waiting_for_tool"
            and bool(checkpoint.get("pending_actions"))
        )
        valid_plan_boundary = (
            execution_mode == "plan_and_execute"
            and schema_version == 3
            and checkpoint.get("loop_state") == "executing"
            and isinstance(step_state, dict)
            and bool(step_state.get("pending_actions"))
        )
        valid_setup_proof_boundary = (
            execution_mode == "setup_proof"
            and schema_version == 1
            and run.budgets.get("setup_proof") is True
            and (
                (
                    checkpoint.get("loop_state") == "searching"
                    and checkpoint.get("tool_calls_consumed") == 0
                    and checkpoint.get("completed_action_keys") == []
                    and checkpoint.get("search_tool_call_id") is None
                    and checkpoint.get("source_url") is None
                    and checkpoint.get("final_url") is None
                )
                or (
                    checkpoint.get("loop_state") == "fetching"
                    and checkpoint.get("tool_calls_consumed") == 1
                    and checkpoint.get("completed_action_keys") == ["setup-proof-search"]
                    and isinstance(checkpoint.get("search_tool_call_id"), str)
                    and isinstance(checkpoint.get("source_url"), str)
                    and checkpoint.get("final_url") is None
                )
            )
        )
        if not (valid_react_boundary or valid_plan_boundary or valid_setup_proof_boundary):
            raise ToolSchemaViolation(
                code="TOOL_CHECKPOINT_INVALID",
                message="tool execution requires a valid native pre-action checkpoint",
                path=[],
                validator="checkpoint",
            )
        expected_hash = checkpoint.get("checkpoint_hash")
        hash_payload = dict(checkpoint)
        hash_payload.pop("checkpoint_hash", None)
        if not isinstance(expected_hash, str) or canonical_hash(hash_payload) != expected_hash:
            raise ToolSchemaViolation(
                code="TOOL_CHECKPOINT_INVALID",
                message="tool execution checkpoint failed its integrity check",
                path=[],
                validator="checkpoint_hash",
            )
        # The RuntimeSession and Run are already protected by the owned Run row
        # lock. This write commits in the same transaction that creates or
        # reclaims the ToolCall, before the executor can produce a side effect.
        runtime_session = await session.scalar(
            select(RuntimeSession).where(
                RuntimeSession.tenant_id == run.tenant_id,
                RuntimeSession.run_id == run.id,
            )
        )
        if runtime_session is None:
            raise ToolAccessDenied("runtime", "checkpoint", "runtime session is unavailable")
        runtime_session.checkpoint = checkpoint
        runtime_session.checkpoint_schema_version = schema_version
        runtime_session.checkpoint_revision += 1
        runtime_session.checkpoint_hash = expected_hash
        runtime_session.loop_state = "waiting_for_tool"
        runtime_session.revision += 1
        run.checkpoint = checkpoint
        run.checkpoint_schema_version = schema_version
        run.checkpoint_revision += 1
        run.checkpoint_hash = expected_hash
        run.revision += 1

    async def _checkpoint_attempts(
        self,
        claim: RunClaim,
        *,
        worker_id: str,
        prepared: _PreparedCall,
        attempts: list[dict[str, Any]],
    ) -> bool:
        context = TenantContext(
            claim.tenant_id,
            f"worker:{worker_id}",
            prepared.context.correlation_id,
        )
        async with self.database.tenant_transaction(context) as session:
            run = await session.scalar(
                select(Run)
                .where(Run.tenant_id == claim.tenant_id, Run.id == claim.run_id)
                .with_for_update()
            )
            if not self._owns(run, claim, worker_id):
                return False
            call = await session.scalar(
                select(ToolCall)
                .where(
                    ToolCall.tenant_id == claim.tenant_id,
                    ToolCall.id == prepared.tool_call_id,
                )
                .with_for_update()
            )
            if (
                call is None
                or ToolCallStatus(call.status) is not ToolCallStatus.RUNNING
                or call.execution_lease_token != claim.lease_token
            ):
                return False
            call.attempts = attempts
            call.revision += 1
            return True

    async def _finish_call(
        self,
        claim: RunClaim,
        *,
        worker_id: str,
        prepared: _PreparedCall,
        status: ToolCallStatus,
        attempts: list[dict[str, Any]],
        output: dict[str, Any] | None,
        error: dict[str, Any] | None,
        usage: dict[str, Any],
    ) -> bool:
        context = TenantContext(
            claim.tenant_id, f"worker:{worker_id}", prepared.context.correlation_id
        )
        async with self.database.tenant_transaction(context) as session:
            run = await session.scalar(
                select(Run)
                .where(Run.tenant_id == claim.tenant_id, Run.id == claim.run_id)
                .with_for_update()
            )
            if not self._owns(run, claim, worker_id):
                return False
            call = await session.scalar(
                select(ToolCall)
                .where(ToolCall.tenant_id == claim.tenant_id, ToolCall.id == prepared.tool_call_id)
                .with_for_update()
            )
            if (
                call is None
                or ToolCallStatus(call.status) is not ToolCallStatus.RUNNING
                or call.execution_lease_token != claim.lease_token
            ):
                return False
            step = await session.scalar(
                select(RunStep)
                .where(RunStep.tenant_id == claim.tenant_id, RunStep.id == prepared.run_step_id)
                .with_for_update()
            )
            if step is None:
                return False
            now = datetime.now(UTC)
            binding = (
                await session.scalar(
                    select(RunToolBinding)
                    .where(
                        RunToolBinding.tenant_id == claim.tenant_id,
                        RunToolBinding.id == call.run_tool_binding_id,
                    )
                    .with_for_update()
                )
                if call.run_tool_binding_id is not None
                else None
            )
            call.status = status.value
            call.attempts = attempts
            call.result = output
            call.error = error
            call.usage = usage
            call.ended_at = now
            if binding is not None:
                duration_ms = round(
                    sum(
                        float(item.get("duration_ms", 0))
                        for item in attempts
                        if isinstance(item, dict)
                    )
                )
                remaining_duration_ms = max(
                    binding.max_total_duration_ms - binding.total_duration_ms,
                    0,
                )
                binding.total_duration_ms += min(
                    max(duration_ms, 0),
                    remaining_duration_ms,
                )
                binding.active_provider_request_id = None
                cancel_requested = usage.get("provider_cancel_requested") is True
                cancel_confirmed = usage.get("provider_cancel_confirmed") is True
                if cancel_requested:
                    binding.cancel_status = (
                        ToolProviderCancelStatus.CANCELLED.value
                        if cancel_confirmed
                        else ToolProviderCancelStatus.UNKNOWN.value
                    )
                binding.revision += 1
                call.provider_status = (
                    ToolProviderCancelStatus.UNKNOWN.value
                    if cancel_requested and not cancel_confirmed
                    else (
                        ToolCallStatus.CANCELLED.value
                        if cancel_requested and status is ToolCallStatus.CANCELLED
                        else status.value
                    )
                )
                execution_id = usage.get("provider_execution_id")
                if isinstance(execution_id, str):
                    call.provider_execution_id = execution_id
                call.external_execution_may_continue = bool(
                    usage.get("external_execution_may_continue", False)
                )
            call.revision += 1
            step.status = {
                ToolCallStatus.SUCCEEDED: RunStepStatus.COMPLETED,
                ToolCallStatus.CANCELLED: RunStepStatus.CANCELLED,
            }.get(status, RunStepStatus.FAILED).value
            step.output = output
            step.error = error
            step.ended_at = now
            step.revision += 1
            await RunLifecycleAuthority.transition(
                session,
                context,
                run,
                target=RunStatus.RUNNING,
                reason="tool_execution_finished",
                metadata={
                    "tool_call_id": str(call.id),
                    "tool_status": status.value,
                    "error_code": error.get("code") if error else None,
                },
                lease_owner=worker_id,
                lease_token=claim.lease_token,
                event_type="RunWoken",
                action="tool.lifecycle.wake",
                clear_lease=False,
            )
            self._record(
                session,
                context,
                event_type={
                    ToolCallStatus.SUCCEEDED: "ToolCallSucceeded",
                    ToolCallStatus.TIMED_OUT: "ToolCallTimedOut",
                    ToolCallStatus.CANCELLED: "ToolCallCancelled",
                }.get(status, "ToolCallFailed"),
                call=call,
                action="tool.call.finish",
                payload={
                    "tool": f"{call.tool_name}@{call.tool_version}",
                    "status": call.status,
                    "attempts": len(attempts),
                    "run_step_id": str(step.id),
                    "code": error.get("code") if error else None,
                },
            )
            if binding is not None:
                error_code = error.get("code") if error else None
                cancel_requested = usage.get("provider_cancel_requested") is True
                cancel_confirmed = usage.get("provider_cancel_confirmed") is True
                if cancel_requested:
                    self._record(
                        session,
                        context,
                        event_type="tool.provider.cancel.requested",
                        call=call,
                        action="tool.provider.cancel.request",
                        payload=self._provider_event_payload(
                            call,
                            binding,
                            task_id=run.task_id,
                            attempt=len(attempts),
                            status="cancel_requested",
                        ),
                    )
                    cancellation_event = (
                        "tool.provider.cancelled" if cancel_confirmed else "tool.provider.failed"
                    )
                    self._record(
                        session,
                        context,
                        event_type=cancellation_event,
                        call=call,
                        action=(
                            "tool.provider.cancel"
                            if cancel_confirmed
                            else "tool.provider.cancel.failed"
                        ),
                        payload=self._provider_event_payload(
                            call,
                            binding,
                            task_id=run.task_id,
                            attempt=len(attempts),
                            status="cancelled" if cancel_confirmed else "unknown",
                            duration_ms=duration_ms,
                            error_code=(
                                None
                                if cancel_confirmed
                                else ToolProviderErrorCode.CANCEL_ERROR.value
                            ),
                        ),
                    )
                if not cancel_requested or (
                    cancel_confirmed and status is not ToolCallStatus.CANCELLED
                ):
                    provider_event = self._provider_terminal_event(status, error_code)
                    self._record(
                        session,
                        context,
                        event_type=provider_event,
                        call=call,
                        action=provider_event,
                        payload=self._provider_event_payload(
                            call,
                            binding,
                            task_id=run.task_id,
                            attempt=len(attempts),
                            status=status.value,
                            duration_ms=duration_ms,
                            error_code=error_code,
                        ),
                    )
            await session.flush()
            return True

    @staticmethod
    def _provider_terminal_event(
        status: ToolCallStatus,
        error_code: str | None,
    ) -> str:
        if status is ToolCallStatus.SUCCEEDED:
            return "tool.provider.completed"
        if status is ToolCallStatus.CANCELLED:
            return "tool.provider.cancelled"
        if error_code in {
            ToolProviderErrorCode.PROTOCOL_ERROR.value,
            ToolProviderErrorCode.INVALID_RESPONSE.value,
            ToolProviderErrorCode.SCHEMA_MISMATCH.value,
            "TOOL_OUTPUT_INVALID",
        }:
            return "tool.provider.protocol_error"
        return "tool.provider.failed"

    async def _definition(
        self,
        session: AsyncSession,
        context: TenantContext,
        executor: ToolExecutor,
        run_id: UUID,
    ) -> ToolDefinition:
        spec = executor.spec
        definition = await session.scalar(
            select(ToolDefinition)
            .where(
                ToolDefinition.tenant_id == context.tenant_id,
                ToolDefinition.name == spec.name,
                ToolDefinition.version == spec.version,
            )
            .with_for_update()
        )
        if definition is None:
            now = datetime.now(UTC)
            definition = ToolDefinition(
                tenant_id=context.tenant_id,
                name=spec.name,
                version=spec.version,
                status="enabled",
                description=spec.description,
                input_schema=spec.input_schema,
                output_schema=spec.output_schema,
                permission=spec.permission,
                timeout_seconds=spec.timeout_seconds,
                retry_policy=spec.retry_policy.model_dump(mode="json"),
                isolation_policy={
                    "kind": spec.isolation.value,
                    "secret_names": sorted(spec.secret_names),
                },
                risk=spec.risk.value,
                max_output_bytes=spec.max_output_bytes,
                implementation_hash=executor.implementation_hash,
                content_hash=spec.content_hash,
                created_by=context.actor_id,
                enabled_at=now,
            )
            session.add(definition)
            await session.flush()
            self._record_definition(session, context, definition, run_id)
        return definition

    async def _authorization(
        self,
        session: AsyncSession,
        claim: RunClaim,
        spec: ToolDefinitionSpec,
        definition: ToolDefinition,
        executor: ToolExecutor,
    ) -> ToolAuthorization:
        if definition.status != "enabled":
            raise ToolDisabled(spec.name, spec.version)
        if (
            definition.content_hash != spec.content_hash
            or definition.implementation_hash != executor.implementation_hash
        ):
            raise ToolImplementationMismatch(spec.name, spec.version)
        runtime_session = await session.scalar(
            select(RuntimeSession).where(
                RuntimeSession.tenant_id == claim.tenant_id,
                RuntimeSession.run_id == claim.run_id,
            )
        )
        if runtime_session is None:
            raise ToolAccessDenied(spec.name, spec.version, "runtime session is unavailable")
        return self._authorize_snapshot(runtime_session.tool_policy_snapshot, executor)

    @staticmethod
    def _authorize_snapshot(
        snapshot: dict[str, Any],
        executor: ToolExecutor,
    ) -> ToolAuthorization:
        return authorize_tool(
            snapshot,
            executor.spec,
            secret_name_selector=lambda tool_config: executor_required_secret_names(
                executor, tool_config
            ),
        )

    async def _owned_run(self, session: AsyncSession, claim: RunClaim, worker_id: str) -> Run:
        run = await session.scalar(
            select(Run)
            .where(Run.tenant_id == claim.tenant_id, Run.id == claim.run_id)
            .with_for_update()
        )
        if not self._owns(run, claim, worker_id):
            raise ToolLeaseLost(str(claim.run_id))
        if RunStatus(run.status) not in _ACTIVE_RUN_STATUSES:
            raise ToolLeaseLost(str(claim.run_id))
        return run

    @staticmethod
    def _owns(run: Run | None, claim: RunClaim, worker_id: str) -> bool:
        return bool(
            run is not None
            and run.lease_owner == worker_id
            and run.lease_token == claim.lease_token
            and run.lease_expires_at is not None
            and run.lease_expires_at > datetime.now(UTC)
        )

    @staticmethod
    def _prepared(
        call: ToolCall,
        executor: ToolExecutor,
        authorization: ToolAuthorization,
        request: ToolGatewayRequest,
        tenant_context: TenantContext,
        secrets: dict[str, str],
        run: Run,
        task: Task,
    ) -> _PreparedCall:
        return _PreparedCall(
            tool_call_id=call.id,
            run_step_id=call.run_step_id,
            executor=executor,
            authorization=authorization,
            context=ToolExecutionContext(
                tenant_id=call.tenant_id,
                run_id=call.run_id,
                run_step_id=call.run_step_id,
                tool_call_id=call.id,
                project_id=task.project_id,
                task_id=task.id,
                agent_id=run.agent_id,
                agent_version_id=run.agent_version_id,
                provider_id=call.provider_id,
                run_tool_binding_id=call.run_tool_binding_id,
                provider_request_id=call.provider_request_id,
                idempotency_key=call.idempotency_key,
                deadline=call.provider_deadline_at,
                trace_id=call.provider_trace_id,
                actor_id=request.caller,
                correlation_id=tenant_context.correlation_id,
                tool_config=authorization.tool_config,
            ),
            previous_attempts=tuple(call.attempts),
            secrets=secrets,
        )

    @staticmethod
    def _activate_external_binding(
        binding: RunToolBinding,
        call: ToolCall,
        *,
        run: Run,
        request: ToolGatewayRequest,
        arguments_hash: str,
        executor: ToolExecutor,
    ) -> None:
        if not isinstance(executor, ExternalToolExecutor):
            raise ToolAccessDenied(
                request.tool_name,
                request.tool_version,
                "external binding cannot use a local Tool executor",
            )
        ToolGateway._ensure_external_binding_capacity(binding, call, run)
        if call.arguments_hash != arguments_hash:
            raise ToolIdempotencyConflict(request.idempotency_key)
        if binding.status == RunToolBindingStatus.FROZEN.value:
            binding.status = RunToolBindingStatus.ACTIVE.value
        elif binding.status != RunToolBindingStatus.ACTIVE.value:
            raise provider_error(
                ToolProviderErrorCode.EXPIRED,
                "Run Tool Binding is not active",
                run_id=run.id,
                tool_call_id=call.id,
                provider_id=binding.provider_id,
            )
        binding.call_count += 1
        binding.active_provider_request_id = call.provider_request_id
        binding.cancel_status = ToolProviderCancelStatus.NONE.value
        binding.revision += 1
        call.provider_status = ToolCallStatus.RUNNING.value

    @staticmethod
    def _ensure_external_binding_capacity(
        binding: RunToolBinding,
        call: ToolCall,
        run: Run,
    ) -> None:
        if (
            binding.call_count >= binding.max_calls
            or binding.total_duration_ms >= binding.max_total_duration_ms
        ):
            raise provider_error(
                ToolProviderErrorCode.BINDING_BUDGET_EXCEEDED,
                "Run Tool Binding call or duration budget is exhausted",
                run_id=run.id,
                tool_call_id=call.id,
                provider_id=binding.provider_id,
                cause="BINDING_BUDGET_EXHAUSTED",
            )
        if (
            binding.active_provider_request_id is not None
            and binding.active_provider_request_id != call.provider_request_id
        ):
            raise provider_error(
                ToolProviderErrorCode.BINDING_BUDGET_EXCEEDED,
                "Run Tool Binding already has an in-flight Provider request",
                run_id=run.id,
                tool_call_id=call.id,
                provider_id=binding.provider_id,
                cause="BINDING_CALL_IN_PROGRESS",
            )

    @staticmethod
    def _external_deadline(
        run: Run,
        binding: RunToolBinding,
        executor: ExternalToolExecutor,
        now: datetime,
    ) -> datetime:
        remaining_binding_duration_ms = binding.max_total_duration_ms - binding.total_duration_ms
        if remaining_binding_duration_ms <= 0:
            raise provider_error(
                ToolProviderErrorCode.BINDING_BUDGET_EXCEEDED,
                "Run Tool Binding duration budget is exhausted",
                run_id=run.id,
                provider_id=executor.provider_id,
                cause="BINDING_DURATION_EXHAUSTED",
            )
        candidates = [
            now + timedelta(seconds=executor.effective_timeout_seconds),
            now + timedelta(milliseconds=remaining_binding_duration_ms),
        ]
        if executor.snapshot.binding_expires_at is not None:
            candidates.append(executor.snapshot.binding_expires_at)
        if run.timeout_seconds is not None:
            candidates.append(run.created_at + timedelta(seconds=run.timeout_seconds))
        deadline = min(candidates)
        if deadline <= now:
            raise provider_error(
                ToolProviderErrorCode.TIMEOUT,
                "Run or Tool Binding deadline has elapsed",
                run_id=run.id,
                provider_id=executor.provider_id,
                cause="DEADLINE_ELAPSED",
            )
        return deadline

    @staticmethod
    def _provider_event_payload(
        call: ToolCall,
        binding: RunToolBinding,
        *,
        task_id: UUID,
        attempt: int,
        status: str,
        duration_ms: int | float | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        return {
            "run_id": str(call.run_id),
            "task_id": str(task_id),
            "tool_call_id": str(call.id),
            "provider_id": str(binding.provider_id),
            "binding_digest": binding.binding_digest,
            "request_id": call.provider_request_id,
            "tool_name": call.tool_name,
            "tool_version": call.tool_version,
            "attempt": attempt,
            "deadline": (
                call.provider_deadline_at.astimezone(UTC).isoformat()
                if call.provider_deadline_at is not None
                else None
            ),
            "duration_ms": duration_ms,
            "result_status": status,
            "error_code": error_code,
            "trace_id": call.provider_trace_id,
        }

    @staticmethod
    def _result(call: ToolCall, *, cached: bool = False) -> ToolGatewayResult:
        return ToolGatewayResult(
            tool_call_id=call.id,
            run_step_id=call.run_step_id,
            status=ToolCallStatus(call.status),
            output=call.result,
            error=call.error,
            usage=call.usage,
            attempts=len(call.attempts),
            cached=cached,
        )

    @staticmethod
    async def _cancel_external(
        executor: ExternalToolExecutor,
        context: ToolExecutionContext,
        *,
        reason: str,
    ) -> bool:
        try:
            async with asyncio.timeout(executor.snapshot.policy.cancel_timeout_ms / 1000):
                return await executor.cancel(context, reason=reason)
        except (TimeoutError, ToolError, ToolProviderError):
            return False

    @staticmethod
    def _cancellation_usage(
        usage: dict[str, Any],
        *,
        confirmed: bool,
    ) -> dict[str, Any]:
        return {
            **usage,
            "provider_cancel_requested": True,
            "provider_cancel_confirmed": confirmed,
            "provider_cancel_attempts": int(usage.get("provider_cancel_attempts", 0)) + 1,
            "external_execution_may_continue": not confirmed,
        }

    @staticmethod
    def _should_retry(
        spec: ToolDefinitionSpec,
        code: str,
        attempt_number: int,
        *,
        retryable: bool | None = None,
        max_attempts: int | None = None,
        allow_undeclared: bool = False,
    ) -> bool:
        policy = spec.retry_policy
        return (
            retryable is not False
            and attempt_number < (max_attempts or policy.max_attempts)
            and (code in policy.retryable_codes or allow_undeclared)
        )

    @staticmethod
    def _attempt(
        number: int,
        started: float,
        *,
        status: str,
        code: str | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "number": number,
            "status": status,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        }
        if code is not None:
            result["code"] = code
        return result

    @staticmethod
    def _record_definition(
        session: AsyncSession,
        context: TenantContext,
        definition: ToolDefinition,
        run_id: UUID,
    ) -> None:
        payload = {
            "name": definition.name,
            "version": definition.version,
            "content_hash": definition.content_hash,
            "implementation_hash": definition.implementation_hash,
        }
        session.add(
            Event(
                tenant_id=context.tenant_id,
                event_type="ToolDefinitionRegistered",
                aggregate_type="tool_definition",
                aggregate_id=definition.id,
                run_id=run_id,
                actor_id=context.actor_id,
                payload=payload,
                correlation_id=context.correlation_id,
            )
        )
        session.add(
            AuditRecord(
                tenant_id=context.tenant_id,
                action="tool.definition.register",
                resource_type="tool_definition",
                resource_id=definition.id,
                actor_id=context.actor_id,
                details=payload,
                correlation_id=context.correlation_id,
            )
        )

    @staticmethod
    def _record_approval_requested(
        session: AsyncSession,
        context: TenantContext,
        approval: ToolApprovalRequest,
        call: ToolCall,
    ) -> None:
        payload = {
            "approval_id": str(approval.id),
            "tool_call_id": str(call.id),
            "run_step_id": str(call.run_step_id),
            "tool": f"{call.tool_name}@{call.tool_version}",
            "risk_level": approval.risk_level,
            "argument_keys": sorted(approval.arguments_redacted),
            "expires_at": approval.expires_at.isoformat(),
            "allowed_scopes": ["once", "run"],
            "status": approval.status,
        }
        session.add(
            Event(
                tenant_id=context.tenant_id,
                event_type="ApprovalRequested",
                aggregate_type="tool_approval_request",
                aggregate_id=approval.id,
                run_id=call.run_id,
                actor_id=context.actor_id,
                payload=payload,
                correlation_id=context.correlation_id,
            )
        )
        session.add(
            AuditRecord(
                tenant_id=context.tenant_id,
                action="tool.approval.request",
                resource_type="tool_approval_request",
                resource_id=approval.id,
                actor_id=context.actor_id,
                details=payload,
                correlation_id=context.correlation_id,
            )
        )

    @staticmethod
    def _record_approval_grant_reused(
        session: AsyncSession,
        context: TenantContext,
        approval: ToolApprovalRequest,
        call: ToolCall,
    ) -> None:
        payload = {
            "approval_id": str(approval.id),
            "tool_call_id": str(call.id),
            "tool": f"{call.tool_name}@{call.tool_version}",
            "allowed_scope": "run",
            "status": "approved",
        }
        session.add(
            Event(
                tenant_id=context.tenant_id,
                event_type="ToolApprovalGrantReused",
                aggregate_type="tool_approval_request",
                aggregate_id=approval.id,
                run_id=call.run_id,
                actor_id=context.actor_id,
                payload=payload,
                correlation_id=context.correlation_id,
            )
        )
        session.add(
            AuditRecord(
                tenant_id=context.tenant_id,
                action="tool.approval.grant_reuse",
                resource_type="tool_approval_request",
                resource_id=approval.id,
                actor_id=context.actor_id,
                details=payload,
                correlation_id=context.correlation_id,
            )
        )

    @staticmethod
    def _record(
        session: AsyncSession,
        context: TenantContext,
        *,
        event_type: str,
        call: ToolCall,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        session.add(
            Event(
                tenant_id=context.tenant_id,
                event_type=event_type,
                aggregate_type="tool_call",
                aggregate_id=call.id,
                run_id=call.run_id,
                actor_id=context.actor_id,
                payload=payload,
                correlation_id=context.correlation_id,
            )
        )
        session.add(
            AuditRecord(
                tenant_id=context.tenant_id,
                action=action,
                resource_type="tool_call",
                resource_id=call.id,
                actor_id=context.actor_id,
                details=payload,
                correlation_id=context.correlation_id,
            )
        )
