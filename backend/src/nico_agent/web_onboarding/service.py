"""Transactional Web Provider onboarding and AgentVersion publication."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.agent_versions import AgentVersionLifecycle
from nico_agent.api_schemas import AgentVersionCreate
from nico_agent.config import Settings
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
from nico_agent.web_onboarding.catalog import get_web_provider_catalog, provider_preset
from nico_agent.web_onboarding.contracts import (
    WebActivationCreate,
    WebActivationPreview,
    WebActivationRead,
    WebPreviewCreate,
    WebProbeCreate,
    WebProbeRead,
    WebProviderCandidate,
    WebSetupReadiness,
    canonical_candidate_hash,
)

_PREVIEW_TTL = timedelta(minutes=15)
_SEARCH_REF = "web.search@1.0.0"
_FETCH_REF = "web.fetch@1.0.0"
_SEARCH_PERMISSION = "network.web.search"
_FETCH_PERMISSION = "network.web.fetch"
_BRAVE_SECRET = "web_search_brave_api_key"


class WebOnboardingService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    async def setup_readiness(self, context: TenantContext) -> WebSetupReadiness:
        async with self.database.tenant_transaction(context) as session:
            tenant = await session.scalar(select(Tenant).where(Tenant.id == context.tenant_id))
            if tenant is None:
                raise ResourceNotFound("tenant", str(context.tenant_id))
            enabled = self.settings.web_provider_writes_enabled
            return WebSetupReadiness(
                writes_enabled=enabled,
                reason="ready" if enabled else "deployment_policy_disabled",
                tenant_revision=tenant.revision,
            )

    async def create_probe(
        self,
        context: TenantContext,
        command: WebProbeCreate,
    ) -> WebProbeRead:
        endpoint = self._validate_candidate(command.candidate)
        candidate_hash = canonical_candidate_hash(command.candidate)
        async with self.database.tenant_transaction(context) as session:
            existing = await session.scalar(
                select(ProviderProbe).where(
                    ProviderProbe.tenant_id == context.tenant_id,
                    ProviderProbe.idempotency_key == command.idempotency_key,
                )
            )
            if existing is not None:
                if existing.kind != "verify_web" or existing.candidate_hash != candidate_hash:
                    raise DomainConflict(
                        "WEB_PROBE_IDEMPOTENCY_CONFLICT",
                        "Web probe idempotency key was already used for another candidate",
                    )
                return self._probe_read(existing)
            tenant_exists = await session.scalar(
                select(Tenant.id).where(Tenant.id == context.tenant_id)
            )
            if tenant_exists is None:
                raise ResourceNotFound("tenant", str(context.tenant_id))
            candidate = command.candidate
            probe = ProviderProbe(
                tenant_id=context.tenant_id,
                kind="verify_web",
                provider_key=candidate.provider,
                protocol="web_search",
                base_url=endpoint,
                credential_ref=candidate.credential_ref or "none",
                provider_options={
                    "endpoint_key": candidate.endpoint_key,
                    "policy": candidate.policy.model_dump(mode="json"),
                },
                model_name=None,
                catalog_revision=candidate.catalog_revision,
                candidate_hash=candidate_hash,
                idempotency_key=command.idempotency_key,
            )
            session.add(probe)
            await session.flush()
            session.add(
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action="web_provider_probe.create",
                    resource_type="provider_probe",
                    resource_id=probe.id,
                    actor_id=context.actor_id,
                    details={
                        "provider": probe.provider_key,
                        "candidate_hash": candidate_hash,
                    },
                    correlation_id=context.correlation_id,
                )
            )
            await session.flush()
            return self._probe_read(probe)

    async def get_probe(self, context: TenantContext, probe_id: UUID) -> WebProbeRead:
        async with self.database.tenant_transaction(context) as session:
            probe = await self._probe(session, context, probe_id)
            return self._probe_read(probe)

    async def preview_activation(
        self,
        context: TenantContext,
        command: WebPreviewCreate,
    ) -> WebActivationPreview:
        async with self.database.tenant_transaction(context) as session:
            probe = await self._probe(session, context, command.probe_id)
            return await self._build_preview(session, context, probe, command)

    async def activate(
        self,
        context: TenantContext,
        command: WebActivationCreate,
    ) -> WebActivationRead:
        async with self.database.tenant_transaction(context) as session:
            probe = await self._probe(session, context, command.probe_id, for_update=True)
            if probe.status == "activated":
                return await self._activated_result(session, context, probe, command.preview_hash)
            await session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtextextended(
                            f"{context.tenant_id}:web-provider:{probe.provider_key}", 0
                        )
                    )
                )
            )
            preview = await self._build_preview(
                session,
                context,
                probe,
                command,
                lock_target=True,
            )
            if preview.preview_hash != command.preview_hash:
                raise DomainConflict(
                    "WEB_PREVIEW_STALE",
                    "the Web activation preview changed; review it again",
                )
            if command.maintenance_attempt_id is not None:
                active = await session.scalar(
                    text("SELECT provider_maintenance_attempt_active(:attempt_id)"),
                    {"attempt_id": command.maintenance_attempt_id},
                )
                if not active:
                    raise DomainConflict(
                        "WEB_MAINTENANCE_LOST",
                        "the local credential maintenance lease is no longer active",
                    )

            projection = preview.projection
            tenant = await session.scalar(
                select(Tenant).where(Tenant.id == context.tenant_id).with_for_update()
            )
            if tenant is None:
                raise ResourceNotFound("tenant", str(context.tenant_id))
            tenant.settings = deepcopy(projection["tenant"]["settings"])
            tenant.revision += 1

            agent_projection = projection["agent"]
            agent = await session.scalar(
                select(Agent)
                .where(
                    Agent.tenant_id == context.tenant_id,
                    Agent.id == UUID(str(agent_projection["id"])),
                )
                .with_for_update()
            )
            if agent is None:
                agent = Agent(
                    id=UUID(str(agent_projection["id"])),
                    tenant_id=context.tenant_id,
                    name=str(agent_projection["name"]),
                    display_name=str(agent_projection["display_name"]),
                    description="Created by guided Web Provider setup.",
                )
                session.add(agent)
                await session.flush()

            lifecycle = AgentVersionLifecycle(session, context)
            version_projection = projection["agent_version"]
            version = await lifecycle.create(
                agent,
                AgentVersionCreate.model_validate(version_projection["command"]),
                version_id=UUID(str(version_projection["id"])),
            )
            expected_revision = command.target.expected_agent_revision or agent.revision
            await lifecycle.publish(agent, version.id, expected_revision=expected_revision)

            activated_at = datetime.now(UTC)
            probe.status = "activated"
            probe.activated_at = activated_at
            probe.activation_correlation_id = context.correlation_id
            probe.revision += 1
            result = WebActivationRead(
                probe_id=probe.id,
                candidate_hash=probe.candidate_hash,
                provider=probe.provider_key,
                tenant_revision=tenant.revision,
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
                    action="web_provider_activation.complete",
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

    async def _build_preview(
        self,
        session: AsyncSession,
        context: TenantContext,
        probe: ProviderProbe,
        command: WebPreviewCreate,
        *,
        lock_target: bool = False,
    ) -> WebActivationPreview:
        candidate = self._validated_probe_candidate(probe, command.candidate_hash)
        tenant_statement = select(Tenant).where(Tenant.id == context.tenant_id)
        if lock_target:
            tenant_statement = tenant_statement.with_for_update()
        tenant = await session.scalar(tenant_statement)
        if tenant is None:
            raise ResourceNotFound("tenant", str(context.tenant_id))
        if tenant.revision != command.target.expected_tenant_revision:
            raise DomainConflict(
                "REVISION_CONFLICT",
                f"tenant revision is {tenant.revision}, expected "
                f"{command.target.expected_tenant_revision}",
            )
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

        tenant_settings = self._tenant_settings(tenant.settings, candidate, probe)
        agent, agent_projection, current = await self._agent_projection(
            session,
            context,
            command,
            lock_target=lock_target,
        )
        agent_tool_policy = _merge_agent_policy(
            current.tool_policy if current is not None else {},
            candidate,
        )
        lifecycle = AgentVersionLifecycle(session, context)
        if current is not None:
            version_command = lifecycle.command_from_version(
                current,
                model_endpoint_id=current.model_endpoint_id,
                model_name=current.model_name,
                runtime_provider=current.runtime_provider,
                tool_policy=agent_tool_policy,
            )
        else:
            endpoint = await session.scalar(
                select(ModelEndpoint)
                .where(
                    ModelEndpoint.tenant_id == context.tenant_id,
                    ModelEndpoint.enabled.is_(True),
                    ModelEndpoint.status == "active",
                    ModelEndpoint.verified_at.is_not(None),
                )
                .order_by(ModelEndpoint.verified_at.desc(), ModelEndpoint.created_at.desc())
                .limit(1)
            )
            if endpoint is None or not endpoint.allowed_models:
                raise DomainConflict(
                    "WEB_MODEL_ROUTE_REQUIRED",
                    "publish a verified model route before creating a Web Starter Agent",
                )
            version_command = AgentVersionCreate(
                role="web researcher",
                mandate="Research current public information and cite observed sources.",
                runtime_provider="nico_native",
                execution_mode="react",
                model_endpoint_id=endpoint.id,
                model_name=str(endpoint.allowed_models[0]),
                tool_policy=agent_tool_policy,
            )
        latest_version = await session.scalar(
            select(func.max(AgentVersion.version)).where(
                AgentVersion.tenant_id == context.tenant_id,
                AgentVersion.agent_id == UUID(str(agent_projection["id"])),
            )
        )
        version_id = uuid5(
            NAMESPACE_URL,
            f"nico:web-version:{context.tenant_id}:{probe.id}:{agent_projection['id']}",
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
        projection: dict[str, Any] = {
            "schema_version": 1,
            "project": {"id": str(project.id), "name": project.name},
            "tenant": {
                "id": str(tenant.id),
                "expected_revision": tenant.revision,
                "settings": tenant_settings,
            },
            "agent": agent_projection,
            "agent_version": version_projection,
        }
        preview_hash = _preview_hash(probe.id, probe.candidate_hash, projection)
        return WebActivationPreview(
            probe_id=probe.id,
            candidate_hash=probe.candidate_hash,
            preview_hash=preview_hash,
            expires_at=probe.verified_at + _PREVIEW_TTL,
            changed_fields=(
                "tenant.settings.tool_policy",
                "tenant.settings.web_provider",
                "tenant.revision",
                "agent_version",
                "agent.current_version_id",
                "agent.revision",
                "provider_probe.status",
            ),
            projection=projection,
        )

    async def _agent_projection(
        self,
        session: AsyncSession,
        context: TenantContext,
        command: WebPreviewCreate,
        *,
        lock_target: bool,
    ) -> tuple[Agent | None, dict[str, Any], AgentVersion | None]:
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
                    f"agent revision is {agent.revision}, expected "
                    f"{target.expected_agent_revision}",
                )
            current = None
            if agent.current_version_id is not None:
                current = await session.scalar(
                    select(AgentVersion).where(
                        AgentVersion.tenant_id == context.tenant_id,
                        AgentVersion.agent_id == agent.id,
                        AgentVersion.id == agent.current_version_id,
                    )
                )
            return agent, {
                "id": str(agent.id),
                "name": agent.name,
                "display_name": agent.display_name,
                "existing": True,
                "expected_revision": agent.revision,
            }, current

        assert target.starter_agent_name is not None
        name_exists = await session.scalar(
            select(Agent.id).where(
                Agent.tenant_id == context.tenant_id,
                Agent.name == target.starter_agent_name,
            )
        )
        if name_exists is not None:
            raise DomainConflict(
                "WEB_STARTER_AGENT_EXISTS",
                "the requested Starter Agent name is already in use",
            )
        agent_id = uuid5(
            NAMESPACE_URL,
            f"nico:web-agent:{context.tenant_id}:{command.probe_id}:{target.starter_agent_name}",
        )
        return None, {
            "id": str(agent_id),
            "name": target.starter_agent_name,
            "display_name": target.starter_agent_display_name,
            "existing": False,
            "expected_revision": 1,
        }, None

    def _validate_candidate(self, candidate: WebProviderCandidate) -> str:
        catalog = get_web_provider_catalog(self.settings)
        if candidate.catalog_revision != catalog.catalog_revision:
            raise DomainConflict(
                "WEB_CATALOG_STALE",
                "Web Provider candidate uses a stale catalog revision",
            )
        preset = provider_preset(self.settings, candidate.provider)
        if preset is None:
            raise DomainConflict("WEB_PROVIDER_NOT_FOUND", "Web Provider is unavailable")
        endpoint = next(
            (choice for choice in preset.endpoints if choice.key == candidate.endpoint_key),
            None,
        )
        if endpoint is None:
            raise DomainConflict(
                "WEB_PROVIDER_ENDPOINT_INVALID",
                "Web Provider endpoint is not in the deployment catalog",
            )
        if preset.requires_secret and candidate.credential_ref is None:
            raise DomainConflict(
                "WEB_SEARCH_SECRET_UNAVAILABLE",
                "Brave Web Search requires a credential reference",
            )
        if not preset.requires_secret and candidate.credential_ref is not None:
            raise DomainConflict(
                "WEB_PROVIDER_CREDENTIAL_INVALID",
                "SearXNG does not accept a credential reference",
            )
        return endpoint.url

    def _validated_probe_candidate(
        self,
        probe: ProviderProbe,
        candidate_hash: str,
    ) -> WebProviderCandidate:
        if probe.kind != "verify_web" or probe.candidate_hash != candidate_hash:
            raise DomainConflict(
                "WEB_CANDIDATE_MISMATCH",
                "Web Provider candidate no longer matches its verification",
            )
        if probe.status != "succeeded" or probe.verified_at is None:
            raise DomainConflict(
                "WEB_PROBE_NOT_VERIFIED",
                "Web activation requires a successful Provider probe",
            )
        if probe.verified_at + _PREVIEW_TTL <= datetime.now(UTC):
            raise DomainConflict(
                "WEB_PROBE_EXPIRED",
                "Web Provider verification expired; test it again",
            )
        options = probe.provider_options
        candidate = WebProviderCandidate.model_validate(
            {
                "provider": probe.provider_key,
                "endpoint_key": options.get("endpoint_key"),
                "credential_ref": None if probe.credential_ref == "none" else probe.credential_ref,
                "policy": options.get("policy"),
                "catalog_revision": probe.catalog_revision,
            }
        )
        self._validate_candidate(candidate)
        if canonical_candidate_hash(candidate) != probe.candidate_hash:
            raise DomainConflict(
                "WEB_CANONICALIZATION_MISMATCH",
                "Web Provider verification uses an unsupported canonical form",
            )
        return candidate

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
            ProviderProbe.kind == "verify_web",
        )
        if for_update:
            statement = statement.with_for_update()
        probe = await session.scalar(statement)
        if probe is None:
            raise ResourceNotFound("web_provider_probe", str(probe_id))
        return probe

    @staticmethod
    def _probe_read(probe: ProviderProbe) -> WebProbeRead:
        return WebProbeRead(
            id=probe.id,
            status=probe.status,
            provider=probe.provider_key,
            candidate_hash=probe.candidate_hash,
            result=probe.result,
            error_code=probe.error_code,
            error_detail=probe.error_detail,
            verified_at=probe.verified_at,
            activated_at=probe.activated_at,
            revision=probe.revision,
        )

    @staticmethod
    def _tenant_settings(
        current: dict[str, Any],
        candidate: WebProviderCandidate,
        probe: ProviderProbe,
    ) -> dict[str, Any]:
        settings = deepcopy(current or {})
        settings["tool_policy"] = _merge_tenant_policy(
            settings.get("tool_policy", {}), candidate
        )
        settings["web_provider"] = {
            "enabled": True,
            "provider": candidate.provider,
            "endpoint_key": candidate.endpoint_key,
            "credential_ref": candidate.credential_ref,
            "candidate_hash": probe.candidate_hash,
            "verified_probe_id": str(probe.id),
            "verified_at": probe.verified_at.isoformat() if probe.verified_at else None,
        }
        return settings

    @staticmethod
    async def _activated_result(
        session: AsyncSession,
        context: TenantContext,
        probe: ProviderProbe,
        preview_hash: str,
    ) -> WebActivationRead:
        audit = await session.scalar(
            select(AuditRecord)
            .where(
                AuditRecord.tenant_id == context.tenant_id,
                AuditRecord.resource_type == "provider_probe",
                AuditRecord.resource_id == probe.id,
                AuditRecord.action == "web_provider_activation.complete",
            )
            .order_by(AuditRecord.created_at.desc())
            .limit(1)
        )
        if audit is None or audit.details.get("preview_hash") != preview_hash:
            raise DomainConflict(
                "WEB_PROBE_CONSUMED",
                "the Web Provider verification was already activated",
            )
        return WebActivationRead.model_validate(audit.details["result"])


def _merge_tenant_policy(
    value: Any,
    candidate: WebProviderCandidate,
) -> dict[str, Any]:
    policy = deepcopy(value) if isinstance(value, dict) else {}
    policy["allow"] = sorted(set(_strings(policy.get("allow"))) | {_SEARCH_REF, _FETCH_REF})
    policy["permissions"] = sorted(
        set(_strings(policy.get("permissions"))) | {_SEARCH_PERMISSION, _FETCH_PERMISSION}
    )
    tools = deepcopy(policy.get("tools")) if isinstance(policy.get("tools"), dict) else {}
    search_config, fetch_config = _tool_configs(candidate)
    tools[_SEARCH_REF] = search_config
    tools[_FETCH_REF] = fetch_config
    policy["tools"] = tools
    secret_refs = (
        deepcopy(policy.get("secret_refs"))
        if isinstance(policy.get("secret_refs"), dict)
        else {}
    )
    if candidate.credential_ref is not None:
        secret_refs[_BRAVE_SECRET] = candidate.credential_ref
    else:
        secret_refs.pop(_BRAVE_SECRET, None)
    policy["secret_refs"] = secret_refs
    return policy


def _merge_agent_policy(
    value: Any,
    candidate: WebProviderCandidate,
) -> dict[str, Any]:
    policy = deepcopy(value) if isinstance(value, dict) else {}
    policy["allow"] = sorted(set(_strings(policy.get("allow"))) | {_SEARCH_REF, _FETCH_REF})
    policy["permissions"] = sorted(
        set(_strings(policy.get("permissions"))) | {_SEARCH_PERMISSION, _FETCH_PERMISSION}
    )
    tools = deepcopy(policy.get("tools")) if isinstance(policy.get("tools"), dict) else {}
    search_config, fetch_config = _tool_configs(candidate)
    tools[_SEARCH_REF] = search_config
    tools[_FETCH_REF] = fetch_config
    policy["tools"] = tools
    secrets = set(_strings(policy.get("secrets")))
    if candidate.credential_ref is not None:
        secrets.add(_BRAVE_SECRET)
    else:
        secrets.discard(_BRAVE_SECRET)
    policy["secrets"] = sorted(secrets)
    return policy


def _tool_configs(candidate: WebProviderCandidate) -> tuple[dict[str, Any], dict[str, Any]]:
    policy = candidate.policy
    return (
        {
            "provider": candidate.provider,
            "safe_search": policy.safe_search,
            "cache_ttl_seconds": policy.cache_ttl_seconds,
            "rate_limit_per_minute": policy.rate_limit_per_minute,
        },
        {
            "allowed_domains": list(policy.allowed_domains),
            "cache_ttl_seconds": policy.cache_ttl_seconds,
        },
    )


def _strings(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _preview_hash(probe_id: UUID, candidate_hash: str, projection: dict[str, Any]) -> str:
    encoded = json.dumps(
        {
            "schema_version": 1,
            "probe_id": str(probe_id),
            "candidate_hash": candidate_hash,
            "projection": projection,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
