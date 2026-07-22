"""Tenant-scoped Provider probe application service."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.agent_versions import AgentVersionLifecycle
from nico_agent.api_schemas import AgentVersionCreate
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainConflict, ResourceNotFound
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    AuditRecord,
    ModelEndpoint,
    Project,
    ProviderProbe,
    Tenant,
)
from nico_agent.provider_onboarding.catalog import get_provider_catalog
from nico_agent.provider_onboarding.contracts import (
    CandidateConfiguration,
    CustomProviderOptions,
    ProviderActivationCreate,
    ProviderActivationPreview,
    ProviderActivationRead,
    ProviderConnectionRead,
    ProviderPreviewCreate,
    ProviderProbeCreate,
    ProviderSetupReadiness,
    canonical_candidate_hash,
)

_PREVIEW_TTL = timedelta(minutes=15)
_PROJECT_COORDINATION_POLICY = {
    "enabled": True,
    "allowed_target_scopes": ["project_members"],
    "allowed_agent_version_ids": [],
    "allowed_secret_refs": [],
    "max_depth": 4,
    "max_children": 16,
    "max_parallelism": 4,
}


class ProviderOnboardingService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create_probe(
        self,
        context: TenantContext,
        command: ProviderProbeCreate,
    ) -> ProviderProbe:
        self._validate_candidate(command.candidate)
        candidate_hash = canonical_candidate_hash(command.candidate)
        async with self.database.tenant_transaction(context) as session:
            tenant_id = await session.scalar(
                select(Tenant.id).where(Tenant.id == context.tenant_id)
            )
            if tenant_id is None:
                raise ResourceNotFound("tenant", str(context.tenant_id))
            await self._validate_custom_binding(session, context, command.candidate)
            existing = await session.scalar(
                select(ProviderProbe).where(
                    ProviderProbe.tenant_id == context.tenant_id,
                    ProviderProbe.idempotency_key == command.idempotency_key,
                )
            )
            if existing is not None:
                if existing.kind != command.kind or existing.candidate_hash != candidate_hash:
                    raise DomainConflict(
                        "PROVIDER_PROBE_IDEMPOTENCY_CONFLICT",
                        "provider probe idempotency key was already used for another candidate",
                    )
                return existing
            candidate = command.candidate
            probe = ProviderProbe(
                tenant_id=context.tenant_id,
                kind=command.kind,
                provider_key=candidate.provider_key,
                protocol=candidate.protocol,
                base_url=candidate.base_url,
                credential_ref=candidate.credential_ref,
                provider_options=candidate.provider_options,
                model_name=candidate.model,
                catalog_revision=candidate.catalog_revision,
                candidate_hash=candidate_hash,
                idempotency_key=command.idempotency_key,
            )
            session.add(probe)
            await session.flush()
            session.add(
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action="provider_probe.create",
                    resource_type="provider_probe",
                    resource_id=probe.id,
                    actor_id=context.actor_id,
                    details={
                        "kind": probe.kind,
                        "provider_key": probe.provider_key,
                        "candidate_hash": probe.candidate_hash,
                    },
                    correlation_id=context.correlation_id,
                )
            )
            await session.flush()
            return probe

    async def get_probe(self, context: TenantContext, probe_id: UUID) -> ProviderProbe:
        async with self.database.tenant_transaction(context) as session:
            probe = await session.scalar(
                select(ProviderProbe).where(
                    ProviderProbe.tenant_id == context.tenant_id,
                    ProviderProbe.id == probe_id,
                )
            )
            if probe is None:
                raise ResourceNotFound("provider_probe", str(probe_id))
            return probe

    async def cancel_probe(self, context: TenantContext, probe_id: UUID) -> ProviderProbe:
        async with self.database.tenant_transaction(context) as session:
            probe = await session.scalar(
                select(ProviderProbe)
                .where(
                    ProviderProbe.tenant_id == context.tenant_id,
                    ProviderProbe.id == probe_id,
                )
                .with_for_update()
            )
            if probe is None:
                raise ResourceNotFound("provider_probe", str(probe_id))
            if probe.status == "cancelled":
                return probe
            if probe.status not in {"pending", "running"}:
                raise DomainConflict(
                    "PROVIDER_PROBE_TERMINAL",
                    "completed provider probes cannot be cancelled",
                )
            probe.status = "cancelled"
            probe.completed_at = datetime.now(UTC)
            probe.lease_token = None
            probe.lease_expires_at = None
            probe.revision += 1
            session.add(
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action="provider_probe.cancel",
                    resource_type="provider_probe",
                    resource_id=probe.id,
                    actor_id=context.actor_id,
                    details={"candidate_hash": probe.candidate_hash},
                    correlation_id=context.correlation_id,
                )
            )
            await session.flush()
            return probe

    async def preview_activation(
        self,
        context: TenantContext,
        command: ProviderPreviewCreate,
    ) -> ProviderActivationPreview:
        async with self.database.tenant_transaction(context) as session:
            probe = await self._probe(session, context, command.probe_id)
            return await self._build_preview(session, context, probe, command)

    async def activate(
        self,
        context: TenantContext,
        command: ProviderActivationCreate,
    ) -> ProviderActivationRead:
        async with self.database.tenant_transaction(context) as session:
            probe = await self._probe(
                session,
                context,
                command.probe_id,
                for_update=True,
            )
            if probe.status == "activated":
                return await self._activated_result(session, context, probe, command.preview_hash)
            self._validate_verified_probe(probe, command.candidate_hash)
            await session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtextextended(
                            f"{context.tenant_id}:provider:{probe.provider_key}",
                            0,
                        )
                    )
                )
            )
            preview = await self._build_preview(session, context, probe, command, lock_target=True)
            if preview.preview_hash != command.preview_hash:
                raise DomainConflict(
                    "PROVIDER_PREVIEW_STALE",
                    "the Provider activation preview changed; review it again",
                )
            if command.maintenance_attempt_id is not None:
                active = await session.scalar(
                    text("SELECT provider_maintenance_attempt_active(:attempt_id)"),
                    {"attempt_id": command.maintenance_attempt_id},
                )
                if not active:
                    raise DomainConflict(
                        "PROVIDER_MAINTENANCE_LOST",
                        "the local Provider maintenance lease is no longer active",
                    )

            projection = preview.projection
            tenant_projection = projection.get("tenant")
            if isinstance(tenant_projection, dict):
                tenant = await session.scalar(
                    select(Tenant).where(Tenant.id == context.tenant_id).with_for_update()
                )
                if tenant is None:
                    raise ResourceNotFound("tenant", str(context.tenant_id))
                tenant.settings = dict(tenant_projection["settings"])
                tenant.revision += 1
                session.add(
                    AuditRecord(
                        tenant_id=context.tenant_id,
                        action="tenant.coordination.enable",
                        resource_type="tenant",
                        resource_id=tenant.id,
                        actor_id=context.actor_id,
                        details={
                            "source": "provider_onboarding",
                            "allowed_target_scopes": ["project_members"],
                            "revision": tenant.revision,
                        },
                        correlation_id=context.correlation_id,
                    )
                )
            endpoint_projection = projection["endpoint"]
            endpoint = await session.scalar(
                select(ModelEndpoint).where(
                    ModelEndpoint.tenant_id == context.tenant_id,
                    ModelEndpoint.id == UUID(endpoint_projection["id"]),
                )
            )
            endpoint_reused = bool(endpoint_projection["reused"])
            if endpoint is None:
                endpoint = ModelEndpoint(
                    id=UUID(endpoint_projection["id"]),
                    tenant_id=context.tenant_id,
                    stable_key=endpoint_projection["stable_key"],
                    revision=endpoint_projection["revision"],
                    display_name=endpoint_projection["display_name"],
                    protocol=endpoint_projection["protocol"],
                    base_url=endpoint_projection["base_url"],
                    credential_ref=endpoint_projection["credential_ref"],
                    provider_key=endpoint_projection["provider_key"],
                    catalog_revision=endpoint_projection["catalog_revision"],
                    provider_options=endpoint_projection["provider_options"],
                    verified_probe_id=probe.id,
                    verified_at=probe.verified_at,
                    allowed_models=endpoint_projection["allowed_models"],
                    capabilities=endpoint_projection["capabilities"],
                    rate_limit={},
                    tls_policy={},
                    enabled=True,
                    status="active",
                )
                session.add(endpoint)
                await session.flush()
                session.add(
                    AuditRecord(
                        tenant_id=context.tenant_id,
                        action="model_endpoint.activate",
                        resource_type="model_endpoint",
                        resource_id=endpoint.id,
                        actor_id=context.actor_id,
                        details={
                            "stable_key": endpoint.stable_key,
                            "revision": endpoint.revision,
                            "probe_id": str(probe.id),
                        },
                        correlation_id=context.correlation_id,
                    )
                )

            agent_projection = projection["agent"]
            agent = await session.scalar(
                select(Agent)
                .where(
                    Agent.tenant_id == context.tenant_id,
                    Agent.id == UUID(agent_projection["id"]),
                )
                .with_for_update()
            )
            if agent is None:
                agent = Agent(
                    id=UUID(agent_projection["id"]),
                    tenant_id=context.tenant_id,
                    name=agent_projection["name"],
                    display_name=agent_projection["display_name"],
                    description="Created by guided Provider setup.",
                )
                session.add(agent)
                await session.flush()
                session.add(
                    AuditRecord(
                        tenant_id=context.tenant_id,
                        action="agent.create",
                        resource_type="agent",
                        resource_id=agent.id,
                        actor_id=context.actor_id,
                        details={"name": agent.name, "source": "provider_onboarding"},
                        correlation_id=context.correlation_id,
                    )
                )

            version_command = AgentVersionCreate.model_validate(
                projection["agent_version"]["command"]
            )
            lifecycle = AgentVersionLifecycle(session, context)
            expected_version_id = UUID(projection["agent_version"]["id"])
            version = await lifecycle.create(
                agent,
                version_command,
                version_id=expected_version_id,
            )
            expected_revision = command.target.expected_agent_revision or agent.revision
            await lifecycle.publish(
                agent,
                version.id,
                expected_revision=expected_revision,
            )

            activated_at = datetime.now(UTC)
            probe.status = "activated"
            probe.activated_at = activated_at
            probe.activation_correlation_id = context.correlation_id
            probe.revision += 1
            result = ProviderActivationRead(
                probe_id=probe.id,
                candidate_hash=probe.candidate_hash,
                endpoint_id=endpoint.id,
                endpoint_revision=endpoint.revision,
                endpoint_reused=endpoint_reused,
                agent_id=agent.id,
                agent_revision=agent.revision,
                agent_version_id=version.id,
                agent_version=version.version,
                project_id=command.target.project_id,
                activated_at=activated_at,
            )
            session.add(
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action="provider_activation.complete",
                    resource_type="provider_probe",
                    resource_id=probe.id,
                    actor_id=context.actor_id,
                    details={
                        "preview_hash": preview.preview_hash,
                        "result": result.model_dump(mode="json"),
                    },
                    correlation_id=context.correlation_id,
                )
            )
            await session.flush()
            return result

    async def list_connections(
        self,
        context: TenantContext,
    ) -> list[ProviderConnectionRead]:
        async with self.database.tenant_transaction(context) as session:
            latest_revisions = (
                select(
                    ModelEndpoint.stable_key,
                    func.max(ModelEndpoint.revision).label("revision"),
                )
                .where(ModelEndpoint.tenant_id == context.tenant_id)
                .group_by(ModelEndpoint.stable_key)
                .subquery()
            )
            endpoints = list(
                await session.scalars(
                    select(ModelEndpoint)
                    .join(
                        latest_revisions,
                        (latest_revisions.c.stable_key == ModelEndpoint.stable_key)
                        & (latest_revisions.c.revision == ModelEndpoint.revision),
                    )
                    .where(ModelEndpoint.tenant_id == context.tenant_id)
                    .order_by(ModelEndpoint.stable_key)
                )
            )
            active_rows = list(
                (
                    await session.execute(
                        select(Agent, AgentVersion)
                        .join(
                            AgentVersion,
                            (AgentVersion.tenant_id == Agent.tenant_id)
                            & (AgentVersion.agent_id == Agent.id)
                            & (AgentVersion.id == Agent.current_version_id),
                        )
                        .where(Agent.tenant_id == context.tenant_id)
                    )
                ).all()
            )
            agents_by_endpoint: dict[UUID, list[dict[str, Any]]] = {}
            for agent, version in active_rows:
                if version.model_endpoint_id is not None:
                    agents_by_endpoint.setdefault(version.model_endpoint_id, []).append(
                        {
                            "id": str(agent.id),
                            "name": agent.name,
                            "display_name": agent.display_name,
                            "status": agent.status,
                            "model": version.model_name,
                        }
                    )
            return [
                ProviderConnectionRead(
                    endpoint_id=endpoint.id,
                    stable_key=endpoint.stable_key,
                    provider_key=endpoint.provider_key,
                    display_name=endpoint.display_name,
                    protocol=endpoint.protocol,
                    base_url=endpoint.base_url,
                    credential_ref=endpoint.credential_ref,
                    catalog_revision=endpoint.catalog_revision,
                    provider_options=endpoint.provider_options,
                    revision=endpoint.revision,
                    status=endpoint.status,
                    enabled=endpoint.enabled,
                    verified=endpoint.verified_at is not None,
                    verified_at=endpoint.verified_at,
                    allowed_models=tuple(endpoint.allowed_models),
                    active_agents=tuple(agents_by_endpoint.get(endpoint.id, [])),
                )
                for endpoint in endpoints
            ]

    async def setup_readiness(self, context: TenantContext) -> ProviderSetupReadiness:
        async with self.database.tenant_transaction(context) as session:
            project_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Project)
                    .where(
                        Project.tenant_id == context.tenant_id,
                        Project.status == "active",
                    )
                )
                or 0
            )
            agent_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Agent)
                    .where(Agent.tenant_id == context.tenant_id)
                )
                or 0
            )
            route_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(Agent)
                    .join(
                        AgentVersion,
                        (AgentVersion.tenant_id == Agent.tenant_id)
                        & (AgentVersion.agent_id == Agent.id)
                        & (AgentVersion.id == Agent.current_version_id),
                    )
                    .join(
                        ModelEndpoint,
                        (ModelEndpoint.tenant_id == AgentVersion.tenant_id)
                        & (ModelEndpoint.id == AgentVersion.model_endpoint_id),
                    )
                    .where(
                        Agent.tenant_id == context.tenant_id,
                        Agent.status == "ready",
                        AgentVersion.runtime_provider == "nico_native",
                        ModelEndpoint.enabled.is_(True),
                        ModelEndpoint.status == "active",
                        ModelEndpoint.verified_at.is_not(None),
                    )
                )
                or 0
            )
            reason = "ready"
            if project_count == 0:
                reason = "project_required"
            elif route_count == 0:
                reason = "verified_native_route_required"
            return ProviderSetupReadiness(
                needs_setup=route_count == 0,
                reason=reason,
                project_count=project_count,
                agent_count=agent_count,
                verified_native_route_count=route_count,
            )

    async def _build_preview(
        self,
        session: AsyncSession,
        context: TenantContext,
        probe: ProviderProbe,
        command: ProviderPreviewCreate,
        *,
        lock_target: bool = False,
    ) -> ProviderActivationPreview:
        self._validate_verified_probe(probe, command.candidate_hash)
        project_statement = select(Project).where(
            Project.tenant_id == context.tenant_id,
            Project.id == command.target.project_id,
            Project.status == "active",
        )
        if lock_target:
            project_statement = project_statement.with_for_update()
        project = await session.scalar(project_statement)
        if project is None:
            raise ResourceNotFound("project", str(command.target.project_id))

        preset = next(
            (item for item in get_provider_catalog().providers if item.key == probe.provider_key),
            None,
        )
        if preset is None:
            custom = CustomProviderOptions.model_validate(probe.provider_options)
            display_name = custom.nico_custom_display_name
            capabilities = {"streaming": True, "tools": True}
        else:
            display_name = preset.display_name
            capabilities = preset.capabilities
        endpoint, endpoint_projection = await self._endpoint_projection(
            session,
            context,
            probe,
            display_name=display_name,
            capabilities=capabilities,
        )
        agent, agent_projection = await self._agent_projection(
            session,
            context,
            probe,
            command,
            lock_target=lock_target,
        )
        lifecycle = AgentVersionLifecycle(session, context)
        if agent is not None and agent.current_version_id is not None:
            current = await lifecycle.version(agent.id, agent.current_version_id)
            version_command = lifecycle.command_from_version(
                current,
                model_endpoint_id=UUID(endpoint_projection["id"]),
                model_name=str(probe.model_name),
            )
        else:
            version_command = AgentVersionCreate(
                role="assistant",
                mandate="Help the user safely and accurately.",
                runtime_provider="nico_native",
                execution_mode="react",
                model_endpoint_id=UUID(endpoint_projection["id"]),
                model_name=str(probe.model_name),
                coordination_policy=dict(_PROJECT_COORDINATION_POLICY),
            )
        latest_version = await session.scalar(
            select(func.max(AgentVersion.version)).where(
                AgentVersion.tenant_id == context.tenant_id,
                AgentVersion.agent_id == UUID(agent_projection["id"]),
            )
        )
        version_id = uuid5(
            NAMESPACE_URL,
            f"nico:provider-version:{context.tenant_id}:{probe.id}:{agent_projection['id']}",
        )
        runtime_provider = lifecycle.runtime_provider(version_command)
        version_projection = {
            "id": str(version_id),
            "version": (latest_version or 0) + 1,
            "content_hash": lifecycle.content_hash(
                version_command,
                runtime_provider=runtime_provider,
            ),
            "command": version_command.model_dump(mode="json", by_alias=True),
        }
        projection = {
            "schema_version": 1,
            "project": {"id": str(project.id), "name": project.name},
            "endpoint": endpoint_projection,
            "agent": agent_projection,
            "agent_version": version_projection,
        }
        changed_fields = ["agent.current_version_id", "agent.status", "agent.revision"]
        if agent is None:
            tenant_statement = select(Tenant).where(Tenant.id == context.tenant_id)
            if lock_target:
                tenant_statement = tenant_statement.with_for_update()
            tenant = await session.scalar(tenant_statement)
            if tenant is None:
                raise ResourceNotFound("tenant", str(context.tenant_id))
            settings = dict(tenant.settings or {})
            current_policy = settings.get("coordination_policy", {})
            merged_policy = dict(current_policy) if isinstance(current_policy, dict) else {}
            scopes = {
                str(item)
                for item in merged_policy.get("allowed_target_scopes", [])
                if isinstance(item, str)
            }
            scopes.add("project_members")
            merged_policy.update(
                {
                    "enabled": True,
                    "allowed_target_scopes": sorted(scopes),
                    "max_depth": merged_policy.get("max_depth") or 4,
                    "max_children": merged_policy.get("max_children") or 16,
                    "max_parallelism": merged_policy.get("max_parallelism") or 4,
                }
            )
            settings["coordination_policy"] = merged_policy
            projection["tenant"] = {"id": str(tenant.id), "settings": settings}
            changed_fields.extend(["tenant.settings.coordination_policy", "tenant.revision"])
        if endpoint is None:
            changed_fields.insert(0, "model_endpoint")
        changed_fields.extend(["agent_version", "provider_probe.status"])
        preview_hash = self._preview_hash(
            probe.id,
            probe.candidate_hash,
            projection,
        )
        return ProviderActivationPreview(
            probe_id=probe.id,
            candidate_hash=probe.candidate_hash,
            preview_hash=preview_hash,
            expires_at=probe.verified_at + _PREVIEW_TTL,
            changed_fields=tuple(changed_fields),
            projection=projection,
        )

    async def _endpoint_projection(
        self,
        session: AsyncSession,
        context: TenantContext,
        probe: ProviderProbe,
        *,
        display_name: str,
        capabilities: dict[str, bool],
    ) -> tuple[ModelEndpoint | None, dict[str, Any]]:
        latest = await session.scalar(
            select(ModelEndpoint)
            .where(
                ModelEndpoint.tenant_id == context.tenant_id,
                ModelEndpoint.stable_key == probe.provider_key,
            )
            .order_by(ModelEndpoint.revision.desc())
            .limit(1)
        )
        semantics = {
            "stable_key": probe.provider_key,
            "display_name": display_name,
            "protocol": probe.protocol,
            "base_url": probe.base_url,
            "credential_ref": probe.credential_ref,
            "provider_key": probe.provider_key,
            "catalog_revision": probe.catalog_revision,
            "provider_options": dict(probe.provider_options),
            "allowed_models": [str(probe.model_name)],
            "capabilities": dict(capabilities),
        }
        reused = (
            latest is not None
            and latest.enabled
            and latest.status == "active"
            and all(getattr(latest, key) == value for key, value in semantics.items())
        )
        endpoint = latest if reused else None
        endpoint_id = (
            latest.id
            if reused
            else uuid5(
                NAMESPACE_URL,
                f"nico:provider-endpoint:{context.tenant_id}:{probe.id}",
            )
        )
        return endpoint, {
            "id": str(endpoint_id),
            "revision": latest.revision if reused else (latest.revision + 1 if latest else 1),
            "reused": reused,
            **semantics,
        }

    async def _agent_projection(
        self,
        session: AsyncSession,
        context: TenantContext,
        probe: ProviderProbe,
        command: ProviderPreviewCreate,
        *,
        lock_target: bool,
    ) -> tuple[Agent | None, dict[str, Any]]:
        target = command.target
        if target.agent_id is not None:
            statement = select(Agent).where(
                Agent.tenant_id == context.tenant_id,
                Agent.id == target.agent_id,
            )
            if lock_target:
                statement = statement.with_for_update()
            agent = await session.scalar(statement)
            if agent is None:
                raise ResourceNotFound("agent", str(target.agent_id))
            if agent.status == "archived":
                raise DomainConflict("AGENT_ARCHIVED", "restore the Agent before activation")
            if agent.revision != target.expected_agent_revision:
                raise DomainConflict(
                    "REVISION_CONFLICT",
                    "agent revision is "
                    f"{agent.revision}, expected {target.expected_agent_revision}",
                )
            return agent, {
                "id": str(agent.id),
                "name": agent.name,
                "display_name": agent.display_name,
                "existing": True,
                "expected_revision": target.expected_agent_revision,
            }

        assert target.starter_agent_name is not None
        existing_name = await session.scalar(
            select(Agent.id).where(
                Agent.tenant_id == context.tenant_id,
                Agent.name == target.starter_agent_name,
            )
        )
        if existing_name is not None:
            raise DomainConflict(
                "PROVIDER_STARTER_AGENT_EXISTS",
                "the requested Starter Agent name is already in use",
            )
        agent_id = uuid5(
            NAMESPACE_URL,
            f"nico:provider-agent:{context.tenant_id}:{probe.id}:{target.starter_agent_name}",
        )
        return None, {
            "id": str(agent_id),
            "name": target.starter_agent_name,
            "display_name": target.starter_agent_display_name,
            "existing": False,
            "expected_revision": 1,
        }

    @staticmethod
    async def _probe(
        session: AsyncSession,
        context: TenantContext,
        probe_id: UUID,
        *,
        for_update: bool = False,
    ) -> ProviderProbe:
        statement = select(ProviderProbe).where(
            ProviderProbe.tenant_id == context.tenant_id,
            ProviderProbe.id == probe_id,
        )
        if for_update:
            statement = statement.with_for_update()
        probe = await session.scalar(statement)
        if probe is None:
            raise ResourceNotFound("provider_probe", str(probe_id))
        return probe

    @staticmethod
    def _validate_verified_probe(probe: ProviderProbe, candidate_hash: str) -> None:
        if probe.candidate_hash != candidate_hash:
            raise DomainConflict(
                "PROVIDER_CANDIDATE_MISMATCH",
                "the Provider candidate no longer matches its verification",
            )
        if probe.kind != "verify_completion" or probe.status != "succeeded":
            raise DomainConflict(
                "PROVIDER_PROBE_NOT_VERIFIED",
                "activation requires one successful completion verification",
            )
        if probe.verified_at is None or probe.verified_at + _PREVIEW_TTL <= datetime.now(UTC):
            raise DomainConflict(
                "PROVIDER_PROBE_EXPIRED",
                "the Provider verification expired; test the connection again",
            )
        candidate = CandidateConfiguration(
            provider_key=probe.provider_key,
            protocol=probe.protocol,
            base_url=probe.base_url,
            credential_ref=probe.credential_ref,
            model=probe.model_name,
            provider_options=probe.provider_options,
            catalog_revision=probe.catalog_revision,
        )
        ProviderOnboardingService._validate_candidate(candidate)
        if canonical_candidate_hash(candidate) != probe.candidate_hash:
            raise DomainConflict(
                "PROVIDER_CANONICALIZATION_MISMATCH",
                "the Provider verification uses an unsupported canonical form",
            )

    @staticmethod
    def _preview_hash(probe_id: UUID, candidate_hash: str, projection: dict[str, Any]) -> str:
        payload = {
            "schema_version": 1,
            "probe_id": str(probe_id),
            "candidate_hash": candidate_hash,
            "projection": projection,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    async def _activated_result(
        session: AsyncSession,
        context: TenantContext,
        probe: ProviderProbe,
        preview_hash: str,
    ) -> ProviderActivationRead:
        audit = await session.scalar(
            select(AuditRecord)
            .where(
                AuditRecord.tenant_id == context.tenant_id,
                AuditRecord.resource_type == "provider_probe",
                AuditRecord.resource_id == probe.id,
                AuditRecord.action == "provider_activation.complete",
            )
            .order_by(AuditRecord.created_at.desc())
            .limit(1)
        )
        if audit is None or audit.details.get("preview_hash") != preview_hash:
            raise DomainConflict(
                "PROVIDER_PROBE_CONSUMED",
                "the Provider verification was already activated",
            )
        return ProviderActivationRead.model_validate(audit.details["result"])

    @staticmethod
    def _validate_candidate(candidate: CandidateConfiguration) -> None:
        catalog = get_provider_catalog()
        if candidate.catalog_revision != catalog.catalog_revision:
            raise DomainConflict(
                "PROVIDER_CATALOG_STALE",
                "provider candidate uses a stale catalog revision",
            )
        provider = next(
            (item for item in catalog.providers if item.key == candidate.provider_key),
            None,
        )
        if provider is None:
            if candidate.provider_key.startswith("custom-"):
                try:
                    CustomProviderOptions.model_validate(candidate.provider_options)
                except ValueError as exc:
                    raise DomainConflict(
                        "PROVIDER_OPTIONS_INVALID",
                        "custom provider contains unsupported options",
                    ) from exc
                ProviderOnboardingService._validate_custom_credential_scope(candidate)
                return
            raise DomainConflict("PROVIDER_NOT_FOUND", "provider preset is not available")
        if candidate.protocol != provider.protocol:
            raise DomainConflict(
                "PROVIDER_PROTOCOL_ERROR",
                "provider candidate protocol does not match the catalog",
            )
        if candidate.base_url not in {location.base_url for location in provider.locations}:
            raise DomainConflict(
                "PROVIDER_LOCATION_INVALID",
                "provider candidate service location is not in the catalog",
            )
        allowed_options = {option.key for option in provider.options}
        unknown_options = sorted(set(candidate.provider_options) - allowed_options)
        if unknown_options:
            raise DomainConflict(
                "PROVIDER_OPTIONS_INVALID",
                "provider candidate contains unsupported options",
                details={"option_keys": unknown_options},
            )

    @staticmethod
    def _validate_custom_credential_scope(candidate: CandidateConfiguration) -> None:
        label = re.sub(r"[^A-Z0-9]", "_", candidate.provider_key.upper())
        env_prefix = f"env:NICO_MODEL_SECRET_{label}_"
        secret_scope = f"secret:providers/{candidate.provider_key}"
        if not (
            candidate.credential_ref.startswith(env_prefix)
            or candidate.credential_ref == secret_scope
            or candidate.credential_ref.startswith(f"{secret_scope}/")
        ):
            raise DomainConflict(
                "PROVIDER_CREDENTIAL_SCOPE_INVALID",
                "custom Providers require a dedicated local credential created for their key",
            )

    @staticmethod
    async def _validate_custom_binding(
        session: AsyncSession,
        context: TenantContext,
        candidate: CandidateConfiguration,
    ) -> None:
        if not candidate.provider_key.startswith("custom-"):
            return
        bound = await session.scalar(
            select(ModelEndpoint)
            .where(
                ModelEndpoint.tenant_id == context.tenant_id,
                ModelEndpoint.stable_key == candidate.provider_key,
            )
            .order_by(ModelEndpoint.revision.desc())
            .limit(1)
        )
        if bound is not None and (
            bound.protocol != candidate.protocol
            or bound.base_url != candidate.base_url
            or bound.credential_ref != candidate.credential_ref
        ):
            raise DomainConflict(
                "PROVIDER_CUSTOM_BINDING_CONFLICT",
                "custom Provider keys are permanently bound to one endpoint and credential",
            )
