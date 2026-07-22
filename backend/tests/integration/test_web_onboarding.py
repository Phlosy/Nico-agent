from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.agent_versions import AgentVersionLifecycle
from nico_agent.config import Settings
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainConflict
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    ModelEndpoint,
    Project,
    ProviderProbe,
    Tenant,
)
from nico_agent.provider_onboarding.worker import ProviderProbeWorker
from nico_agent.web.contracts import SearchPage
from nico_agent.web.registry import WebProviderRegistry
from nico_agent.web_onboarding.catalog import get_web_provider_catalog
from nico_agent.web_onboarding.contracts import (
    WebActivationCreate,
    WebActivationTarget,
    WebConfigurationTestCreate,
    WebDisableCreate,
    WebDisablePreviewCreate,
    WebDisableTarget,
    WebPreviewCreate,
    WebProbeCreate,
    WebProviderCandidate,
)
from nico_agent.web_onboarding.service import WebOnboardingService

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


class EmptySearchProvider:
    key = "searxng"

    async def search(self, request, *, secret=None, config=None):
        del request, secret, config
        return SearchPage(provider="searxng", results=())


async def _seed_existing_agent(database: Database):
    suffix = uuid4().hex[:10]
    now = datetime.now(UTC)
    async with database.admin_transaction() as session:
        tenant = Tenant(
            name=f"Web onboarding {suffix}",
            slug=f"web-onboarding-{suffix}",
            settings={
                "tool_policy": {
                    "allow": ["file.read@1.0.0"],
                    "permissions": ["filesystem.read"],
                    "tools": {"file.read@1.0.0": {"roots": ["workspace"]}},
                }
            },
        )
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Existing Agent",
            status="ready",
        )
        model_probe = ProviderProbe(
            tenant_id=tenant.id,
            kind="verify_completion",
            status="succeeded",
            provider_key="openai",
            protocol="openai_compatible",
            base_url="https://api.openai.com/v1",
            credential_ref="secret:test/model",
            model_name="test-model",
            catalog_revision="test",
            candidate_hash="a" * 64,
            idempotency_key=f"model-{suffix}",
            result={"response_present": True},
            completed_at=now,
            verified_at=now,
        )
        session.add_all([project, agent, model_probe])
        await session.flush()
        endpoint = ModelEndpoint(
            tenant_id=tenant.id,
            stable_key=f"model-{suffix}",
            revision=1,
            display_name="Verified model",
            base_url="https://models.example/v1",
            credential_ref="env:NICO_MODEL_SECRET_TEST",
            allowed_models=["test-model"],
            capabilities={"streaming": True, "tools": True},
            verified_probe_id=model_probe.id,
            verified_at=now,
        )
        session.add(endpoint)
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="researcher",
            mandate="Preserve this existing Agent configuration",
            runtime_provider="nico_native",
            execution_mode="react",
            model_endpoint_id=endpoint.id,
            model_name="test-model",
            tool_policy={
                "allow": ["file.read@1.0.0"],
                "permissions": ["filesystem.read"],
                "tools": {"file.read@1.0.0": {"roots": ["workspace"]}},
            },
            content_hash="b" * 64,
        )
        session.add(version)
        await session.flush()
        agent.current_version_id = version.id
        await session.flush()
        return tenant.id, tenant.revision, project.id, agent.id, agent.revision, version.id


async def _verified_web_probe(
    database: Database,
    service: WebOnboardingService,
    context: TenantContext,
    candidate: WebProviderCandidate,
):
    probe = await service.create_probe(
        context,
        WebProbeCreate(
            candidate=candidate,
            idempotency_key=f"web-probe-{uuid4().hex}",
        ),
    )
    async with database.tenant_transaction(context) as session:
        stored = await session.get(ProviderProbe, probe.id)
        assert stored is not None
        now = datetime.now(UTC)
        stored.status = "succeeded"
        stored.result = {"response_present": True, "provider": candidate.provider}
        stored.completed_at = now
        stored.verified_at = now
        stored.revision += 1
        await session.flush()
    return await service.get_probe(context, probe.id)


