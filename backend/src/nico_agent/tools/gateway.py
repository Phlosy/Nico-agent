"""Lease-aware Tool Gateway with authorization, idempotency and audit persistence."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    ToolCall,
    ToolDefinition,
)
from nico_agent.domain.states import RunStatus, RunStepStatus, ToolCallStatus
from nico_agent.tools.contracts import (
    ToolDefinitionSpec,
    ToolExecutionContext,
    ToolExecutor,
    canonical_hash,
    canonical_json,
)
from nico_agent.tools.errors import (
    ToolAccessDenied,
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
_FIRST_TOOL_STEP_SEQUENCE = 1_000_000


class ToolGatewayRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool_name: str = Field(min_length=1, max_length=120)
    tool_version: str = Field(min_length=1, max_length=50)
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=200)
    caller: str = Field(min_length=1, max_length=200)
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


class ToolGateway:
    def __init__(
        self,
        database: Database,
        registry: ToolRegistry,
        *,
        secret_resolver: SecretResolver | None = None,
    ) -> None:
        self.database = database
        self.registry = registry
        self.secret_resolver = secret_resolver or EnvironmentSecretResolver()

    async def list_authorized(
        self,
        claim: RunClaim,
        *,
        worker_id: str,
    ) -> tuple[ToolDefinitionSpec, ...]:
        """Return only exact tool versions currently executable under the Run lease."""

        context = TenantContext(claim.tenant_id, f"worker:{worker_id}", uuid4())
        authorized: list[ToolDefinitionSpec] = []
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
            for spec in self.registry.definitions():
                try:
                    authorize_tool(runtime_session.tool_policy_snapshot, spec)
                except ToolError:
                    continue
                executor = self.registry.get(spec.name, spec.version)
                definition = await self._definition(session, context, executor, run.id)
                try:
                    await self._authorization(session, claim, spec, definition)
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
        prepared = prepared_or_result
        secrets = prepared.secrets
        attempts = list(prepared.previous_attempts)
        final_status = ToolCallStatus.FAILED
        final_output: dict[str, Any] | None = None
        final_error: dict[str, Any] | None = None
        final_usage: dict[str, Any] = {}

        try:
            for attempt_number in range(
                len(attempts) + 1,
                prepared.executor.spec.retry_policy.max_attempts + 1,
            ):
                started = time.monotonic()
                try:
                    async with asyncio.timeout(prepared.executor.spec.timeout_seconds):
                        execution = await prepared.executor.execute(
                            prepared.context,
                            request.arguments,
                            secrets,
                        )
                    prepared.executor.spec.validate_output(execution.output)
                    final_output = redact_value(execution.output, secrets)
                    final_usage = redact_value(execution.usage, secrets)
                    attempts.append(self._attempt(attempt_number, started, status="succeeded"))
                    final_status = ToolCallStatus.SUCCEEDED
                    break
                except TimeoutError:
                    code = "TOOL_TIMEOUT"
                    attempts.append(
                        self._attempt(attempt_number, started, status="timed_out", code=code)
                    )
                    if not self._should_retry(prepared.executor.spec, code, attempt_number):
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
                    if not self._should_retry(prepared.executor.spec, exc.code, attempt_number):
                        final_error = {"code": exc.code, "message": safe_message}
                        break
                    if not await self._checkpoint_attempts(
                        claim,
                        worker_id=worker_id,
                        prepared=prepared,
                        attempts=attempts,
                    ):
                        raise ToolLeaseLost(str(claim.run_id)) from None
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

                if prepared.executor.spec.retry_policy.backoff_seconds:
                    await asyncio.sleep(prepared.executor.spec.retry_policy.backoff_seconds)
            if final_status is ToolCallStatus.FAILED and final_error is None:
                final_error = {
                    "code": "TOOL_RETRY_EXHAUSTED",
                    "message": "the tool retry budget was exhausted",
                }
        except asyncio.CancelledError:
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
                usage={},
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
    ) -> _PreparedCall | ToolGatewayResult:
        context = TenantContext(claim.tenant_id, f"worker:{worker_id}", request.correlation_id)
        async with self.database.tenant_transaction(context) as session:
            run = await self._owned_run(session, claim, worker_id)
            definition = await self._definition(session, context, executor, run.id)
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
                if existing.execution_lease_token == claim.lease_token:
                    raise ToolCallInProgress(str(existing.id))
                existing.execution_owner = worker_id
                existing.execution_lease_token = claim.lease_token
                existing.status = ToolCallStatus.RUNNING.value
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
                authorization = await self._authorization(session, claim, executor.spec, definition)
                secrets = resolve_secrets(self.secret_resolver, authorization.secret_refs)
                run.status = RunStatus.WAITING_FOR_TOOL.value
                run.revision += 1
                return self._prepared(
                    existing,
                    executor,
                    authorization,
                    request,
                    context,
                    secrets,
                )

            rejected: ToolError | None = None
            authorization: ToolAuthorization | None = None
            secrets: dict[str, str] = {}
            try:
                authorization = await self._authorization(session, claim, executor.spec, definition)
                executor.spec.validate_input(request.arguments)
                secrets = resolve_secrets(self.secret_resolver, authorization.secret_refs)
            except ToolError as exc:
                rejected = exc

            sequence = (
                await session.scalar(
                    select(
                        func.coalesce(func.max(RunStep.sequence), _FIRST_TOOL_STEP_SEQUENCE - 1)
                    ).where(
                        RunStep.tenant_id == claim.tenant_id,
                        RunStep.run_id == claim.run_id,
                        RunStep.sequence >= _FIRST_TOOL_STEP_SEQUENCE,
                    )
                )
            ) + 1
            now = datetime.now(UTC)
            step = RunStep(
                tenant_id=claim.tenant_id,
                run_id=claim.run_id,
                sequence=sequence,
                kind=f"tool:{executor.spec.name}"[:100],
                status=(
                    RunStepStatus.FAILED.value
                    if rejected is not None
                    else RunStepStatus.RUNNING.value
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
            call = ToolCall(
                tenant_id=claim.tenant_id,
                run_id=claim.run_id,
                run_step_id=step.id,
                tool_definition_id=definition.id,
                tool_name=executor.spec.name,
                tool_version=executor.spec.version,
                idempotency_key=request.idempotency_key,
                arguments_hash=arguments_hash,
                caller=request.caller,
                execution_owner=worker_id,
                execution_lease_token=claim.lease_token,
                arguments=redact_value(request.arguments),
                status=(
                    ToolCallStatus.FAILED.value
                    if rejected is not None
                    else ToolCallStatus.RUNNING.value
                ),
                error=(
                    {"code": rejected.code, "message": rejected.message}
                    if rejected is not None
                    else None
                ),
                started_at=now,
                ended_at=now if rejected is not None else None,
            )
            session.add(call)
            await session.flush()
            self._record(
                session,
                context,
                event_type="ToolCallRejected" if rejected is not None else "ToolCallStarted",
                call=call,
                action="tool.call.reject" if rejected is not None else "tool.call.start",
                payload={
                    "tool": executor.spec.reference,
                    "status": call.status,
                    "code": rejected.code if rejected is not None else None,
                    "run_step_id": str(step.id),
                },
            )
            if rejected is not None:
                return self._result(call)
            assert authorization is not None
            run.status = RunStatus.WAITING_FOR_TOOL.value
            run.revision += 1
            return self._prepared(call, executor, authorization, request, context, secrets)

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
            call.status = status.value
            call.attempts = attempts
            call.result = output
            call.error = error
            call.usage = usage
            call.ended_at = now
            call.revision += 1
            step.status = {
                ToolCallStatus.SUCCEEDED: RunStepStatus.COMPLETED,
                ToolCallStatus.CANCELLED: RunStepStatus.CANCELLED,
            }.get(status, RunStepStatus.FAILED).value
            step.output = output
            step.error = error
            step.ended_at = now
            step.revision += 1
            run.status = RunStatus.RUNNING.value
            run.revision += 1
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
            await session.flush()
            return True

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
    ) -> ToolAuthorization:
        if definition.status != "enabled":
            raise ToolDisabled(spec.name, spec.version)
        executor = self.registry.get(spec.name, spec.version)
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
        return authorize_tool(runtime_session.tool_policy_snapshot, spec)

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
                actor_id=request.caller,
                correlation_id=tenant_context.correlation_id,
                tool_config=authorization.tool_config,
            ),
            previous_attempts=tuple(call.attempts),
            secrets=secrets,
        )

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
    def _should_retry(spec: ToolDefinitionSpec, code: str, attempt_number: int) -> bool:
        policy = spec.retry_policy
        return attempt_number < policy.max_attempts and code in policy.retryable_codes

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
