from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.agent_capabilities.contracts import (
    CapabilityActivationCreate,
    CapabilityPreviewCreate,
    CapabilitySelection,
    CapabilityTarget,
)
from nico_agent.agent_capabilities.service import AgentCapabilityService
from nico_agent.config import Settings
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import AccessDenied, RevisionConflict
from nico_agent.domain.models import Agent, AgentVersion, ModelEndpoint, ProviderProbe, Tenant
from nico_agent.local_defaults import local_tenant_settings

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


async def _seed_agent(database: Database):
    suffix = uuid4().hex[:10]
    now = datetime.now(UTC)
    async with database.admin_transaction() as session:
        tenant = Tenant(
            name=f"Capability activation {suffix}",
            slug=f"capability-activation-{suffix}",
            settings=local_tenant_settings(),
        )
        session.add(tenant)
        await session.flush()
        agent = Agent(
            tenant_id=tenant.id,
            name=f"source-{suffix}",
            display_name="Source Agent",
            status="ready",
        )
        probe = ProviderProbe(
            tenant_id=tenant.id,
            kind="verify_completion",
            status="succeeded",
            provider_key="deepseek",
            protocol="openai_compatible",
            base_url="https://models.example/v1",
            credential_ref="env:NICO_MODEL_SECRET_TEST",
            model_name="deepseek-test",
            catalog_revision="test",
            candidate_hash="a" * 64,
            idempotency_key=f"model-{suffix}",
            result={"response_present": True},
            completed_at=now,
            verified_at=now,
        )
        session.add_all([agent, probe])
        await session.flush()
        endpoint = ModelEndpoint(
            tenant_id=tenant.id,
            stable_key=f"model-{suffix}",
            revision=1,
            display_name="Verified model",
            base_url="https://models.example/v1",
            credential_ref="env:NICO_MODEL_SECRET_TEST",
            allowed_models=["deepseek-test"],
            capabilities={
                "streaming": True,
                "native_tool_calling": True,
                "json_object": True,
            },
            verified_probe_id=probe.id,
            verified_at=now,
        )
        session.add(endpoint)
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="assistant",
            mandate="Help the operator",
            runtime_provider="nico_native",
            execution_mode="react",
            model_endpoint_id=endpoint.id,
            model_name="deepseek-test",
            tool_policy={},
            memory_policy={"enabled": True, "top_k": 3},
            skill_policy={},
            coordination_policy={"enabled": False},
            budgets={"max_steps": 12},
            run_config={"temperature": 0.2},
            content_hash="b" * 64,
        )
        session.add(version)
        await session.flush()
        agent.current_version_id = version.id
        await session.flush()
        return tenant.id, tenant.revision, agent.id, agent.revision, version.id


@pytest.mark.asyncio
async def test_existing_agent_publication_is_immutable_and_idempotent() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    service = AgentCapabilityService(database)
    try:
        tenant_id, tenant_revision, agent_id, agent_revision, old_version_id = await _seed_agent(
            database
        )
        context = TenantContext(tenant_id, "capability-operator", uuid4())
        target = CapabilityTarget(
            agent_id=agent_id,
            expected_agent_revision=agent_revision,
        )
        selection = CapabilitySelection(profile="developer")
        preview = await service.preview(
            context,
            CapabilityPreviewCreate(
                expected_tenant_revision=tenant_revision,
                target=target,
                selection=selection,
            ),
        )
        assert preview.risks == ("high", "medium")
        command = CapabilityActivationCreate(
            expected_tenant_revision=tenant_revision,
            target=target,
            selection=selection,
            preview_hash=preview.preview_hash,
            accepted_risks=("high", "medium"),
        )
        activated = await service.activate(context, command)
        repeated = await service.activate(context, command)
        assert repeated == activated
        async with database.tenant_transaction(context) as session:
            tenant = await session.get(Tenant, tenant_id)
            agent = await session.get(Agent, agent_id)
            old = await session.get(AgentVersion, old_version_id)
            new = await session.get(AgentVersion, activated.agent_version_id)
            assert tenant is not None and tenant.revision == tenant_revision + 1
            assert tenant.settings["guided_setup"]["selected_profile"] == "developer"
            assert agent is not None and agent.current_version_id == new.id
            assert old is not None and old.status == "superseded" and old.tool_policy == {}
            assert new is not None and new.status == "published"
            assert new.model_name == "deepseek-test"
            assert new.memory_policy == {"enabled": True, "top_k": 3}
            assert new.coordination_policy == {"enabled": False}
            assert new.budgets == {"max_steps": 12}
            assert new.run_config == {"temperature": 0.2}
            assert set(new.tool_policy["allow"]) == {
                "file.read@1.0.0",
                "file.write@1.0.0",
                "report.write@1.0.0",
                "python.execute@1.0.0",
            }
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AgentVersion)
                    .where(
                        AgentVersion.tenant_id == tenant_id,
                        AgentVersion.agent_id == agent_id,
                    )
                )
                == 2
            )
        with pytest.raises(RevisionConflict, match="revision"):
            await service.preview(
                context,
                CapabilityPreviewCreate(
                    expected_tenant_revision=tenant_revision,
                    target=target,
                    selection=selection,
                ),
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_starter_publication_copies_model_behavior_without_mutating_source() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    service = AgentCapabilityService(database)
    try:
        (
            tenant_id,
            tenant_revision,
            source_agent_id,
            source_revision,
            source_version_id,
        ) = await _seed_agent(database)
        context = TenantContext(tenant_id, "capability-operator", uuid4())
        target = CapabilityTarget(
            starter_agent_name=f"starter-{uuid4().hex[:8]}",
            starter_agent_display_name="Starter Agent",
            source_agent_version_id=source_version_id,
        )
        selection = CapabilitySelection(profile="minimal")
        preview = await service.preview(
            context,
            CapabilityPreviewCreate(
                expected_tenant_revision=tenant_revision,
                target=target,
                selection=selection,
            ),
        )
        activated = await service.activate(
            context,
            CapabilityActivationCreate(
                expected_tenant_revision=tenant_revision,
                target=target,
                selection=selection,
                preview_hash=preview.preview_hash,
            ),
        )
        async with database.tenant_transaction(context) as session:
            source = await session.get(Agent, source_agent_id)
            starter = await session.get(Agent, activated.agent_id)
            version = await session.get(AgentVersion, activated.agent_version_id)
            assert source is not None and source.revision == source_revision
            assert source.current_version_id == source_version_id
            assert starter is not None and starter.current_version_id == version.id
            assert version is not None and version.model_name == "deepseek-test"
            assert version.tool_policy["allow"] == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_actor_cannot_publish_capabilities() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    service = AgentCapabilityService(database)
    try:
        context = TenantContext(uuid4(), "runtime:agent", uuid4())
        with pytest.raises(AccessDenied, match="runtime Agents"):
            await service.preview(
                context,
                CapabilityPreviewCreate(
                    expected_tenant_revision=1,
                    target=CapabilityTarget(
                        agent_id=uuid4(),
                        expected_agent_revision=1,
                    ),
                    selection=CapabilitySelection(profile="minimal"),
                ),
            )
    finally:
        await engine.dispose()
