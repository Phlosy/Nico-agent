"""Session-scoped AgentVersion lifecycle shared by atomic application services."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.api_schemas import AgentVersionCreate
from nico_agent.database import TenantContext
from nico_agent.domain.errors import DomainConflict, ResourceNotFound
from nico_agent.domain.models import Agent, AgentVersion, AuditRecord, Event
from nico_agent.domain.states import (
    AGENT_TRANSITIONS,
    AGENT_VERSION_TRANSITIONS,
    AgentStatus,
    AgentVersionStatus,
    require_revision,
    transition_state,
)


class AgentVersionLifecycle:
    """Mutate Agent versions inside a transaction owned by the caller."""

    def __init__(
        self,
        session: AsyncSession,
        context: TenantContext,
    ) -> None:
        self.session = session
        self.context = context

    async def create(
        self,
        agent: Agent,
        command: AgentVersionCreate,
        *,
        version_id: UUID | None = None,
    ) -> AgentVersion:
        if AgentStatus(agent.status) is AgentStatus.ARCHIVED:
            raise DomainConflict(
                "AGENT_ARCHIVED",
                "restore the agent before creating a new version",
            )
        latest = await self.session.scalar(
            select(func.max(AgentVersion.version)).where(
                AgentVersion.tenant_id == self.context.tenant_id,
                AgentVersion.agent_id == agent.id,
            )
        )
        runtime_provider = self.runtime_provider(command)
        version = AgentVersion(
            id=version_id,
            tenant_id=self.context.tenant_id,
            agent_id=agent.id,
            version=(latest or 0) + 1,
            role=command.role,
            mandate=command.mandate,
            boundaries=command.boundaries,
            long_term_goal=command.long_term_goal,
            current_goal=command.current_goal,
            runtime_provider=runtime_provider,
            execution_mode=command.execution_mode,
            model_endpoint_id=command.model_endpoint_id,
            model_name=command.model_name,
            model_config_json=command.model_config_data,
            tool_policy=command.tool_policy,
            memory_policy=command.memory_policy,
            skill_policy=command.skill_policy,
            plugin_refs=command.plugin_refs,
            coordination_policy=command.coordination_policy,
            budgets=command.budgets,
            run_config=command.run_config,
            content_hash=self.content_hash(command, runtime_provider=runtime_provider),
        )
        self.session.add(version)
        await self.session.flush()
        self._record(
            event_type="AgentVersionCreated",
            aggregate_type="agent_version",
            aggregate_id=version.id,
            action="agent_version.create",
            payload={"agent_id": str(agent.id), "version": version.version},
        )
        await self.session.flush()
        return version

    async def publish(
        self,
        agent: Agent,
        version_id: UUID,
        *,
        expected_revision: int,
    ) -> Agent:
        require_revision(
            "agent",
            expected=expected_revision,
            actual=agent.revision,
        )
        if AgentStatus(agent.status) is AgentStatus.ARCHIVED:
            raise DomainConflict("AGENT_ARCHIVED", "restore the agent before publishing")
        version = await self.version(agent.id, version_id, for_update=True)
        version.status = transition_state(
            "agent_version",
            AgentVersionStatus(version.status),
            AgentVersionStatus.PUBLISHED,
            AGENT_VERSION_TRANSITIONS,
        ).value
        if agent.current_version_id is not None:
            current = await self.version(
                agent.id,
                agent.current_version_id,
                for_update=True,
            )
            current.status = transition_state(
                "agent_version",
                AgentVersionStatus(current.status),
                AgentVersionStatus.SUPERSEDED,
                AGENT_VERSION_TRANSITIONS,
            ).value
        agent.current_version_id = version.id
        if AgentStatus(agent.status) in {AgentStatus.DRAFT, AgentStatus.ERROR}:
            agent.status = transition_state(
                "agent",
                AgentStatus(agent.status),
                AgentStatus.READY,
                AGENT_TRANSITIONS,
            ).value
        agent.revision += 1
        self._record(
            event_type="AgentVersionPublished",
            aggregate_type="agent",
            aggregate_id=agent.id,
            action="agent_version.publish",
            payload={"version_id": str(version.id), "version": version.version},
        )
        await self.session.flush()
        return agent

    async def rollback(
        self,
        agent: Agent,
        version_id: UUID,
        *,
        expected_revision: int,
    ) -> Agent:
        require_revision("agent", expected=expected_revision, actual=agent.revision)
        if agent.current_version_id == version_id:
            raise DomainConflict(
                "VERSION_ALREADY_ACTIVE",
                "the requested version is already active",
            )
        target = await self.version(agent.id, version_id, for_update=True)
        target.status = transition_state(
            "agent_version",
            AgentVersionStatus(target.status),
            AgentVersionStatus.PUBLISHED,
            AGENT_VERSION_TRANSITIONS,
        ).value
        if agent.current_version_id is not None:
            current = await self.version(
                agent.id,
                agent.current_version_id,
                for_update=True,
            )
            current.status = transition_state(
                "agent_version",
                AgentVersionStatus(current.status),
                AgentVersionStatus.SUPERSEDED,
                AGENT_VERSION_TRANSITIONS,
            ).value
        agent.current_version_id = target.id
        agent.revision += 1
        self._record(
            event_type="AgentVersionRolledBack",
            aggregate_type="agent",
            aggregate_id=agent.id,
            action="agent_version.rollback",
            payload={"version_id": str(target.id), "version": target.version},
        )
        await self.session.flush()
        return agent

    async def version(
        self,
        agent_id: UUID,
        version_id: UUID,
        *,
        for_update: bool = False,
    ) -> AgentVersion:
        statement = select(AgentVersion).where(
            AgentVersion.tenant_id == self.context.tenant_id,
            AgentVersion.agent_id == agent_id,
            AgentVersion.id == version_id,
        )
        if for_update:
            statement = statement.with_for_update()
        value = await self.session.scalar(statement)
        if value is None:
            raise ResourceNotFound("agent_version", str(version_id))
        return value

    @staticmethod
    def project_lead_compatibility_issues(
        version: AgentVersion,
        tenant_settings: dict,
    ) -> list[str]:
        """Explain why an immutable version cannot coordinate managed Project members."""

        issues: list[str] = []
        if version.runtime_provider != "nico_native":
            issues.append("Lead Agent must use the Nico native runtime")
        if version.execution_mode not in {"react", "plan_and_execute"}:
            issues.append("Lead Agent must use react or plan_and_execute mode")
        tenant_policy = tenant_settings.get("coordination_policy", {})
        for owner, policy in (
            ("Tenant", tenant_policy),
            ("Lead AgentVersion", version.coordination_policy),
        ):
            if not isinstance(policy, dict) or policy.get("enabled") is not True:
                issues.append(f"{owner} coordination policy must be enabled")
                continue
            scopes = policy.get("allowed_target_scopes", [])
            if not isinstance(scopes, list) or "project_members" not in scopes:
                issues.append(f"{owner} must allow the project_members target scope")
        return issues

    @staticmethod
    def runtime_provider(command: AgentVersionCreate) -> str:
        if command.runtime_provider:
            return command.runtime_provider
        legacy_provider = command.run_config.get("runtime_provider")
        if isinstance(legacy_provider, str) and legacy_provider.strip():
            return legacy_provider.strip()
        return "nico_native"

    @staticmethod
    def content_hash(command: AgentVersionCreate, *, runtime_provider: str) -> str:
        payload = command.model_dump(mode="json", by_alias=True)
        payload["runtime_provider"] = runtime_provider
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def command_from_version(
        source: AgentVersion,
        *,
        model_endpoint_id: UUID,
        model_name: str,
    ) -> AgentVersionCreate:
        return AgentVersionCreate(
            role=source.role,
            mandate=source.mandate,
            boundaries=list(source.boundaries),
            long_term_goal=source.long_term_goal,
            current_goal=source.current_goal,
            runtime_provider="nico_native",
            execution_mode=source.execution_mode or "direct",
            model_endpoint_id=model_endpoint_id,
            model_name=model_name,
            model_config=source.model_config_json,
            tool_policy=source.tool_policy,
            memory_policy=source.memory_policy,
            skill_policy=source.skill_policy,
            plugin_refs=source.plugin_refs,
            coordination_policy=source.coordination_policy,
            budgets=source.budgets,
            run_config=source.run_config,
        )

    @staticmethod
    def copy(source: AgentVersion, agent_id: UUID, *, version: int) -> AgentVersion:
        return AgentVersion(
            tenant_id=source.tenant_id,
            agent_id=agent_id,
            version=version,
            role=source.role,
            mandate=source.mandate,
            boundaries=source.boundaries,
            long_term_goal=source.long_term_goal,
            current_goal=source.current_goal,
            runtime_provider=source.runtime_provider,
            execution_mode=source.execution_mode,
            model_endpoint_id=source.model_endpoint_id,
            model_name=source.model_name,
            model_config_json=source.model_config_json,
            tool_policy=source.tool_policy,
            memory_policy=source.memory_policy,
            skill_policy=source.skill_policy,
            plugin_refs=source.plugin_refs,
            coordination_policy=source.coordination_policy,
            budgets=source.budgets,
            run_config=source.run_config,
            content_hash=source.content_hash,
        )

    def _record(
        self,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: UUID,
        action: str,
        payload: dict,
    ) -> None:
        self.session.add(
            Event(
                tenant_id=self.context.tenant_id,
                event_type=event_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                actor_id=self.context.actor_id,
                payload=payload,
                correlation_id=self.context.correlation_id,
            )
        )
        self.session.add(
            AuditRecord(
                tenant_id=self.context.tenant_id,
                action=action,
                resource_type=aggregate_type,
                resource_id=aggregate_id,
                actor_id=self.context.actor_id,
                details=payload,
                correlation_id=self.context.correlation_id,
            )
        )
