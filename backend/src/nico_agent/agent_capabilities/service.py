"""Transactional Agent capability catalog, preview, and immutable publication."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.agent_capabilities.catalog import (
    maintained_profiles,
    profile_tool_refs,
    skill_catalog,
    tool_catalog,
    web_state,
)
from nico_agent.agent_capabilities.contracts import (
    CapabilityActivationCreate,
    CapabilityActivationRead,
    CapabilityCatalogRead,
    CapabilityPreviewCreate,
    CapabilityPreviewRead,
)
from nico_agent.agent_capabilities.policy import (
    capability_diff,
    compile_skill_policy,
    compile_tool_policy,
    elevated_risks,
    preview_hash,
)
from nico_agent.agent_versions import AgentVersionLifecycle
from nico_agent.api_schemas import AgentVersionCreate
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import AccessDenied, DomainConflict, ResourceNotFound
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    AuditRecord,
    Skill,
    SkillVersion,
    Tenant,
    ToolDefinition,
)
from nico_agent.domain.states import require_revision
from nico_agent.guided_setup.service import merge_guided_setup_ledger

_PREVIEW_TTL = timedelta(minutes=15)


class AgentCapabilityService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def catalog(
        self,
        context: TenantContext,
        *,
        agent_id: UUID | None = None,
    ) -> CapabilityCatalogRead:
        async with self.database.tenant_transaction(context) as session:
            tenant = await self._tenant(session, context)
            agent = None
            version = None
            if agent_id is not None:
                agent = await self._agent(session, context, agent_id)
                version = await self._current_version(session, context, agent)
            return await self._catalog(session, tenant, agent=agent, version=version)

    async def preview(
        self,
        context: TenantContext,
        command: CapabilityPreviewCreate,
    ) -> CapabilityPreviewRead:
        self._require_operator(context)
        async with self.database.tenant_transaction(context) as session:
            return await self._build_preview(session, context, command, lock_target=False)

    async def activate(
        self,
        context: TenantContext,
        command: CapabilityActivationCreate,
    ) -> CapabilityActivationRead:
        self._require_operator(context)
        async with self.database.tenant_transaction(context) as session:
            repeated = await self._activation_result(session, context, command.preview_hash)
            if repeated is not None:
                return repeated
            preview = await self._build_preview(session, context, command, lock_target=True)
            if preview.preview_hash != command.preview_hash:
                raise DomainConflict(
                    "CAPABILITY_PREVIEW_STALE",
                    "the capability proposal changed; review it again",
                )
            if not set(preview.risks) <= set(command.accepted_risks):
                raise DomainConflict(
                    "CAPABILITY_RISK_CONFIRMATION_REQUIRED",
                    "confirm every elevated risk in the capability proposal",
                    details={"required": list(preview.risks)},
                )
            projection = preview.projection
            tenant = await self._tenant(session, context, for_update=True)
            target = projection["target"]
            agent = await session.scalar(
                select(Agent)
                .where(
                    Agent.tenant_id == context.tenant_id,
                    Agent.id == UUID(str(target["agent_id"])),
                )
                .with_for_update()
            )
            if agent is None:
                agent = Agent(
                    id=UUID(str(target["agent_id"])),
                    tenant_id=context.tenant_id,
                    name=str(target["name"]),
                    display_name=str(target["display_name"]),
                    description="Created by guided Agent capability setup.",
                    approval_owner_actor_id=context.actor_id,
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

            tenant.settings = merge_guided_setup_ledger(
                tenant.settings if isinstance(tenant.settings, dict) else {},
                selected_agent_id=agent.id,
                selected_profile=command.selection.profile,
                capability_agent_version_id=version.id,
                proof=None,
            )
            tenant.revision += 1
            published_at = datetime.now(UTC)
            result = CapabilityActivationRead(
                tenant_revision=tenant.revision,
                agent_id=agent.id,
                agent_name=agent.name,
                agent_revision=agent.revision,
                agent_version_id=version.id,
                agent_version=version.version,
                profile=command.selection.profile,
                tool_refs=preview.tool_refs,
                skill_version_ids=preview.skill_version_ids,
                published_at=published_at,
            )
            session.add(
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action="agent_capabilities.activate",
                    resource_type="agent_version",
                    resource_id=version.id,
                    actor_id=context.actor_id,
                    details={
                        "preview_hash": preview.preview_hash,
                        "profile": command.selection.profile,
                        "tool_refs": list(preview.tool_refs),
                        "skill_version_ids": [str(value) for value in preview.skill_version_ids],
                        "risks": list(preview.risks),
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
        command: CapabilityPreviewCreate,
        *,
        lock_target: bool,
    ) -> CapabilityPreviewRead:
        tenant = await self._tenant(session, context, for_update=lock_target)
        require_revision(
            "tenant",
            expected=command.expected_tenant_revision,
            actual=tenant.revision,
        )
        agent, source, target_projection = await self._target(
            session,
            context,
            command,
            lock_target=lock_target,
        )
        catalog = await self._catalog(session, tenant, agent=agent, version=source)
        web_ready = catalog.web_ready
        if command.selection.profile == "custom":
            tool_refs = tuple(sorted(command.selection.tool_refs))
            skill_version_ids = tuple(sorted(command.selection.skill_version_ids, key=str))
        else:
            tool_refs = profile_tool_refs(command.selection.profile, web_ready=web_ready)
            skill_version_ids = ()
        settings = tenant.settings if isinstance(tenant.settings, dict) else {}
        tool_policy = compile_tool_policy(settings, tool_refs, catalog.tools)
        skill_policy = compile_skill_policy(
            settings,
            skill_version_ids,
            catalog.skills,
            base_policy=source.skill_policy,
        )
        lifecycle = AgentVersionLifecycle(session, context)
        version_command = lifecycle.command_from_version(
            source,
            model_endpoint_id=source.model_endpoint_id,
            model_name=source.model_name,
            runtime_provider=source.runtime_provider,
            tool_policy=tool_policy,
            skill_policy=skill_policy,
        )
        target_agent_id = UUID(str(target_projection["agent_id"]))
        latest_version = await session.scalar(
            select(func.max(AgentVersion.version)).where(
                AgentVersion.tenant_id == context.tenant_id,
                AgentVersion.agent_id == target_agent_id,
            )
        )
        next_version = (latest_version or 0) + 1
        proposal_seed = preview_hash(
            {
                "tenant_id": str(context.tenant_id),
                "tenant_revision": tenant.revision,
                "target": target_projection,
                "source_version_id": str(source.id),
                "selection": command.selection.model_dump(mode="json"),
                "command": version_command.model_dump(mode="json", by_alias=True),
            }
        )
        version_id = uuid5(NAMESPACE_URL, f"nico:capability-version:{proposal_seed}")
        diff = capability_diff(
            source.tool_policy if agent is not None else {},
            source.skill_policy if agent is not None else {},
            tool_policy,
            skill_policy,
        )
        projection: dict[str, Any] = {
            "schema_version": 1,
            "tenant": {
                "id": str(tenant.id),
                "expected_revision": tenant.revision,
            },
            "target": target_projection,
            "source_agent_version_id": str(source.id),
            "agent_version": {
                "id": str(version_id),
                "version": next_version,
                "command": version_command.model_dump(mode="json", by_alias=True),
            },
            "selection": {
                "profile": command.selection.profile,
                "tool_refs": list(tool_refs),
                "skill_version_ids": [str(value) for value in skill_version_ids],
            },
            "diff": diff.model_dump(mode="json"),
        }
        proposal_hash = preview_hash(projection)
        session.add(
            AuditRecord(
                tenant_id=context.tenant_id,
                action="agent_capabilities.preview",
                resource_type="agent",
                resource_id=target_agent_id,
                actor_id=context.actor_id,
                details={
                    "preview_hash": proposal_hash,
                    "profile": command.selection.profile,
                    "risks": list(elevated_risks(tool_refs, catalog.tools)),
                },
                correlation_id=context.correlation_id,
            )
        )
        await session.flush()
        return CapabilityPreviewRead(
            preview_hash=proposal_hash,
            expires_at=datetime.now(UTC) + _PREVIEW_TTL,
            target_agent_id=target_agent_id,
            target_agent_name=str(target_projection["name"]),
            proposed_agent_version_id=version_id,
            proposed_agent_version=next_version,
            profile=command.selection.profile,
            tool_refs=tool_refs,
            skill_version_ids=skill_version_ids,
            risks=elevated_risks(tool_refs, catalog.tools),
            diff=diff,
            projection=projection,
        )

    async def _catalog(
        self,
        session: AsyncSession,
        tenant: Tenant,
        *,
        agent: Agent | None,
        version: AgentVersion | None,
    ) -> CapabilityCatalogRead:
        settings = tenant.settings if isinstance(tenant.settings, dict) else {}
        web_ready, web_candidate_hash = web_state(settings)
        definitions = tuple(
            await session.scalars(
                select(ToolDefinition)
                .where(ToolDefinition.tenant_id == tenant.id)
                .order_by(ToolDefinition.name, ToolDefinition.version)
            )
        )
        skill_rows = tuple(
            (
                await session.execute(
                    select(Skill, SkillVersion)
                    .join(
                        SkillVersion,
                        (SkillVersion.tenant_id == Skill.tenant_id)
                        & (SkillVersion.skill_id == Skill.id)
                        & (SkillVersion.id == Skill.current_version_id),
                    )
                    .where(Skill.tenant_id == tenant.id)
                )
            ).all()
        )
        return CapabilityCatalogRead(
            tenant_revision=tenant.revision,
            agent_id=agent.id if agent is not None else None,
            agent_revision=agent.revision if agent is not None else None,
            web_ready=web_ready,
            web_candidate_hash=web_candidate_hash,
            profiles=maintained_profiles(web_ready=web_ready),
            tools=tool_catalog(settings, definitions, target_version=version),
            skills=skill_catalog(
                settings,
                skill_rows,
                target_agent_id=agent.id if agent is not None else None,
            ),
        )

    async def _target(
        self,
        session: AsyncSession,
        context: TenantContext,
        command: CapabilityPreviewCreate,
        *,
        lock_target: bool,
    ) -> tuple[Agent | None, AgentVersion, dict[str, Any]]:
        target = command.target
        if target.agent_id is not None:
            agent = await self._agent(
                session,
                context,
                target.agent_id,
                for_update=lock_target,
            )
            require_revision(
                "agent",
                expected=target.expected_agent_revision,
                actual=agent.revision,
            )
            source = await self._current_version(session, context, agent)
            return (
                agent,
                source,
                {
                    "agent_id": str(agent.id),
                    "name": agent.name,
                    "display_name": agent.display_name,
                    "existing": True,
                    "expected_revision": agent.revision,
                },
            )

        assert target.starter_agent_name is not None
        assert target.source_agent_version_id is not None
        name_exists = await session.scalar(
            select(Agent.id).where(
                Agent.tenant_id == context.tenant_id,
                Agent.name == target.starter_agent_name,
            )
        )
        if name_exists is not None:
            raise DomainConflict(
                "CAPABILITY_STARTER_AGENT_EXISTS",
                "the requested Starter Agent name is already in use",
            )
        source_row = (
            await session.execute(
                select(Agent, AgentVersion)
                .join(
                    AgentVersion,
                    (AgentVersion.tenant_id == Agent.tenant_id)
                    & (AgentVersion.agent_id == Agent.id)
                    & (AgentVersion.id == target.source_agent_version_id),
                )
                .where(
                    Agent.tenant_id == context.tenant_id,
                    Agent.current_version_id == target.source_agent_version_id,
                    AgentVersion.status == "published",
                )
            )
        ).one_or_none()
        if source_row is None:
            raise ResourceNotFound("source_agent_version", str(target.source_agent_version_id))
        _source_agent, source = source_row
        agent_id = uuid5(
            NAMESPACE_URL,
            f"nico:capability-agent:{context.tenant_id}:{target.starter_agent_name}",
        )
        return (
            None,
            source,
            {
                "agent_id": str(agent_id),
                "name": target.starter_agent_name,
                "display_name": target.starter_agent_display_name,
                "existing": False,
                "expected_revision": 1,
            },
        )

    @staticmethod
    async def _tenant(
        session: AsyncSession,
        context: TenantContext,
        *,
        for_update: bool = False,
    ) -> Tenant:
        statement = select(Tenant).where(Tenant.id == context.tenant_id)
        if for_update:
            statement = statement.with_for_update()
        tenant = await session.scalar(statement)
        if tenant is None:
            raise ResourceNotFound("tenant", str(context.tenant_id))
        return tenant

    @staticmethod
    async def _agent(
        session: AsyncSession,
        context: TenantContext,
        agent_id: UUID,
        *,
        for_update: bool = False,
    ) -> Agent:
        statement = select(Agent).where(
            Agent.tenant_id == context.tenant_id,
            Agent.id == agent_id,
        )
        if for_update:
            statement = statement.with_for_update()
        agent = await session.scalar(statement)
        if agent is None:
            raise ResourceNotFound("agent", str(agent_id))
        if agent.status == "archived":
            raise DomainConflict("AGENT_ARCHIVED", "restore the Agent before publication")
        return agent

    @staticmethod
    async def _current_version(
        session: AsyncSession,
        context: TenantContext,
        agent: Agent,
    ) -> AgentVersion:
        if agent.current_version_id is None:
            raise DomainConflict(
                "CAPABILITY_SOURCE_VERSION_REQUIRED",
                "the selected Agent needs a published model version first",
            )
        version = await session.scalar(
            select(AgentVersion).where(
                AgentVersion.tenant_id == context.tenant_id,
                AgentVersion.agent_id == agent.id,
                AgentVersion.id == agent.current_version_id,
                AgentVersion.status == "published",
            )
        )
        if version is None:
            raise DomainConflict(
                "CAPABILITY_SOURCE_VERSION_REQUIRED",
                "the selected Agent published version is unavailable",
            )
        return version

    @staticmethod
    async def _activation_result(
        session: AsyncSession,
        context: TenantContext,
        proposal_hash: str,
    ) -> CapabilityActivationRead | None:
        audits = tuple(
            await session.scalars(
                select(AuditRecord)
                .where(
                    AuditRecord.tenant_id == context.tenant_id,
                    AuditRecord.action == "agent_capabilities.activate",
                )
                .order_by(AuditRecord.created_at.desc())
                .limit(100)
            )
        )
        audit = next(
            (item for item in audits if item.details.get("preview_hash") == proposal_hash),
            None,
        )
        if audit is None:
            return None
        return CapabilityActivationRead.model_validate(audit.details["result"])

    @staticmethod
    def _require_operator(context: TenantContext) -> None:
        if context.actor_id.startswith(("runtime:", "worker:", "agent:")):
            raise AccessDenied(
                "CAPABILITY_CONTROL_PLANE_REQUIRED",
                "runtime Agents cannot change Agent capability grants",
            )