@pytest.mark.asyncio
async def test_web_activation_merges_policies_and_publishes_one_immutable_version() -> None:
    settings = Settings(
        environment="test",
        web_provider_writes_enabled=True,
        _env_file=None,
    )
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    service = WebOnboardingService(database, settings)
    try:
        (
            tenant_id,
            tenant_revision,
            project_id,
            agent_id,
            agent_revision,
            old_version_id,
        ) = await _seed_existing_agent(database)
        context = TenantContext(tenant_id, "web-onboarding-test", uuid4())
        candidate = WebProviderCandidate(
            provider="brave",
            endpoint_key="managed",
            credential_ref="env:NICO_TOOL_SECRET_WEB_SEARCH_BRAVE_TEST",
            catalog_revision=get_web_provider_catalog(settings).catalog_revision,
        )
        probe = await _verified_web_probe(database, service, context, candidate)
        target = WebActivationTarget(
            project_id=project_id,
            expected_tenant_revision=tenant_revision,
            agent_id=agent_id,
            expected_agent_revision=agent_revision,
        )
        preview = await service.preview_activation(
            context,
            WebPreviewCreate(
                probe_id=probe.id,
                candidate_hash=probe.candidate_hash,
                target=target,
            ),
        )

        projected_tenant_policy = preview.projection["tenant"]["settings"]["tool_policy"]
        projected_agent_policy = preview.projection["agent_version"]["command"]["tool_policy"]
        assert "file.read@1.0.0" in projected_tenant_policy["allow"]
        assert "file.read@1.0.0" in projected_agent_policy["allow"]
        assert projected_tenant_policy["secret_refs"] == {
            "web_search_brave_api_key": "env:NICO_TOOL_SECRET_WEB_SEARCH_BRAVE_TEST"
        }
        assert projected_agent_policy["secrets"] == ["web_search_brave_api_key"]

        activated = await service.activate(
            context,
            WebActivationCreate(
                probe_id=probe.id,
                candidate_hash=probe.candidate_hash,
                target=target,
                preview_hash=preview.preview_hash,
            ),
        )
        repeated = await service.activate(
            context,
            WebActivationCreate(
                probe_id=probe.id,
                candidate_hash=probe.candidate_hash,
                target=target,
                preview_hash=preview.preview_hash,
            ),
        )

        assert repeated == activated
        async with database.tenant_transaction(context) as session:
            tenant = await session.get(Tenant, tenant_id)
            agent = await session.get(Agent, agent_id)
            old_version = await session.get(AgentVersion, old_version_id)
            new_version = await session.get(AgentVersion, activated.agent_version_id)
            assert tenant is not None and tenant.revision == tenant_revision + 1
            assert tenant.settings["web_provider"]["provider"] == "brave"
            assert agent is not None and agent.current_version_id == new_version.id
            assert old_version is not None and old_version.status == "superseded"
            assert old_version.tool_policy == {
                "allow": ["file.read@1.0.0"],
                "permissions": ["filesystem.read"],
                "tools": {"file.read@1.0.0": {"roots": ["workspace"]}},
            }
            assert new_version is not None and new_version.status == "published"
            assert new_version.role == "researcher"
            assert new_version.model_name == "test-model"
            assert {"web.search@1.0.0", "web.fetch@1.0.0"} <= set(new_version.tool_policy["allow"])
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
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_web_activation_rolls_back_tenant_when_version_publish_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        environment="test",
        web_provider_writes_enabled=True,
        _env_file=None,
    )
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    service = WebOnboardingService(database, settings)
    try:
        (
            tenant_id,
            tenant_revision,
            project_id,
            agent_id,
            agent_revision,
            _,
        ) = await _seed_existing_agent(database)
        context = TenantContext(tenant_id, "web-rollback-test", uuid4())
        candidate = WebProviderCandidate(
            provider="searxng",
            endpoint_key="configured",
            catalog_revision=get_web_provider_catalog(settings).catalog_revision,
        )
        probe = await _verified_web_probe(database, service, context, candidate)
        target = WebActivationTarget(
            project_id=project_id,
            expected_tenant_revision=tenant_revision,
            agent_id=agent_id,
            expected_agent_revision=agent_revision,
        )
        preview = await service.preview_activation(
            context,
            WebPreviewCreate(
                probe_id=probe.id,
                candidate_hash=probe.candidate_hash,
                target=target,
            ),
        )

        async def fail_create(*_args, **_kwargs):
            raise RuntimeError("injected publish failure")

        monkeypatch.setattr(AgentVersionLifecycle, "create", fail_create)
        with pytest.raises(RuntimeError, match="injected publish failure"):
            await service.activate(
                context,
                WebActivationCreate(
                    probe_id=probe.id,
                    candidate_hash=probe.candidate_hash,
                    target=target,
                    preview_hash=preview.preview_hash,
                ),
            )

        async with database.tenant_transaction(context) as session:
            tenant = await session.get(Tenant, tenant_id)
            stored_probe = await session.get(ProviderProbe, probe.id)
            assert tenant is not None and tenant.revision == tenant_revision
            assert "web_provider" not in tenant.settings
            assert stored_probe is not None and stored_probe.status == "succeeded"
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AgentVersion)
                    .where(
                        AgentVersion.tenant_id == tenant_id,
                        AgentVersion.agent_id == agent_id,
                    )
                )
                == 1
            )

        with pytest.raises(DomainConflict, match="tenant revision"):
            await service.preview_activation(
                context,
                WebPreviewCreate(
                    probe_id=probe.id,
                    candidate_hash=probe.candidate_hash,
                    target=target.model_copy(
                        update={"expected_tenant_revision": tenant_revision + 1}
                    ),
                ),
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_web_activation_can_publish_a_starter_agent_from_verified_model_route() -> None:
    settings = Settings(
        environment="test",
        web_provider_writes_enabled=True,
        _env_file=None,
    )
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    service = WebOnboardingService(database, settings)
    try:
        tenant_id, tenant_revision, project_id, _, _, _ = await _seed_existing_agent(database)
        context = TenantContext(tenant_id, "web-starter-test", uuid4())
        candidate = WebProviderCandidate(
            provider="searxng",
            endpoint_key="configured",
            catalog_revision=get_web_provider_catalog(settings).catalog_revision,
        )
        probe = await _verified_web_probe(database, service, context, candidate)
        target = WebActivationTarget(
            project_id=project_id,
            expected_tenant_revision=tenant_revision,
            starter_agent_name=f"web-starter-{uuid4().hex[:10]}",
            starter_agent_display_name="Web Starter",
        )
        preview = await service.preview_activation(
            context,
            WebPreviewCreate(
                probe_id=probe.id,
                candidate_hash=probe.candidate_hash,
                target=target,
            ),
        )
        result = await service.activate(
            context,
            WebActivationCreate(
                probe_id=probe.id,
                candidate_hash=probe.candidate_hash,
                target=target,
                preview_hash=preview.preview_hash,
            ),
        )

        async with database.tenant_transaction(context) as session:
            agent = await session.get(Agent, result.agent_id)
            version = await session.get(AgentVersion, result.agent_version_id)
            assert agent is not None and agent.status == "ready"
            assert version is not None and version.status == "published"
            assert version.execution_mode == "react"
            assert version.model_name == "test-model"
            assert version.tool_policy["secrets"] == []
            assert version.tool_policy["tools"]["web.search@1.0.0"]["provider"] == ("searxng")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_durable_worker_verifies_searxng_without_a_fake_secret() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    service = WebOnboardingService(database, settings)
    try:
        tenant_id, _, _, _, _, _ = await _seed_existing_agent(database)
        context = TenantContext(tenant_id, "web-worker-test", uuid4())
        probe = await service.create_probe(
            context,
            WebProbeCreate(
                candidate=WebProviderCandidate(
                    provider="searxng",
                    endpoint_key="configured",
                    catalog_revision=get_web_provider_catalog(settings).catalog_revision,
                ),
                idempotency_key=f"web-worker-{uuid4().hex}",
            ),
        )
        worker = ProviderProbeWorker(
            database,
            None,  # type: ignore[arg-type]
            worker_id=f"web-worker-{uuid4().hex[:8]}",
            web_providers=WebProviderRegistry([EmptySearchProvider()]),
        )

        assert await worker.execute_once() is True

        completed = await service.get_probe(context, probe.id)
        assert completed.status == "succeeded"
        assert completed.verified_at is not None
        assert completed.result["provider"] == "searxng"
        assert completed.result["result_count"] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_web_status_test_and_disable_preserve_immutable_versions() -> None:
    settings = Settings(
        environment="test",
        web_provider_writes_enabled=True,
        _env_file=None,
    )
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    service = WebOnboardingService(database, settings)
    try:
        (
            tenant_id,
            tenant_revision,
            project_id,
            agent_id,
            agent_revision,
            old_version_id,
        ) = await _seed_existing_agent(database)
        context = TenantContext(tenant_id, "web-disable-test", uuid4())
        candidate = WebProviderCandidate(
            provider="searxng",
            endpoint_key="configured",
            catalog_revision=get_web_provider_catalog(settings).catalog_revision,
        )
        probe = await _verified_web_probe(database, service, context, candidate)
        target = WebActivationTarget(
            project_id=project_id,
            expected_tenant_revision=tenant_revision,
            agent_id=agent_id,
            expected_agent_revision=agent_revision,
        )
        activation_preview = await service.preview_activation(
            context,
            WebPreviewCreate(
                probe_id=probe.id,
                candidate_hash=probe.candidate_hash,
                target=target,
            ),
        )
        activated = await service.activate(
            context,
            WebActivationCreate(
                probe_id=probe.id,
                candidate_hash=probe.candidate_hash,
                target=target,
                preview_hash=activation_preview.preview_hash,
            ),
        )

        ready = await service.status(context)
        assert ready.diagnosis == "ready"
        assert ready.authorized is True
        assert ready.provider == "searxng"
        assert [agent.id for agent in ready.agents] == [agent_id]

        test_probe = await service.test_configuration(
            context,
            WebConfigurationTestCreate(idempotency_key=f"web-test-{uuid4().hex}"),
        )
        assert test_probe.status == "pending"
        async with database.tenant_transaction(context) as session:
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

        disable_target = WebDisableTarget(
            agent_id=agent_id,
            expected_tenant_revision=activated.tenant_revision,
            expected_agent_revision=activated.agent_revision,
        )
        disable_preview = await service.preview_disable(
            context,
            WebDisablePreviewCreate(target=disable_target),
        )
        disabled = await service.disable(
            context,
            WebDisableCreate(
                target=disable_target,
                preview_hash=disable_preview.preview_hash,
            ),
        )

        assert disabled.agent_version == 3
        after = await service.status(context)
        assert after.configured is True
        assert after.enabled is False
        assert after.authorized is False
        assert after.diagnosis == "unauthorized"
        async with database.tenant_transaction(context) as session:
            tenant = await session.get(Tenant, tenant_id)
            agent = await session.get(Agent, agent_id)
            original = await session.get(AgentVersion, old_version_id)
            web_version = await session.get(AgentVersion, activated.agent_version_id)
            disabled_version = await session.get(AgentVersion, disabled.agent_version_id)
            assert tenant is not None
            assert tenant.settings["web_provider"]["enabled"] is False
            assert tenant.settings["tool_policy"]["allow"] == ["file.read@1.0.0"]
            assert agent is not None and agent.current_version_id == disabled_version.id
            assert original is not None and original.tool_policy["allow"] == ["file.read@1.0.0"]
            assert web_version is not None
            assert "web.search@1.0.0" in web_version.tool_policy["allow"]
            assert disabled_version is not None
            assert disabled_version.tool_policy["allow"] == ["file.read@1.0.0"]
            assert disabled_version.status == "published"
    finally:
        await engine.dispose()
