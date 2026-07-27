"""Binding-first resolver and ToolExecutor adapter for external Providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select

from nico_agent.database import Database, RunClaim, TenantContext
from nico_agent.domain.models import (
    ExternalToolProvider,
    Run,
    RunToolBinding,
    Task,
    ToolDefinition,
)
from nico_agent.domain.states import (
    ExternalToolProviderStatus,
    RunToolBindingStatus,
)
from nico_agent.tool_providers.client import ProviderEndpoint, ToolProviderClient
from nico_agent.tool_providers.contracts import (
    ProviderCancelRequest,
    ProviderToolCallRequest,
    ProviderToolCallStatus,
    ProviderTrace,
    RunToolBindingPolicy,
    RunToolBindingSnapshot,
)
from nico_agent.tool_providers.errors import ToolProviderErrorCode, provider_error
from nico_agent.tool_providers.service import tool_spec_from_definition
from nico_agent.tools.contracts import (
    ToolDefinitionSpec,
    ToolExecutionContext,
    ToolExecutionResult,
)


@dataclass(frozen=True, slots=True)
class ExternalToolExecutor:
    client: ToolProviderClient
    spec: ToolDefinitionSpec
    implementation_hash: str
    snapshot: RunToolBindingSnapshot
    endpoint: ProviderEndpoint

    @property
    def provider_id(self) -> UUID:
        return self.snapshot.provider_id

    @property
    def provider_binding_id(self) -> UUID:
        return self.snapshot.binding_id

    @property
    def effective_timeout_seconds(self) -> float:
        return self.snapshot.policy.provider_request_timeout_ms / 1000

    @property
    def effective_max_attempts(self) -> int:
        return self.snapshot.policy.budget.max_retries + 1

    async def execute(
        self,
        context: ToolExecutionContext,
        arguments: dict[str, Any],
        secrets: dict[str, str],
    ) -> ToolExecutionResult:
        del secrets
        if (
            context.project_id is None
            or context.task_id is None
            or context.agent_id is None
            or context.agent_version_id is None
            or context.tool_call_id is None
            or context.provider_request_id is None
            or context.idempotency_key is None
            or context.attempt is None
            or context.deadline is None
            or context.trace_id is None
        ):
            raise provider_error(
                ToolProviderErrorCode.PROTOCOL_ERROR,
                "external Tool execution context is incomplete",
                run_id=context.run_id,
                provider_id=self.provider_id,
                attempt=context.attempt,
                cause="EXECUTION_CONTEXT_INCOMPLETE",
            )
        request = ProviderToolCallRequest(
            provider_id=str(self.provider_id),
            binding_digest=self.snapshot.binding_digest,
            request_id=context.provider_request_id,
            tool_call_id=str(context.tool_call_id),
            idempotency_key=context.idempotency_key,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            run_id=context.run_id,
            task_id=context.task_id,
            agent_id=context.agent_id,
            agent_version_id=context.agent_version_id,
            tool=self.snapshot.tool,
            arguments=arguments,
            deadline=context.deadline,
            attempt=context.attempt,
            trace=ProviderTrace(
                trace_id=context.trace_id,
                parent_event_id=str(context.correlation_id),
            ),
        )
        response = await self.client.execute(
            self.endpoint,
            request,
            max_response_bytes=self.snapshot.policy.max_response_bytes,
        )
        if (
            response.binding_digest != self.snapshot.binding_digest
            or response.tool_call_id != str(context.tool_call_id)
            or response.tool.name != self.spec.name
            or response.tool.version != self.spec.version
        ):
            raise provider_error(
                ToolProviderErrorCode.PROTOCOL_ERROR,
                "Tool Provider response scope or Tool identity does not match",
                run_id=context.run_id,
                provider_id=self.provider_id,
                attempt=context.attempt,
                cause="RESPONSE_SCOPE_MISMATCH",
            )
        if response.status is not ProviderToolCallStatus.SUCCEEDED:
            error = response.error
            code = {
                ProviderToolCallStatus.TIMED_OUT: ToolProviderErrorCode.TIMEOUT,
                ProviderToolCallStatus.CANCELLED: ToolProviderErrorCode.CANCEL_ERROR,
            }.get(response.status, ToolProviderErrorCode.PROTOCOL_ERROR)
            raise provider_error(
                code,
                error.message if error is not None else "Tool Provider execution failed",
                run_id=context.run_id,
                provider_id=self.provider_id,
                attempt=context.attempt,
                cause=error.code if error is not None else "PROVIDER_EXECUTION_FAILED",
                retryable=error.retryable if error is not None else False,
            )
        duration_ms = max(
            0,
            round((response.finished_at - response.started_at).total_seconds() * 1000),
        )
        return ToolExecutionResult(
            output=response.result or {},
            usage={
                "provider_id": str(self.provider_id),
                "binding_id": str(self.provider_binding_id),
                "binding_digest": self.snapshot.binding_digest,
                "provider_request_id": response.request_id,
                "provider_execution_id": response.provider_execution_id,
                "provider_duration_ms": duration_ms,
                "idempotency_replayed": response.idempotency_replayed,
            },
        )

    async def cancel(
        self,
        context: ToolExecutionContext,
        *,
        reason: str,
    ) -> bool:
        if context.provider_request_id is None:
            return False
        deadline = datetime.now(UTC) + timedelta(
            milliseconds=self.snapshot.policy.cancel_timeout_ms
        )
        response = await self.client.cancel(
            self.endpoint,
            ProviderCancelRequest(
                provider_id=str(self.provider_id),
                binding_digest=self.snapshot.binding_digest,
                request_id=context.provider_request_id,
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                run_id=context.run_id,
                reason=reason,
                deadline=deadline,
            ),
        )
        return not response.side_effects_may_continue


class ExternalToolResolver:
    def __init__(
        self,
        database: Database,
        client: ToolProviderClient,
    ) -> None:
        self.database = database
        self.client = client

    async def resolve(
        self,
        claim: RunClaim,
        *,
        worker_id: str,
        tool_name: str,
        tool_version: str,
    ) -> ExternalToolExecutor | None:
        context = TenantContext(claim.tenant_id, f"worker:{worker_id}", claim.lease_token)
        async with self.database.tenant_transaction(context) as session:
            binding = await session.scalar(
                select(RunToolBinding).where(
                    RunToolBinding.tenant_id == claim.tenant_id,
                    RunToolBinding.run_id == claim.run_id,
                    RunToolBinding.tool_name == tool_name,
                    RunToolBinding.tool_version == tool_version,
                )
            )
            if binding is None:
                return None
            run = await session.scalar(
                select(Run).where(
                    Run.tenant_id == claim.tenant_id,
                    Run.id == claim.run_id,
                )
            )
            provider = await session.scalar(
                select(ExternalToolProvider).where(
                    ExternalToolProvider.tenant_id == claim.tenant_id,
                    ExternalToolProvider.id == binding.provider_id,
                )
            )
            definition = await session.scalar(
                select(ToolDefinition).where(
                    ToolDefinition.tenant_id == claim.tenant_id,
                    ToolDefinition.id == binding.tool_definition_id,
                )
            )
            task = (
                None
                if run is None
                else await session.scalar(
                    select(Task).where(
                        Task.tenant_id == claim.tenant_id,
                        Task.id == run.task_id,
                    )
                )
            )
            if run is None or provider is None or definition is None or task is None:
                raise provider_error(
                    ToolProviderErrorCode.BINDING_NOT_FOUND,
                    "frozen Tool Binding dependencies are unavailable",
                    run_id=claim.run_id,
                    provider_id=binding.provider_id,
                )
            snapshot = _snapshot_for_binding(run.tool_binding_snapshot, binding.id)
            _validate_binding(
                binding,
                provider=provider,
                run=run,
                task=task,
                snapshot=snapshot,
            )
            return ExternalToolExecutor(
                client=self.client,
                spec=tool_spec_from_definition(definition),
                implementation_hash=definition.implementation_hash,
                snapshot=snapshot,
                endpoint=ProviderEndpoint(
                    provider_id=provider.id,
                    endpoint_ref=provider.endpoint_url,
                    credential_ref=provider.credential_ref,
                    allow_http=bool(provider.endpoint_policy.get("allow_http", False)),
                    allow_loopback=bool(provider.endpoint_policy.get("allow_loopback", False)),
                    allow_private=bool(provider.endpoint_policy.get("allow_private", False)),
                ),
            )

    async def list_for_run(
        self,
        claim: RunClaim,
        *,
        worker_id: str,
    ) -> tuple[ExternalToolExecutor, ...]:
        context = TenantContext(claim.tenant_id, f"worker:{worker_id}", claim.lease_token)
        async with self.database.tenant_transaction(context) as session:
            references = list(
                await session.execute(
                    select(RunToolBinding.tool_name, RunToolBinding.tool_version).where(
                        RunToolBinding.tenant_id == claim.tenant_id,
                        RunToolBinding.run_id == claim.run_id,
                    )
                )
            )
        resolved: list[ExternalToolExecutor] = []
        for name, version in references:
            executor = await self.resolve(
                claim,
                worker_id=worker_id,
                tool_name=name,
                tool_version=version,
            )
            if executor is not None:
                resolved.append(executor)
        return tuple(resolved)


def _snapshot_for_binding(
    snapshot: dict[str, Any],
    binding_id: UUID,
) -> RunToolBindingSnapshot:
    values = snapshot.get("bindings") if isinstance(snapshot, dict) else None
    if not isinstance(values, list):
        raise provider_error(
            ToolProviderErrorCode.BINDING_IMMUTABLE,
            "Run Tool Binding Snapshot is unavailable",
            cause="SNAPSHOT_MISSING",
        )
    for value in values:
        if isinstance(value, dict) and value.get("binding_id") == str(binding_id):
            try:
                return RunToolBindingSnapshot.model_validate(value)
            except ValueError as exc:
                raise provider_error(
                    ToolProviderErrorCode.BINDING_IMMUTABLE,
                    "Run Tool Binding Snapshot failed integrity validation",
                    cause="SNAPSHOT_INVALID",
                ) from exc
    raise provider_error(
        ToolProviderErrorCode.BINDING_NOT_FOUND,
        "Run Tool Binding is absent from the frozen Snapshot",
        cause="SNAPSHOT_BINDING_MISSING",
    )


def _validate_binding(
    binding: RunToolBinding,
    *,
    provider: ExternalToolProvider,
    run: Run,
    task: Task,
    snapshot: RunToolBindingSnapshot,
) -> None:
    now = datetime.now(UTC)
    if snapshot.binding_digest != binding.binding_digest:
        raise provider_error(
            ToolProviderErrorCode.BINDING_IMMUTABLE,
            "Run Tool Binding digest does not match its Snapshot",
            run_id=run.id,
            provider_id=provider.id,
            cause="BINDING_DIGEST_MISMATCH",
        )
    scope = snapshot.scope
    if (
        scope.tenant_id != run.tenant_id
        or scope.project_id != task.project_id
        or scope.run_id != run.id
        or scope.task_id != task.id
        or scope.agent_id != run.agent_id
        or scope.agent_version_id != run.agent_version_id
        or snapshot.provider_id != provider.id
        or snapshot.endpoint_identity != provider.endpoint_identity
    ):
        raise provider_error(
            ToolProviderErrorCode.SCOPE_MISMATCH,
            "Run Tool Binding scope does not match the authoritative Run",
            run_id=run.id,
            provider_id=provider.id,
            cause="BINDING_SCOPE_MISMATCH",
        )
    if provider.status != ExternalToolProviderStatus.ACTIVE.value:
        raise provider_error(
            ToolProviderErrorCode.DISABLED,
            "Tool Provider is no longer active",
            run_id=run.id,
            provider_id=provider.id,
        )
    if binding.status not in {
        RunToolBindingStatus.FROZEN.value,
        RunToolBindingStatus.ACTIVE.value,
    }:
        raise provider_error(
            ToolProviderErrorCode.EXPIRED,
            "Run Tool Binding is no longer active",
            run_id=run.id,
            provider_id=provider.id,
        )
    if (
        binding.expires_at is not None
        and binding.expires_at <= now
        or provider.expires_at is not None
        and provider.expires_at <= now
    ):
        raise provider_error(
            ToolProviderErrorCode.EXPIRED,
            "Run Tool Binding or Provider has expired",
            run_id=run.id,
            provider_id=provider.id,
        )
    policy = RunToolBindingPolicy.model_validate(binding.policy)
    if policy != snapshot.policy:
        raise provider_error(
            ToolProviderErrorCode.BINDING_IMMUTABLE,
            "Run Tool Binding policy does not match its Snapshot",
            run_id=run.id,
            provider_id=provider.id,
            cause="BINDING_POLICY_MISMATCH",
        )
