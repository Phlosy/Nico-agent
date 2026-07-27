"""Registry lifecycle and transactional Run binding freeze service."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.config import Settings
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainConflict, ResourceNotFound
from nico_agent.domain.models import (
    AgentRunRelation,
    AgentVersion,
    AuditRecord,
    Event,
    ExternalToolProvider,
    Project,
    Run,
    RunToolBinding,
    Task,
    Tenant,
    ToolCall,
    ToolDefinition,
)
from nico_agent.domain.states import (
    ExternalToolProviderStatus,
    RunToolBindingStatus,
    ToolDefinitionStatus,
    ToolProviderCancelStatus,
)
from nico_agent.net.safe_http import SafeHttpError
from nico_agent.tool_providers.client import ProviderEndpoint, ToolProviderClient
from nico_agent.tool_providers.contracts import (
    ExternalToolProviderCreate,
    ExternalToolProviderRead,
    ProviderCancelRequest,
    ProviderCapabilitiesResponse,
    ProviderHealthStatus,
    ProviderToolContract,
    RunToolBindingCreate,
    RunToolBindingScope,
    RunToolBindingSnapshot,
    ToolBindingApprovalMode,
    provider_status_can_transition,
    schema_digest,
)
from nico_agent.tool_providers.errors import (
    ToolProviderError,
    ToolProviderErrorCode,
    provider_error,
)
from nico_agent.tool_providers.security import provider_endpoint_identity
from nico_agent.tools.contracts import (
    ToolDefinitionSpec,
    ToolIsolation,
    ToolRetryPolicy,
    ToolRisk,
)
from nico_agent.tools.policy import authorize_tool, build_tool_policy_snapshot


@dataclass(frozen=True, slots=True)
class _CancelTarget:
    call_id: UUID
    run_id: UUID
    task_id: UUID
    binding_id: UUID
    provider_id: UUID
    request_id: str
    tool_name: str
    tool_version: str
    binding_digest: str
    endpoint: ProviderEndpoint
    request: ProviderCancelRequest
    timeout_seconds: float
    supports_cancellation: bool


@dataclass(frozen=True, slots=True)
class _CancelOutcome:
    target: _CancelTarget
    confirmed: bool
    provider_execution_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderBindingObservation:
    revision: int
    status: str
    endpoint_identity: str
    capability_digest: str | None
    capability_snapshot: dict[str, Any]

    @classmethod
    def from_provider(
        cls,
        provider: ExternalToolProvider,
        capabilities: ProviderCapabilitiesResponse,
    ) -> ProviderBindingObservation:
        return cls(
            revision=provider.revision,
            status=provider.status,
            endpoint_identity=provider.endpoint_identity,
            capability_digest=provider.capability_digest,
            capability_snapshot=capabilities.model_dump(mode="json"),
        )


class ToolProviderService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        client: ToolProviderClient | None = None,
        approval_required_risks: frozenset[str] = frozenset({"medium", "high"}),
    ) -> None:
        self.database = database
        self.settings = settings
        self.client = client or ToolProviderClient(
            connect_timeout=settings.tool_provider_connect_timeout_seconds,
            read_timeout=settings.tool_provider_read_timeout_seconds,
            max_response_bytes=settings.tool_provider_max_response_bytes,
        )
        self.approval_required_risks = approval_required_risks

    async def register(
        self,
        context: TenantContext,
        command: ExternalToolProviderCreate,
    ) -> ExternalToolProviderRead:
        now = datetime.now(UTC)
        if command.expires_at is not None and command.expires_at <= now:
            raise provider_error(
                ToolProviderErrorCode.EXPIRED,
                "Tool Provider expiry must be in the future",
            )
        endpoint_policy = self._endpoint_policy(command.endpoint_ref)
        provider_id = uuid4()
        endpoint = ProviderEndpoint(
            provider_id=provider_id,
            endpoint_ref=command.endpoint_ref,
            credential_ref=command.credential_ref,
            **endpoint_policy,
        )
        try:
            await self.client.validate_endpoint(endpoint)
        except SafeHttpError as exc:
            raise provider_error(
                ToolProviderErrorCode.CONNECTION_ERROR,
                "Tool Provider endpoint validation failed",
                provider_id=provider_id,
                cause=f"ENDPOINT_{exc.code}",
            ) from exc
        endpoint_identity = provider_endpoint_identity(
            command.endpoint_ref,
            allow_http=endpoint_policy["allow_http"],
            allow_http_loopback=endpoint_policy["allow_loopback"],
        )
        async with self.database.tenant_transaction(context) as session:
            await self._require_project(session, context, command.project_id)
            provider = ExternalToolProvider(
                id=provider_id,
                tenant_id=context.tenant_id,
                project_id=command.project_id,
                name=command.name,
                protocol=command.protocol,
                endpoint_url=command.endpoint_ref,
                endpoint_identity=endpoint_identity,
                credential_ref=command.credential_ref,
                status=ExternalToolProviderStatus.REGISTERED.value,
                capability_snapshot={},
                endpoint_policy=endpoint_policy,
                metadata_json=command.metadata,
                expires_at=command.expires_at,
            )
            session.add(provider)
            self._record(
                session,
                context,
                event_type="tool.provider.registered",
                action="tool.provider.register",
                provider=provider,
                payload={
                    "provider_id": str(provider.id),
                    "project_id": str(provider.project_id) if provider.project_id else None,
                    "endpoint_identity": provider.endpoint_identity,
                    "status": provider.status,
                },
            )
            await session.flush()
            return self._read(provider)

    async def get(
        self,
        context: TenantContext,
        provider_id: UUID,
    ) -> ExternalToolProviderRead:
        async with self.database.tenant_transaction(context) as session:
            provider = await self._provider(session, context, provider_id)
            return self._read(provider)

    async def verify(
        self,
        context: TenantContext,
        provider_id: UUID,
    ) -> ExternalToolProviderRead:
        async with self.database.tenant_transaction(context) as session:
            provider = await self._provider(session, context, provider_id)
            revision = provider.revision
            endpoint = self._endpoint(provider)
        health = await self.client.health(endpoint)
        if health.status is not ProviderHealthStatus.HEALTHY:
            raise provider_error(
                ToolProviderErrorCode.CONNECTION_ERROR,
                "Tool Provider healthcheck did not report healthy",
                provider_id=provider_id,
                cause="HEALTHCHECK_FAILED",
            )
        capabilities = await self.client.capabilities(endpoint)
        expired = False
        result: ExternalToolProviderRead | None = None
        async with self.database.tenant_transaction(context) as session:
            provider = await self._provider(
                session,
                context,
                provider_id,
                for_update=True,
                populate_existing=True,
            )
            if provider.revision != revision:
                raise DomainConflict(
                    "TOOL_PROVIDER_REVISION_CONFLICT",
                    "Tool Provider changed during verification",
                )
            if provider.status in {
                ExternalToolProviderStatus.REVOKED.value,
                ExternalToolProviderStatus.EXPIRED.value,
            }:
                raise provider_error(
                    ToolProviderErrorCode.DISABLED,
                    "Tool Provider cannot be verified in its terminal state",
                    provider_id=provider.id,
                )
            now = datetime.now(UTC)
            if provider.expires_at is not None and provider.expires_at <= now:
                provider.status = ExternalToolProviderStatus.EXPIRED.value
                provider.revision += 1
                self._record(
                    session,
                    context,
                    event_type="tool.provider.expired",
                    action="tool.provider.expire",
                    provider=provider,
                    payload={"provider_id": str(provider.id), "status": provider.status},
                )
                await session.flush()
                expired = True
            elif provider.status == ExternalToolProviderStatus.ACTIVE.value:
                if provider.capability_digest != capabilities.capability_digest:
                    raise DomainConflict(
                        "TOOL_PROVIDER_CAPABILITY_CHANGE_REQUIRES_DISABLE",
                        "disable the active Tool Provider before refreshing capabilities",
                    )
                result = self._read(provider)
            else:
                if (
                    provider.status == ExternalToolProviderStatus.VERIFIED.value
                    and provider.capability_digest != capabilities.capability_digest
                ):
                    raise DomainConflict(
                        "TOOL_PROVIDER_CAPABILITY_CHANGE_REQUIRES_DISABLE",
                        "disable the Tool Provider before refreshing capabilities",
                    )
                provider.capability_snapshot = capabilities.model_dump(mode="json")
                provider.capability_digest = capabilities.capability_digest
                provider.verified_at = now
                provider.disabled_at = None
                provider.status = ExternalToolProviderStatus.VERIFIED.value
                provider.revision += 1
                await session.flush()
                provider.activated_at = now
                provider.status = ExternalToolProviderStatus.ACTIVE.value
                provider.revision += 1
                self._record(
                    session,
                    context,
                    event_type="tool.provider.verified",
                    action="tool.provider.verify",
                    provider=provider,
                    payload={
                        "provider_id": str(provider.id),
                        "status": provider.status,
                        "capability_digest": provider.capability_digest,
                        "supported_tool_count": len(capabilities.supported_tools),
                    },
                )
                await session.flush()
                result = self._read(provider)
        if expired:
            raise provider_error(
                ToolProviderErrorCode.EXPIRED,
                "Tool Provider expired before verification completed",
                provider_id=provider_id,
            )
        assert result is not None
        return result

    async def disable(
        self,
        context: TenantContext,
        provider_id: UUID,
    ) -> ExternalToolProviderRead:
        return await self._transition_provider(
            context,
            provider_id,
            target=ExternalToolProviderStatus.DISABLED,
            action="tool.provider.disable",
        )

    async def revoke(
        self,
        context: TenantContext,
        provider_id: UUID,
    ) -> ExternalToolProviderRead:
        return await self._transition_provider(
            context,
            provider_id,
            target=ExternalToolProviderStatus.REVOKED,
            action="tool.provider.revoke",
        )

    async def cancel_run_tree(
        self,
        context: TenantContext,
        root_run_id: UUID,
    ) -> None:
        """Best-effort Provider cancellation after authoritative Run-tree cancellation."""

        targets: list[_CancelTarget] = []
        async with self.database.tenant_transaction(context) as session:
            descendant_ids = list(
                await session.scalars(
                    select(AgentRunRelation.descendant_run_id).where(
                        AgentRunRelation.tenant_id == context.tenant_id,
                        AgentRunRelation.ancestor_run_id == root_run_id,
                    )
                )
            )
            run_ids = [root_run_id, *[value for value in descendant_ids if value != root_run_id]]
            calls = list(
                await session.scalars(
                    select(ToolCall)
                    .where(
                        ToolCall.tenant_id == context.tenant_id,
                        ToolCall.run_id.in_(run_ids),
                        ToolCall.provider_request_id.is_not(None),
                        ToolCall.provider_status.in_(["pending", "accepted", "running"]),
                    )
                    .with_for_update()
                )
            )
            binding_ids = {
                call.run_tool_binding_id for call in calls if call.run_tool_binding_id is not None
            }
            provider_ids = {call.provider_id for call in calls if call.provider_id is not None}
            call_run_ids = {call.run_id for call in calls}
            bindings = {
                binding.id: binding
                for binding in await session.scalars(
                    select(RunToolBinding)
                    .where(
                        RunToolBinding.tenant_id == context.tenant_id,
                        RunToolBinding.id.in_(binding_ids),
                    )
                    .with_for_update()
                )
            }
            providers = {
                provider.id: provider
                for provider in await session.scalars(
                    select(ExternalToolProvider).where(
                        ExternalToolProvider.tenant_id == context.tenant_id,
                        ExternalToolProvider.id.in_(provider_ids),
                    )
                )
            }
            runs = {
                run.id: run
                for run in await session.scalars(
                    select(Run).where(
                        Run.tenant_id == context.tenant_id,
                        Run.id.in_(call_run_ids),
                    )
                )
            }
            for call in calls:
                binding = bindings.get(call.run_tool_binding_id)
                provider = providers.get(call.provider_id)
                run = runs.get(call.run_id)
                if (
                    binding is None
                    or provider is None
                    or run is None
                    or call.provider_request_id is None
                ):
                    call.provider_status = "unknown"
                    call.external_execution_may_continue = True
                    call.revision += 1
                    continue
                try:
                    snapshot = self._binding_snapshot(run, binding.id)
                except ToolProviderError:
                    call.provider_status = "unknown"
                    call.external_execution_may_continue = True
                    call.revision += 1
                    binding.cancel_status = ToolProviderCancelStatus.UNKNOWN.value
                    binding.active_provider_request_id = None
                    binding.revision += 1
                    continue
                try:
                    capabilities = ProviderCapabilitiesResponse.model_validate(
                        provider.capability_snapshot
                    )
                    supports_cancellation = capabilities.features.cancellation
                except ValueError:
                    supports_cancellation = False
                deadline = datetime.now(UTC) + timedelta(
                    milliseconds=snapshot.policy.cancel_timeout_ms
                )
                target = _CancelTarget(
                    call_id=call.id,
                    run_id=call.run_id,
                    task_id=run.task_id,
                    binding_id=binding.id,
                    provider_id=provider.id,
                    request_id=call.provider_request_id,
                    tool_name=call.tool_name,
                    tool_version=call.tool_version,
                    binding_digest=binding.binding_digest,
                    endpoint=self._endpoint(provider),
                    request=ProviderCancelRequest(
                        provider_id=str(provider.id),
                        binding_digest=binding.binding_digest,
                        request_id=call.provider_request_id,
                        tenant_id=context.tenant_id,
                        project_id=binding.project_id,
                        run_id=call.run_id,
                        reason="run_cancelled",
                        deadline=deadline,
                    ),
                    timeout_seconds=snapshot.policy.cancel_timeout_ms / 1000,
                    supports_cancellation=supports_cancellation,
                )
                targets.append(target)
                self._record_cancellation(
                    session,
                    context,
                    target,
                    event_type="tool.provider.cancel.requested",
                    action="tool.provider.cancel.request",
                    status="requested",
                )

        if not targets:
            return
        outcomes: list[_CancelOutcome] = []
        batch_size = self.settings.tool_provider_cancel_max_concurrency
        for offset in range(0, len(targets), batch_size):
            outcomes.extend(
                await asyncio.gather(
                    *(
                        self._cancel_target(target)
                        for target in targets[offset : offset + batch_size]
                    )
                )
            )
        async with self.database.tenant_transaction(context) as session:
            binding_outcomes: dict[UUID, list[_CancelOutcome]] = {}
            calls_by_id = {
                call.id: call
                for call in await session.scalars(
                    select(ToolCall)
                    .where(
                        ToolCall.tenant_id == context.tenant_id,
                        ToolCall.id.in_({outcome.target.call_id for outcome in outcomes}),
                    )
                    .with_for_update()
                )
            }
            bindings_by_id = {
                binding.id: binding
                for binding in await session.scalars(
                    select(RunToolBinding)
                    .where(
                        RunToolBinding.tenant_id == context.tenant_id,
                        RunToolBinding.id.in_({outcome.target.binding_id for outcome in outcomes}),
                    )
                    .with_for_update()
                )
            }
            for outcome in outcomes:
                target = outcome.target
                call = calls_by_id.get(target.call_id)
                if call is not None:
                    call.provider_status = "cancelled" if outcome.confirmed else "unknown"
                    call.external_execution_may_continue = not outcome.confirmed
                    if outcome.provider_execution_id is not None:
                        call.provider_execution_id = outcome.provider_execution_id
                    call.revision += 1
                binding_outcomes.setdefault(target.binding_id, []).append(outcome)
                self._record_cancellation(
                    session,
                    context,
                    target,
                    event_type=(
                        "tool.provider.cancelled" if outcome.confirmed else "tool.provider.failed"
                    ),
                    action=(
                        "tool.provider.cancel"
                        if outcome.confirmed
                        else "tool.provider.cancel.failed"
                    ),
                    status="cancelled" if outcome.confirmed else "unknown",
                    error_code=(
                        None if outcome.confirmed else ToolProviderErrorCode.CANCEL_ERROR.value
                    ),
                )
            for binding_id, values in binding_outcomes.items():
                binding = bindings_by_id.get(binding_id)
                if binding is None:
                    continue
                binding.cancel_status = (
                    ToolProviderCancelStatus.CANCELLED.value
                    if all(value.confirmed for value in values)
                    else ToolProviderCancelStatus.UNKNOWN.value
                )
                binding.active_provider_request_id = None
                binding.revision += 1
            await session.flush()

    async def _cancel_target(self, target: _CancelTarget) -> _CancelOutcome:
        if not target.supports_cancellation:
            return _CancelOutcome(target=target, confirmed=False)
        try:
            async with asyncio.timeout(target.timeout_seconds):
                response = await self.client.cancel(target.endpoint, target.request)
            return _CancelOutcome(
                target=target,
                confirmed=not response.side_effects_may_continue,
                provider_execution_id=response.provider_execution_id,
            )
        except Exception:
            return _CancelOutcome(target=target, confirmed=False)

    async def register_tool_definition(
        self,
        context: TenantContext,
        spec: ToolDefinitionSpec,
    ) -> ToolDefinition:
        if spec.secret_names:
            raise DomainConflict(
                "EXTERNAL_TOOL_SECRET_DECLARATION_DENIED",
                "external Tool Definitions cannot expose Nico Tool Secrets to a Provider",
            )
        async with self.database.tenant_transaction(context) as session:
            existing = await session.scalar(
                select(ToolDefinition).where(
                    ToolDefinition.tenant_id == context.tenant_id,
                    ToolDefinition.name == spec.name,
                    ToolDefinition.version == spec.version,
                )
            )
            if existing is not None:
                if existing.content_hash != spec.content_hash:
                    raise DomainConflict(
                        "TOOL_DEFINITION_CONFLICT",
                        "the exact Tool version already has a different contract",
                    )
                return existing
            now = datetime.now(UTC)
            definition = ToolDefinition(
                tenant_id=context.tenant_id,
                name=spec.name,
                version=spec.version,
                status=ToolDefinitionStatus.ENABLED.value,
                description=spec.description,
                input_schema=spec.input_schema,
                output_schema=spec.output_schema,
                permission=spec.permission,
                timeout_seconds=spec.timeout_seconds,
                retry_policy=spec.retry_policy.model_dump(mode="json"),
                isolation_policy={
                    "kind": spec.isolation.value,
                    "secret_names": [],
                    "source": "external_provider_contract",
                },
                risk=spec.risk.value,
                max_output_bytes=spec.max_output_bytes,
                implementation_hash=hashlib.sha256(
                    f"external-contract:{spec.content_hash}".encode()
                ).hexdigest(),
                content_hash=spec.content_hash,
                created_by=context.actor_id,
                enabled_at=now,
            )
            session.add(definition)
            await session.flush()
            session.add_all(
                [
                    Event(
                        tenant_id=context.tenant_id,
                        event_type="ToolDefinitionRegistered",
                        aggregate_type="tool_definition",
                        aggregate_id=definition.id,
                        actor_id=context.actor_id,
                        payload={
                            "name": definition.name,
                            "version": definition.version,
                            "content_hash": definition.content_hash,
                            "source": "external_provider_contract",
                        },
                        correlation_id=context.correlation_id,
                    ),
                    AuditRecord(
                        tenant_id=context.tenant_id,
                        action="tool.definition.register_external_contract",
                        resource_type="tool_definition",
                        resource_id=definition.id,
                        actor_id=context.actor_id,
                        details={
                            "name": definition.name,
                            "version": definition.version,
                            "content_hash": definition.content_hash,
                        },
                        correlation_id=context.correlation_id,
                    ),
                ]
            )
            await session.flush()
            return definition

    async def preflight_run_bindings(
        self,
        context: TenantContext,
        commands: list[RunToolBindingCreate],
    ) -> dict[UUID, ProviderBindingObservation]:
        """Probe distinct Providers without holding a database transaction open."""

        provider_ids = sorted({command.provider_id for command in commands}, key=str)
        if not provider_ids:
            return {}
        observations: dict[UUID, ProviderBindingObservation] = {}
        endpoints: dict[UUID, ProviderEndpoint] = {}
        now = datetime.now(UTC)
        async with self.database.tenant_transaction(context) as session:
            for provider_id in provider_ids:
                provider = await self._provider(session, context, provider_id)
                if provider.status != ExternalToolProviderStatus.ACTIVE.value:
                    raise provider_error(
                        ToolProviderErrorCode.DISABLED,
                        "Tool Provider is not active",
                        provider_id=provider.id,
                    )
                if provider.expires_at is not None and provider.expires_at <= now:
                    raise provider_error(
                        ToolProviderErrorCode.EXPIRED,
                        "Tool Provider expired before Run creation",
                        provider_id=provider.id,
                    )
                capabilities = ProviderCapabilitiesResponse.model_validate(
                    provider.capability_snapshot
                )
                observations[provider.id] = ProviderBindingObservation.from_provider(
                    provider,
                    capabilities,
                )
                endpoints[provider.id] = self._endpoint(provider)

        batch_size = self.settings.tool_provider_cancel_max_concurrency
        for offset in range(0, len(provider_ids), batch_size):
            batch = provider_ids[offset : offset + batch_size]
            health_results = await asyncio.gather(
                *(self.client.health(endpoints[provider_id]) for provider_id in batch)
            )
            for provider_id, health in zip(batch, health_results, strict=True):
                if health.status is not ProviderHealthStatus.HEALTHY:
                    raise provider_error(
                        ToolProviderErrorCode.CONNECTION_ERROR,
                        "Tool Provider healthcheck failed during Run creation",
                        provider_id=provider_id,
                        cause="HEALTHCHECK_FAILED",
                    )
        return observations

    async def freeze_run_bindings(
        self,
        session: AsyncSession,
        context: TenantContext,
        *,
        task: Task,
        run: Run,
        version: AgentVersion,
        commands: list[RunToolBindingCreate],
        provider_observations: dict[UUID, ProviderBindingObservation],
    ) -> tuple[RunToolBinding, ...]:
        if not commands:
            run.tool_binding_snapshot = {"schema_version": "1", "bindings": []}
            return ()
        references = [command.tool.reference for command in commands]
        if len(references) != len(set(references)):
            raise DomainConflict(
                "TOOL_BINDING_DUPLICATE",
                "a Run can bind each exact Tool at most once",
            )
        tenant = await session.scalar(select(Tenant).where(Tenant.id == context.tenant_id))
        if tenant is None:
            raise ResourceNotFound("tenant", str(context.tenant_id))
        policy_snapshot = build_tool_policy_snapshot(
            tenant.settings,
            version.tool_policy,
            plugin_refs=version.plugin_refs,
        )
        now = datetime.now(UTC)
        snapshots: list[RunToolBindingSnapshot] = []
        bindings: list[RunToolBinding] = []
        providers: dict[UUID, ExternalToolProvider] = {}
        capabilities_by_provider: dict[UUID, dict[str, ProviderToolContract]] = {}
        for command in commands:
            binding_policy = command.policy.to_frozen_policy()
            definition = await session.scalar(
                select(ToolDefinition).where(
                    ToolDefinition.tenant_id == context.tenant_id,
                    ToolDefinition.name == command.tool.name,
                    ToolDefinition.version == command.tool.version,
                )
            )
            if definition is None:
                known_name = await session.scalar(
                    select(ToolDefinition.id)
                    .where(
                        ToolDefinition.tenant_id == context.tenant_id,
                        ToolDefinition.name == command.tool.name,
                    )
                    .limit(1)
                )
                raise provider_error(
                    (
                        ToolProviderErrorCode.BINDING_VERSION_MISMATCH
                        if known_name is not None
                        else ToolProviderErrorCode.BINDING_NOT_FOUND
                    ),
                    (
                        "Tool Binding version does not match a registered Tool Definition"
                        if known_name is not None
                        else "Tool Binding references an unknown Tool Definition"
                    ),
                    run_id=run.id,
                )
            if definition.status != ToolDefinitionStatus.ENABLED.value:
                raise provider_error(
                    ToolProviderErrorCode.BINDING_VERSION_MISMATCH,
                    "Tool Binding references a disabled Tool Definition",
                    run_id=run.id,
                )
            spec = tool_spec_from_definition(definition)
            authorize_tool(policy_snapshot, spec)
            provider = providers.get(command.provider_id)
            if provider is None:
                observation = provider_observations.get(command.provider_id)
                if observation is None:
                    raise DomainConflict(
                        "TOOL_PROVIDER_PREFLIGHT_MISSING",
                        "Tool Provider was not healthchecked before Run creation",
                    )
                provider = await self._provider(
                    session,
                    context,
                    command.provider_id,
                    for_update=True,
                    populate_existing=True,
                )
                locked_capabilities = ProviderCapabilitiesResponse.model_validate(
                    provider.capability_snapshot
                )
                locked_observation = ProviderBindingObservation.from_provider(
                    provider,
                    locked_capabilities,
                )
                if locked_observation != observation:
                    raise DomainConflict(
                        "TOOL_PROVIDER_REVISION_CONFLICT",
                        "Tool Provider changed during Run binding healthcheck",
                    )
                self._authorize_provider_for_run(provider, task=task, now=now)
                provider_capabilities = {
                    item.reference: item for item in locked_capabilities.supported_tools
                }
                providers[provider.id] = provider
                capabilities_by_provider[provider.id] = provider_capabilities
            provider_capabilities = capabilities_by_provider[provider.id]
            capability = provider_capabilities.get(spec.reference)
            if capability is None:
                raise provider_error(
                    ToolProviderErrorCode.CAPABILITY_MISMATCH,
                    "Tool Provider does not support the exact Tool version",
                    run_id=run.id,
                    provider_id=provider.id,
                )
            expected_input = schema_digest(spec.input_schema)
            expected_output = schema_digest(spec.output_schema)
            if (
                capability.input_schema_digest != expected_input
                or capability.output_schema_digest != expected_output
            ):
                raise provider_error(
                    ToolProviderErrorCode.SCHEMA_MISMATCH,
                    "Tool Provider schema digest does not match the Tool Definition",
                    run_id=run.id,
                    provider_id=provider.id,
                )
            if (
                binding_policy.approval is ToolBindingApprovalMode.NEVER
                and spec.risk.value in self.approval_required_risks
            ):
                raise DomainConflict(
                    "TOOL_BINDING_APPROVAL_UNENFORCEABLE",
                    "approval_mode=never conflicts with the Tool risk policy",
                )
            if (
                binding_policy.approval is ToolBindingApprovalMode.ALWAYS
                and binding_policy.budget.max_retries > 0
            ):
                raise DomainConflict(
                    "TOOL_BINDING_APPROVAL_UNENFORCEABLE",
                    "approval_mode=always requires max_attempts=1 in protocol v1",
                )
            binding_id = uuid4()
            expires_at = self._binding_expiry(
                now,
                run=run,
                provider=provider,
                requested=command.expires_at,
            )
            contract = ProviderToolContract(
                name=spec.name,
                version=spec.version,
                input_schema_digest=expected_input,
                output_schema_digest=expected_output,
            )
            snapshot = RunToolBindingSnapshot(
                binding_id=binding_id,
                provider_id=provider.id,
                provider_name=provider.name,
                endpoint_identity=provider.endpoint_identity,
                credential_ref=provider.credential_ref,
                scope=RunToolBindingScope(
                    tenant_id=context.tenant_id,
                    project_id=task.project_id,
                    run_id=run.id,
                    task_id=task.id,
                    agent_id=run.agent_id,
                    agent_version_id=run.agent_version_id,
                ),
                tool=contract,
                capability_digest=provider.capability_digest
                or ProviderCapabilitiesResponse.model_validate(
                    provider.capability_snapshot
                ).capability_digest,
                policy=binding_policy,
                provider_expires_at=provider.expires_at,
                binding_expires_at=expires_at,
                frozen_at=now,
            )
            budget = binding_policy.budget
            binding = RunToolBinding(
                id=binding_id,
                tenant_id=context.tenant_id,
                project_id=task.project_id,
                run_id=run.id,
                provider_id=provider.id,
                tool_definition_id=definition.id,
                tool_name=spec.name,
                tool_version=spec.version,
                binding_digest=snapshot.binding_digest,
                status=RunToolBindingStatus.FROZEN.value,
                policy=binding_policy.model_dump(mode="json"),
                max_calls=budget.max_calls,
                max_total_duration_ms=budget.max_total_duration_ms,
                max_single_call_duration_ms=budget.max_single_call_duration_ms,
                max_retries=budget.max_retries,
                expires_at=expires_at,
            )
            session.add(binding)
            bindings.append(binding)
            snapshots.append(snapshot)
            self._record(
                session,
                context,
                event_type="tool.binding.resolved",
                action="tool.binding.freeze",
                provider=provider,
                payload={
                    "run_id": str(run.id),
                    "task_id": str(task.id),
                    "provider_id": str(provider.id),
                    "binding_id": str(binding.id),
                    "binding_digest": binding.binding_digest,
                    "tool_name": binding.tool_name,
                    "tool_version": binding.tool_version,
                    "status": binding.status,
                },
                run_id=run.id,
                aggregate_id=binding.id,
                aggregate_type="run_tool_binding",
            )
        run.tool_binding_snapshot = {
            "schema_version": "1",
            "bindings": [
                item.model_dump(mode="json")
                for item in sorted(snapshots, key=lambda value: value.tool.reference)
            ],
        }
        return tuple(bindings)

    @staticmethod
    def _binding_snapshot(run: Run, binding_id: UUID) -> RunToolBindingSnapshot:
        values = (
            run.tool_binding_snapshot.get("bindings")
            if isinstance(run.tool_binding_snapshot, dict)
            else None
        )
        if not isinstance(values, list):
            raise provider_error(
                ToolProviderErrorCode.BINDING_IMMUTABLE,
                "Run Tool Binding Snapshot is unavailable during cancellation",
                run_id=run.id,
                cause="SNAPSHOT_MISSING",
            )
        for value in values:
            if isinstance(value, dict) and value.get("binding_id") == str(binding_id):
                try:
                    return RunToolBindingSnapshot.model_validate(value)
                except ValueError as exc:
                    raise provider_error(
                        ToolProviderErrorCode.BINDING_IMMUTABLE,
                        "Run Tool Binding Snapshot failed cancellation validation",
                        run_id=run.id,
                        cause="SNAPSHOT_INVALID",
                    ) from exc
        raise provider_error(
            ToolProviderErrorCode.BINDING_NOT_FOUND,
            "Run Tool Binding is absent from the frozen Snapshot",
            run_id=run.id,
            cause="SNAPSHOT_BINDING_MISSING",
        )

    @staticmethod
    def _record_cancellation(
        session: AsyncSession,
        context: TenantContext,
        target: _CancelTarget,
        *,
        event_type: str,
        action: str,
        status: str,
        error_code: str | None = None,
    ) -> None:
        payload = {
            "run_id": str(target.run_id),
            "task_id": str(target.task_id),
            "tool_call_id": str(target.call_id),
            "provider_id": str(target.provider_id),
            "binding_id": str(target.binding_id),
            "binding_digest": target.binding_digest,
            "request_id": target.request_id,
            "tool_name": target.tool_name,
            "tool_version": target.tool_version,
            "result_status": status,
            "error_code": error_code,
        }
        session.add_all(
            [
                Event(
                    tenant_id=context.tenant_id,
                    event_type=event_type,
                    aggregate_type="tool_call",
                    aggregate_id=target.call_id,
                    run_id=target.run_id,
                    actor_id=context.actor_id,
                    payload=payload,
                    correlation_id=context.correlation_id,
                ),
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action=action,
                    resource_type="tool_call",
                    resource_id=target.call_id,
                    actor_id=context.actor_id,
                    details=payload,
                    correlation_id=context.correlation_id,
                ),
            ]
        )

    async def _transition_provider(
        self,
        context: TenantContext,
        provider_id: UUID,
        *,
        target: ExternalToolProviderStatus,
        action: str,
    ) -> ExternalToolProviderRead:
        async with self.database.tenant_transaction(context) as session:
            provider = await self._provider(session, context, provider_id, for_update=True)
            if provider.status == target.value:
                return self._read(provider)
            if not provider_status_can_transition(provider.status, target):
                raise DomainConflict(
                    "TOOL_PROVIDER_TERMINAL",
                    "terminal Tool Provider state cannot transition",
                )
            now = datetime.now(UTC)
            provider.status = target.value
            if target is ExternalToolProviderStatus.DISABLED:
                provider.disabled_at = now
            else:
                provider.revoked_at = now
            provider.revision += 1
            self._record(
                session,
                context,
                event_type=action,
                action=action,
                provider=provider,
                payload={"provider_id": str(provider.id), "status": provider.status},
            )
            await session.flush()
            return self._read(provider)

    async def _provider(
        self,
        session: AsyncSession,
        context: TenantContext,
        provider_id: UUID,
        *,
        for_update: bool = False,
        populate_existing: bool = False,
    ) -> ExternalToolProvider:
        statement = (
            select(ExternalToolProvider)
            .outerjoin(
                Project,
                (Project.tenant_id == ExternalToolProvider.tenant_id)
                & (Project.id == ExternalToolProvider.project_id),
            )
            .where(
                ExternalToolProvider.tenant_id == context.tenant_id,
                ExternalToolProvider.id == provider_id,
                or_(
                    ExternalToolProvider.project_id.is_(None),
                    Project.kind == "shared",
                    Project.owner_actor_id == context.actor_id,
                ),
            )
        )
        if for_update:
            statement = statement.with_for_update(of=ExternalToolProvider)
        if populate_existing:
            statement = statement.execution_options(populate_existing=True)
        provider = await session.scalar(statement)
        if provider is None:
            raise provider_error(
                ToolProviderErrorCode.NOT_FOUND,
                "Tool Provider was not found",
                provider_id=provider_id,
            )
        return provider

    async def _require_project(
        self,
        session: AsyncSession,
        context: TenantContext,
        project_id: UUID | None,
    ) -> None:
        if project_id is None:
            return
        project = await session.scalar(
            select(Project).where(
                Project.tenant_id == context.tenant_id,
                Project.id == project_id,
                or_(Project.kind == "shared", Project.owner_actor_id == context.actor_id),
            )
        )
        if project is None:
            raise ResourceNotFound("project", str(project_id))
        if project.status != "active":
            raise DomainConflict("PROJECT_ARCHIVED", "archived Project cannot own a Provider")

    def _endpoint_policy(self, endpoint_ref: str) -> dict[str, bool]:
        parsed = urlsplit(endpoint_ref)
        hostname = _canonical_endpoint_hostname(parsed.hostname or "")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        loopback = (
            hostname == "localhost"
            or hostname.endswith(".localhost")
            or (address is not None and address.is_loopback)
        )
        trusted_private = hostname in {
            _canonical_endpoint_hostname(item)
            for item in self.settings.tool_provider_trusted_private_hosts
        }
        allow_loopback = self.settings.tool_provider_allow_http_loopback and loopback
        allow_private = trusted_private
        allow_http = allow_loopback or (
            trusted_private and self.settings.tool_provider_allow_http_trusted_hosts
        )
        if parsed.scheme == "http" and not allow_http:
            raise provider_error(
                ToolProviderErrorCode.PERMISSION_DENIED,
                "Tool Provider endpoint requires HTTPS",
                cause="HTTP_ENDPOINT_DENIED",
            )
        return {
            "allow_http": allow_http,
            "allow_loopback": allow_loopback,
            "allow_private": allow_private,
        }

    @staticmethod
    def _endpoint(provider: ExternalToolProvider) -> ProviderEndpoint:
        policy = provider.endpoint_policy
        return ProviderEndpoint(
            provider_id=provider.id,
            endpoint_ref=provider.endpoint_url,
            credential_ref=provider.credential_ref,
            allow_http=bool(policy.get("allow_http", False)),
            allow_loopback=bool(policy.get("allow_loopback", False)),
            allow_private=bool(policy.get("allow_private", False)),
        )

    def _binding_expiry(
        self,
        now: datetime,
        *,
        run: Run,
        provider: ExternalToolProvider,
        requested: datetime | None,
    ) -> datetime:
        candidates = [
            now + timedelta(seconds=self.settings.tool_provider_max_binding_lifetime_seconds)
        ]
        if run.timeout_seconds is not None:
            candidates.append(now + timedelta(seconds=run.timeout_seconds))
        if provider.expires_at is not None:
            candidates.append(provider.expires_at)
        if requested is not None:
            candidates.append(requested)
        expiry = min(candidates)
        if expiry <= now:
            raise provider_error(
                ToolProviderErrorCode.EXPIRED,
                "Tool Binding expiry must be in the future",
                run_id=run.id,
                provider_id=provider.id,
            )
        return expiry

    @staticmethod
    def _authorize_provider_for_run(
        provider: ExternalToolProvider,
        *,
        task: Task,
        now: datetime,
    ) -> None:
        if provider.status != ExternalToolProviderStatus.ACTIVE.value:
            raise provider_error(
                ToolProviderErrorCode.DISABLED,
                "Tool Provider is not active",
                provider_id=provider.id,
                run_id=None,
            )
        if provider.project_id is not None and provider.project_id != task.project_id:
            raise provider_error(
                ToolProviderErrorCode.SCOPE_MISMATCH,
                "Tool Provider Project scope does not match the Run",
                provider_id=provider.id,
            )
        if provider.expires_at is not None and provider.expires_at <= now:
            raise provider_error(
                ToolProviderErrorCode.EXPIRED,
                "Tool Provider has expired",
                provider_id=provider.id,
            )

    @staticmethod
    def _read(provider: ExternalToolProvider) -> ExternalToolProviderRead:
        return ExternalToolProviderRead(
            id=provider.id,
            tenant_id=provider.tenant_id,
            project_id=provider.project_id,
            name=provider.name,
            protocol=provider.protocol,
            endpoint_ref=provider.endpoint_url,
            endpoint_identity=provider.endpoint_identity,
            credential_ref=_redacted_credential_ref(provider.credential_ref),
            status=provider.status,
            capability_snapshot=provider.capability_snapshot,
            capability_digest=provider.capability_digest,
            verified_at=provider.verified_at,
            expires_at=provider.expires_at,
            revision=provider.revision,
            metadata=provider.metadata_json,
            created_at=provider.created_at,
            updated_at=provider.updated_at,
        )

    @staticmethod
    def _record(
        session: AsyncSession,
        context: TenantContext,
        *,
        event_type: str,
        action: str,
        provider: ExternalToolProvider,
        payload: dict[str, Any],
        run_id: UUID | None = None,
        aggregate_id: UUID | None = None,
        aggregate_type: str = "external_tool_provider",
    ) -> None:
        resource_id = aggregate_id or provider.id
        session.add_all(
            [
                Event(
                    tenant_id=context.tenant_id,
                    event_type=event_type,
                    aggregate_type=aggregate_type,
                    aggregate_id=resource_id,
                    run_id=run_id,
                    actor_id=context.actor_id,
                    payload=payload,
                    correlation_id=context.correlation_id,
                ),
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action=action,
                    resource_type=aggregate_type,
                    resource_id=resource_id,
                    actor_id=context.actor_id,
                    details=payload,
                    correlation_id=context.correlation_id,
                ),
            ]
        )


def tool_spec_from_definition(definition: ToolDefinition) -> ToolDefinitionSpec:
    isolation = definition.isolation_policy
    return ToolDefinitionSpec(
        name=definition.name,
        version=definition.version,
        description=definition.description,
        input_schema=definition.input_schema,
        output_schema=definition.output_schema,
        permission=definition.permission,
        timeout_seconds=definition.timeout_seconds,
        retry_policy=ToolRetryPolicy.model_validate(definition.retry_policy),
        isolation=ToolIsolation(isolation.get("kind", ToolIsolation.NETWORK.value)),
        risk=ToolRisk(definition.risk),
        max_output_bytes=definition.max_output_bytes,
        secret_names=frozenset(isolation.get("secret_names", [])),
    )


def _redacted_credential_ref(reference: str) -> str:
    prefix = reference.partition(":")[0]
    return f"{prefix}:[REDACTED]"


def _canonical_endpoint_hostname(value: str) -> str:
    hostname = value.rstrip(".").lower()
    try:
        return ipaddress.ip_address(hostname).compressed
    except ValueError:
        return hostname.encode("idna").decode("ascii")
