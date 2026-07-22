"""Transactional Web Provider onboarding and AgentVersion publication."""

from __future__ import annotations

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
from nico_agent.guided_setup.service import merge_guided_setup_ledger
from nico_agent.web_onboarding.catalog import get_web_provider_catalog, provider_preset
from nico_agent.web_onboarding.contracts import (
    WebActivationCreate,
    WebActivationPreview,
    WebActivationRead,
    WebAuthorizedAgent,
    WebConfigurationTestCreate,
    WebDisableCreate,
    WebDisablePreview,
    WebDisablePreviewCreate,
    WebDisableRead,
    WebPreviewCreate,
    WebProbeCreate,
    WebProbeRead,
    WebProviderCandidate,
    WebProviderStatus,
    WebSearchPolicy,
    WebSetupReadiness,
    canonical_candidate_hash,
)
from nico_agent.web_onboarding.policy import (
    FETCH_REF as _FETCH_REF,
)
from nico_agent.web_onboarding.policy import (
    SEARCH_REF as _SEARCH_REF,
)
from nico_agent.web_onboarding.policy import (
    disable_preview_hash as _disable_preview_hash,
)
from nico_agent.web_onboarding.policy import (
    merge_agent_policy as _merge_agent_policy,
)
from nico_agent.web_onboarding.policy import (
    merge_tenant_policy as _merge_tenant_policy,
)
from nico_agent.web_onboarding.policy import (
    preview_hash as _preview_hash,
)
from nico_agent.web_onboarding.policy import (
    remove_agent_web_policy as _remove_agent_web_policy,
)
from nico_agent.web_onboarding.policy import (
    remove_tenant_web_policy as _remove_tenant_web_policy,
)
from nico_agent.web_onboarding.policy import (
    strings as _strings,
)

_PREVIEW_TTL = timedelta(minutes=15)


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

    async def status(self, context: TenantContext) -> WebProviderStatus:
        async with self.database.tenant_transaction(context) as session:
            tenant = await session.scalar(select(Tenant).where(Tenant.id == context.tenant_id))
            if tenant is None:
                raise ResourceNotFound("tenant", str(context.tenant_id))
            settings = tenant.settings if isinstance(tenant.settings, dict) else {}
            configured_value = settings.get("web_provider")
            configured_web = configured_value if isinstance(configured_value, dict) else {}
            provider_value = configured_web.get("provider")
            provider = provider_value if provider_value in {"brave", "searxng"} else None
            configured = provider is not None and bool(configured_web.get("endpoint_key"))
            enabled = configured and configured_web.get("enabled") is True
            candidate_hash = configured_web.get("candidate_hash")
            dns_resolver_value = configured_web.get("dns_resolver")
            dns_resolver = (
                dns_resolver_value
                if dns_resolver_value in {"system", "cloudflare", "google"}
                else "system"
            )

            tenant_policy_value = settings.get("tool_policy")
            tenant_policy = tenant_policy_value if isinstance(tenant_policy_value, dict) else {}
            tenant_allow = set(_strings(tenant_policy.get("allow")))
            tenant_authorized = {_SEARCH_REF, _FETCH_REF} <= tenant_allow

            agent_rows = (
                await session.execute(
                    select(Agent, AgentVersion)
                    .join(AgentVersion, Agent.current_version_id == AgentVersion.id)
                    .where(
                        Agent.tenant_id == context.tenant_id,
                        Agent.status != "archived",
                    )
                    .order_by(Agent.name)
                )
            ).all()
            agents = tuple(
                WebAuthorizedAgent(
                    id=agent.id,
                    name=agent.name,
                    revision=agent.revision,
                    current_version_id=version.id,
                    current_version=version.version,
                )
                for agent, version in agent_rows
                if {_SEARCH_REF, _FETCH_REF} <= set(_strings(version.tool_policy.get("allow")))
            )
            authorized = enabled and tenant_authorized and bool(agents)

            probe = await session.scalar(
                select(ProviderProbe)
                .where(
                    ProviderProbe.tenant_id == context.tenant_id,
                    ProviderProbe.kind == "verify_web",
                    ProviderProbe.provider_key == provider,
                    ProviderProbe.candidate_hash == candidate_hash,
                )
                .order_by(ProviderProbe.created_at.desc())
                .limit(1)
            )
            latest_probe = self._probe_read(probe) if probe is not None else None
            diagnosis = "ready"
            if not configured:
                diagnosis = "unconfigured"
            elif not authorized:
                diagnosis = "unauthorized"
            elif probe is not None and probe.status == "failed":
                diagnosis = (
                    "provider_unreachable"
                    if probe.error_code == "WEB_PROVIDER_UNAVAILABLE"
                    else "recent_probe_failed"
                )
            return WebProviderStatus(
                writes_enabled=self.settings.web_provider_writes_enabled,
                tenant_revision=tenant.revision,
                configured=configured,
                authorized=authorized,
                enabled=enabled,
                provider=provider,
                endpoint_key=(
                    str(configured_web["endpoint_key"])
                    if configured and configured_web.get("endpoint_key")
                    else None
                ),
                dns_resolver=dns_resolver,
                credential_ref=(
                    str(configured_web["credential_ref"])
                    if configured_web.get("credential_ref")
                    else None
                ),
                secret_required=provider == "brave",
                diagnosis=diagnosis,
                latest_probe=latest_probe,
                agents=agents,
            )

    async def test_configuration(
        self,
        context: TenantContext,
        command: WebConfigurationTestCreate,
    ) -> WebProbeRead:
        async with self.database.tenant_transaction(context) as session:
            tenant = await session.scalar(select(Tenant).where(Tenant.id == context.tenant_id))
            if tenant is None:
                raise ResourceNotFound("tenant", str(context.tenant_id))
            candidate = self._candidate_from_settings(tenant.settings)
        return await self.create_probe(
            context,
            WebProbeCreate(candidate=candidate, idempotency_key=command.idempotency_key),
        )

    async def preview_disable(
        self,
        context: TenantContext,
        command: WebDisablePreviewCreate,
    ) -> WebDisablePreview:
        async with self.database.tenant_transaction(context) as session:
            return await self._build_disable_preview(session, context, command)

    async def disable(
        self,
        context: TenantContext,
        command: WebDisableCreate,
    ) -> WebDisableRead:
        async with self.database.tenant_transaction(context) as session:
            await session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtextextended(f"{context.tenant_id}:web-provider:disable", 0)
                    )
                )
            )
            preview = await self._build_disable_preview(
                session,
                context,
                command,
                lock_target=True,
            )
            if preview.preview_hash != command.preview_hash:
                raise DomainConflict(
                    "WEB_DISABLE_PREVIEW_STALE",
                    "the Web disable preview changed; review it again",
                )
            projection = preview.projection
            tenant = await session.scalar(
                select(Tenant).where(Tenant.id == context.tenant_id).with_for_update()
            )
            if tenant is None:
                raise ResourceNotFound("tenant", str(context.tenant_id))
            tenant.settings = deepcopy(projection["tenant"]["settings"])
            tenant.revision += 1
            agent = await session.scalar(
                select(Agent)
                .where(
                    Agent.tenant_id == context.tenant_id,
                    Agent.id == command.target.agent_id,
                )
                .with_for_update()
            )
            if agent is None:
                raise ResourceNotFound("agent", str(command.target.agent_id))
            lifecycle = AgentVersionLifecycle(session, context)
            version_projection = projection["agent_version"]
            version = await lifecycle.create(
                agent,
                AgentVersionCreate.model_validate(version_projection["command"]),
                version_id=UUID(str(version_projection["id"])),
            )
            await lifecycle.publish(
                agent,
                version.id,
                expected_revision=command.target.expected_agent_revision,
            )
            disabled_at = datetime.now(UTC)
            result = WebDisableRead(
                provider=str(projection["provider"]),
                tenant_revision=tenant.revision,
                agent_id=agent.id,
                agent_revision=agent.revision,
                agent_version_id=version.id,
                agent_version=version.version,
                disabled_at=disabled_at,
            )
            session.add(
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action="web_provider_disable.complete",
                    resource_type="agent",
                    resource_id=agent.id,
                    actor_id=context.actor_id,
                    details={
                        "provider": result.provider,
                        "preview_hash": preview.preview_hash,
                        "agent_version_id": str(result.agent_version_id),
                    },
                    correlation_id=context.correlation_id,
                )
            )
            await session.flush()
            return result

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

            agent = None
            version = None
            if command.scope == "provider_and_agent":
                assert command.target is not None
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
                scope=command.scope,
                probe_id=probe.id,
                candidate_hash=probe.candidate_hash,
                provider=probe.provider_key,
                tenant_revision=tenant.revision,
                agent_id=agent.id if agent is not None else None,
                agent_revision=agent.revision if agent is not None else None,
                agent_version_id=version.id if version is not None else None,
                agent_version=version.version if version is not None else None,
                project_id=(command.target.project_id if command.target is not None else None),
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
        if tenant.revision != command.tenant_revision:
            raise DomainConflict(
                "REVISION_CONFLICT",
                f"tenant revision is {tenant.revision}, expected {command.tenant_revision}",
            )
        tenant_settings = self._tenant_settings(tenant.settings, candidate, probe)
        if command.scope == "provider_only":
            tenant_settings = merge_guided_setup_ledger(
                tenant_settings,
                web_intent="enabled",
            )
            projection: dict[str, Any] = {
                "schema_version": 1,
                "scope": "provider_only",
                "tenant": {
                    "id": str(tenant.id),
                    "expected_revision": tenant.revision,
                    "settings": tenant_settings,
                },
            }
            return WebActivationPreview(
                probe_id=probe.id,
                candidate_hash=probe.candidate_hash,
                preview_hash=_preview_hash(probe.id, probe.candidate_hash, projection),
                expires_at=probe.verified_at + _PREVIEW_TTL,
                changed_fields=(
                    "tenant.settings.tool_policy",
                    "tenant.settings.web_provider",
                    "tenant.settings.guided_setup.web_intent",
                    "tenant.revision",
                    "provider_probe.status",
                ),
                projection=projection,
            )

        assert command.target is not None
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
        self._bind_candidate_hash(agent_tool_policy, probe.candidate_hash)
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
        assert target is not None
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
            return (
                agent,
                {
                    "id": str(agent.id),
                    "name": agent.name,
                    "display_name": agent.display_name,
                    "existing": True,
                    "expected_revision": agent.revision,
                },
                current,
            )

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
        return (
            None,
            {
                "id": str(agent_id),
                "name": target.starter_agent_name,
                "display_name": target.starter_agent_display_name,
                "existing": False,
                "expected_revision": 1,
            },
            None,
        )

    def _candidate_from_settings(self, value: Any) -> WebProviderCandidate:
        settings = value if isinstance(value, dict) else {}
        configured_value = settings.get("web_provider")
        configured = configured_value if isinstance(configured_value, dict) else {}
        provider = configured.get("provider")
        endpoint_key = configured.get("endpoint_key")
        if (
            configured.get("enabled") is not True
            or provider not in {"brave", "searxng"}
            or not isinstance(endpoint_key, str)
        ):
            raise DomainConflict(
                "WEB_SEARCH_NOT_CONFIGURED",
                "configure and enable a Web Provider before testing it",
            )
        tenant_policy_value = settings.get("tool_policy")
        tenant_policy = tenant_policy_value if isinstance(tenant_policy_value, dict) else {}
        tools_value = tenant_policy.get("tools")
        tools = tools_value if isinstance(tools_value, dict) else {}
        search_value = tools.get(_SEARCH_REF)
        search = search_value if isinstance(search_value, dict) else {}
        fetch_value = tools.get(_FETCH_REF)
        fetch = fetch_value if isinstance(fetch_value, dict) else {}
        credential_ref = configured.get("credential_ref")
        return WebProviderCandidate(
            provider=provider,
            endpoint_key=endpoint_key,
            credential_ref=credential_ref if isinstance(credential_ref, str) else None,
            policy=WebSearchPolicy(
                safe_search=search.get("safe_search", "moderate"),
                cache_ttl_seconds=search.get("cache_ttl_seconds", 900),
                rate_limit_per_minute=search.get("rate_limit_per_minute", 20),
                allowed_domains=tuple(
                    item for item in fetch.get("allowed_domains", []) if isinstance(item, str)
                ),
            ),
            catalog_revision=get_web_provider_catalog(self.settings).catalog_revision,
        )

    async def _build_disable_preview(
        self,
        session: AsyncSession,
        context: TenantContext,
        command: WebDisablePreviewCreate,
        *,
        lock_target: bool = False,
    ) -> WebDisablePreview:
        tenant_statement = select(Tenant).where(Tenant.id == context.tenant_id)
        agent_statement = select(Agent).where(
            Agent.tenant_id == context.tenant_id,
            Agent.id == command.target.agent_id,
        )
        if lock_target:
            tenant_statement = tenant_statement.with_for_update()
            agent_statement = agent_statement.with_for_update()
        tenant = await session.scalar(tenant_statement)
        if tenant is None:
            raise ResourceNotFound("tenant", str(context.tenant_id))
        if tenant.revision != command.target.expected_tenant_revision:
            raise DomainConflict(
                "REVISION_CONFLICT",
                f"tenant revision is {tenant.revision}, expected "
                f"{command.target.expected_tenant_revision}",
            )
        agent = await session.scalar(agent_statement)
        if agent is None:
            raise ResourceNotFound("agent", str(command.target.agent_id))
        if agent.revision != command.target.expected_agent_revision:
            raise DomainConflict(
                "REVISION_CONFLICT",
                f"agent revision is {agent.revision}, expected "
                f"{command.target.expected_agent_revision}",
            )
        if agent.current_version_id is None:
            raise DomainConflict(
                "WEB_AGENT_VERSION_REQUIRED",
                "the selected Agent has no published version",
            )
        current = await session.scalar(
            select(AgentVersion).where(
                AgentVersion.tenant_id == context.tenant_id,
                AgentVersion.agent_id == agent.id,
                AgentVersion.id == agent.current_version_id,
            )
        )
        if current is None:
            raise DomainConflict(
                "WEB_AGENT_VERSION_REQUIRED",
                "the selected Agent published version is unavailable",
            )
        settings = tenant.settings if isinstance(tenant.settings, dict) else {}
        configured_value = settings.get("web_provider")
        configured = configured_value if isinstance(configured_value, dict) else {}
        provider = configured.get("provider")
        if configured.get("enabled") is not True or provider not in {"brave", "searxng"}:
            raise DomainConflict("WEB_ALREADY_DISABLED", "Web access is not currently enabled")
        current_allow = set(_strings(current.tool_policy.get("allow")))
        if not current_allow.intersection({_SEARCH_REF, _FETCH_REF}):
            raise DomainConflict(
                "WEB_AGENT_NOT_AUTHORIZED",
                "the selected Agent does not currently have Web access",
            )

        tenant_settings = deepcopy(settings)
        tenant_settings["tool_policy"] = _remove_tenant_web_policy(
            tenant_settings.get("tool_policy")
        )
        disabled_config = deepcopy(configured)
        disabled_config["enabled"] = False
        tenant_settings["web_provider"] = disabled_config
        lifecycle = AgentVersionLifecycle(session, context)
        version_command = lifecycle.command_from_version(
            current,
            model_endpoint_id=current.model_endpoint_id,
            model_name=current.model_name,
            runtime_provider=current.runtime_provider,
            tool_policy=_remove_agent_web_policy(current.tool_policy),
        )
        latest_version = await session.scalar(
            select(func.max(AgentVersion.version)).where(
                AgentVersion.tenant_id == context.tenant_id,
                AgentVersion.agent_id == agent.id,
            )
        )
        version_id = uuid5(
            NAMESPACE_URL,
            f"nico:web-disable:{context.tenant_id}:{tenant.revision}:{agent.id}:"
            f"{agent.revision}:{current.id}",
        )
        runtime_provider = lifecycle.runtime_provider(version_command)
        projection: dict[str, Any] = {
            "schema_version": 1,
            "provider": provider,
            "tenant": {
                "id": str(tenant.id),
                "expected_revision": tenant.revision,
                "settings": tenant_settings,
            },
            "agent": {
                "id": str(agent.id),
                "name": agent.name,
                "expected_revision": agent.revision,
                "current_version_id": str(current.id),
            },
            "agent_version": {
                "id": str(version_id),
                "version": (latest_version or 0) + 1,
                "content_hash": lifecycle.content_hash(
                    version_command,
                    runtime_provider=runtime_provider,
                ),
                "command": version_command.model_dump(mode="json", by_alias=True),
            },
        }
        return WebDisablePreview(
            preview_hash=_disable_preview_hash(projection),
            changed_fields=(
                "tenant.settings.tool_policy",
                "tenant.settings.web_provider.enabled",
                "tenant.revision",
                "agent_version",
                "agent.current_version_id",
                "agent.revision",
            ),
            projection=projection,
        )

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
        settings["tool_policy"] = _merge_tenant_policy(settings.get("tool_policy", {}), candidate)
        WebOnboardingService._bind_candidate_hash(
            settings["tool_policy"],
            probe.candidate_hash,
        )
        settings["web_provider"] = {
            "enabled": True,
            "provider": candidate.provider,
            "endpoint_key": candidate.endpoint_key,
            "dns_resolver": candidate.policy.dns_resolver,
            "credential_ref": candidate.credential_ref,
            "candidate_hash": probe.candidate_hash,
            "verified_probe_id": str(probe.id),
            "verified_at": probe.verified_at.isoformat() if probe.verified_at else None,
        }
        return settings

    @staticmethod
    def _bind_candidate_hash(policy: dict[str, Any], candidate_hash: str) -> None:
        tools = policy.get("tools")
        if not isinstance(tools, dict):
            return
        for reference in (_SEARCH_REF, _FETCH_REF):
            config = tools.get(reference)
            if isinstance(config, dict):
                config["candidate_hash"] = candidate_hash

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
