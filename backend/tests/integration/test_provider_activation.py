from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import Settings
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainConflict
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    Conversation,
    ModelEndpoint,
    Project,
    Tenant,
)
from nico_agent.projects.contracts import ProjectPreflightRequest
from nico_agent.projects.service import ProjectCollaborationService
from nico_agent.provider_onboarding.contracts import (
    CandidateConfiguration,
    ProviderActivationCreate,
    ProviderActivationTarget,
    ProviderPreviewCreate,
    ProviderProbeCreate,
)
from nico_agent.provider_onboarding.service import ProviderOnboardingService

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


def _candidate(
    *, credential_ref: str = "secret:provider-activation-test"
) -> CandidateConfiguration:
    return CandidateConfiguration(
        provider_key="openai",
        protocol="openai_compatible",
        base_url="https://api.openai.com/v1",
        credential_ref=credential_ref,
        model="gpt-5.6-terra",
        catalog_revision="2026-07-20",
    )


async def _verified_probe(
    database: Database,
    service: ProviderOnboardingService,
    context: TenantContext,
    *,
    candidate: CandidateConfiguration | None = None,
):
    probe = await service.create_probe(
        context,
        ProviderProbeCreate(
            kind="verify_completion",
            candidate=candidate or _candidate(),
            idempotency_key=f"activation-{uuid4().hex}",
        ),
    )
    async with database.tenant_transaction(context) as session:
        stored = await service._probe(session, context, probe.id, for_update=True)
        now = datetime.now(UTC)
        stored.status = "succeeded"
        stored.result = {"response_present": True}
        stored.completed_at = now
        stored.verified_at = now
        stored.revision += 1
        await session.flush()
    return await service.get_probe(context, probe.id)


@pytest.mark.asyncio
async def test_verified_probe_activates_endpoint_and_starter_agent_atomically() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    service = ProviderOnboardingService(database)
    try:
        suffix = uuid4().hex[:10]
        async with database.admin_transaction() as session:
            tenant = Tenant(name=f"Activation {suffix}", slug=f"activation-{suffix}")
            session.add(tenant)
            await session.flush()
            project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
            session.add(project)
            await session.flush()
            tenant_id, project_id = tenant.id, project.id
        context = TenantContext(tenant_id, "activation-test", uuid4())
        probe = await _verified_probe(database, service, context)
        before = await service.setup_readiness(context)
        assert before.needs_setup is True
        assert before.reason == "verified_native_route_required"
        target = ProviderActivationTarget(
            project_id=project_id,
            starter_agent_name=f"starter-{suffix}",
            starter_agent_display_name="Starter Assistant",
        )
        preview = await service.preview_activation(
            context,
            ProviderPreviewCreate(
                probe_id=probe.id,
                candidate_hash=probe.candidate_hash,
                target=target,
            ),
        )

        result = await service.activate(
            context,
            ProviderActivationCreate(
                probe_id=probe.id,
                candidate_hash=probe.candidate_hash,
                target=target,
                preview_hash=preview.preview_hash,
            ),
        )
        repeated = await service.activate(
            context,
            ProviderActivationCreate(
                probe_id=probe.id,
                candidate_hash=probe.candidate_hash,
                target=target,
                preview_hash=preview.preview_hash,
            ),
        )

        assert repeated == result
        assert result.endpoint_reused is False
        after = await service.setup_readiness(context)
        connections = await service.list_connections(context)
        assert after.needs_setup is False
        assert after.verified_native_route_count == 1
        assert len(connections) == 1
        assert connections[0].credential_ref == "secret:provider-activation-test"
        assert connections[0].active_agents[0]["id"] == str(result.agent_id)
        async with database.tenant_transaction(context) as session:
            endpoint = await session.get(ModelEndpoint, result.endpoint_id)
            agent = await session.get(Agent, result.agent_id)
            version = await session.get(AgentVersion, result.agent_version_id)
            assert endpoint is not None and endpoint.verified_probe_id == probe.id
            assert agent is not None and agent.current_version_id == version.id
            assert agent.status == "ready"
            assert version is not None and version.status == "published"
            assert version.model_endpoint_id == endpoint.id
            assert version.model_name == "gpt-5.6-terra"
            assert version.execution_mode == "react"
            assert version.coordination_policy["enabled"] is True
            assert version.coordination_policy["allowed_target_scopes"] == ["project_members"]
            tenant = await session.get(Tenant, tenant_id)
            assert tenant is not None
            tenant_policy = tenant.settings["coordination_policy"]
            assert tenant_policy["enabled"] is True
            assert "project_members" in tenant_policy["allowed_target_scopes"]
            conversation = Conversation(
                tenant_id=tenant_id,
                project_id=project_id,
                agent_id=agent.id,
                agent_version_id=version.id,
                title="Frozen before Provider rotation",
                created_by="activation-test",
                idempotency_key=f"frozen-{suffix}",
            )
            session.add(conversation)
            await session.flush()
            conversation_id, first_version_id = conversation.id, version.id

        preflight = await ProjectCollaborationService(database).preflight(
            context,
            ProjectPreflightRequest(lead_agent_id=result.agent_id, member_agent_ids=[]),
        )
        assert preflight.compatible is True

        rotated_probe = await _verified_probe(database, service, context)
        rotation_target = ProviderActivationTarget(
            project_id=project_id,
            agent_id=result.agent_id,
            expected_agent_revision=result.agent_revision,
        )
        rotated_preview = await service.preview_activation(
            context,
            ProviderPreviewCreate(
                probe_id=rotated_probe.id,
                candidate_hash=rotated_probe.candidate_hash,
                target=rotation_target,
            ),
        )
        rotated = await service.activate(
            context,
            ProviderActivationCreate(
                probe_id=rotated_probe.id,
                candidate_hash=rotated_probe.candidate_hash,
                target=rotation_target,
                preview_hash=rotated_preview.preview_hash,
            ),
        )

        assert rotated.endpoint_reused is True
        assert rotated.endpoint_id == result.endpoint_id
        assert rotated.agent_version == 2
        async with database.tenant_transaction(context) as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ModelEndpoint)
                    .where(
                        ModelEndpoint.tenant_id == tenant_id,
                        ModelEndpoint.stable_key == "openai",
                    )
                )
                == 1
            )
            frozen = await session.get(Conversation, conversation_id)
            first_version = await session.get(AgentVersion, first_version_id)
            second_version = await session.get(AgentVersion, rotated.agent_version_id)
            assert frozen is not None and frozen.agent_version_id == first_version_id
            assert first_version is not None and first_version.status == "superseded"
            assert second_version is not None and second_version.status == "published"
            assert second_version.role == first_version.role
            assert second_version.mandate == first_version.mandate
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_custom_provider_activates_with_its_own_identity() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    service = ProviderOnboardingService(database)
    try:
        suffix = uuid4().hex[:10]
        async with database.admin_transaction() as session:
            tenant = Tenant(name=f"Custom activation {suffix}", slug=f"custom-{suffix}")
            session.add(tenant)
            await session.flush()
            project = Project(tenant_id=tenant.id, name=f"custom-project-{suffix}")
            session.add(project)
            await session.flush()
            tenant_id, project_id = tenant.id, project.id
        context = TenantContext(tenant_id, "custom-activation-test", uuid4())
        candidate = CandidateConfiguration(
            provider_key=f"custom-acme-{suffix}",
            protocol="openai_compatible",
            base_url="https://models.example.com/v1",
            credential_ref=f"secret:providers/custom-acme-{suffix}",
            model="acme-chat",
            provider_options={"nico_custom_display_name": "Acme Models"},
            catalog_revision="2026-07-20",
        )
        probe = await _verified_probe(database, service, context, candidate=candidate)
        target = ProviderActivationTarget(
            project_id=project_id,
            starter_agent_name=f"custom-starter-{suffix}",
            starter_agent_display_name="Custom Assistant",
        )
        preview = await service.preview_activation(
            context,
            ProviderPreviewCreate(
                probe_id=probe.id,
                candidate_hash=probe.candidate_hash,
                target=target,
            ),
        )
        result = await service.activate(
            context,
            ProviderActivationCreate(
                probe_id=probe.id,
                candidate_hash=probe.candidate_hash,
                target=target,
                preview_hash=preview.preview_hash,
            ),
        )

        connection = (await service.list_connections(context))[0]
        assert connection.endpoint_id == result.endpoint_id
        assert connection.provider_key == f"custom-acme-{suffix}"
        assert connection.display_name == "Acme Models"
        assert connection.allowed_models == ("acme-chat",)

        rebound = candidate.model_copy(update={"base_url": "https://other.example.com/v1"})
        with pytest.raises(DomainConflict) as conflict:
            await service.create_probe(
                context,
                ProviderProbeCreate(
                    kind="verify_completion",
                    candidate=rebound,
                    idempotency_key=f"custom-rebind-{suffix}",
                ),
            )
        assert conflict.value.code == "PROVIDER_CUSTOM_BINDING_CONFLICT"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_preview_rolls_back_without_partial_activation() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    service = ProviderOnboardingService(database)
    try:
        suffix = uuid4().hex[:10]
        async with database.admin_transaction() as session:
            tenant = Tenant(name=f"Stale {suffix}", slug=f"stale-{suffix}")
            session.add(tenant)
            await session.flush()
            project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
            session.add(project)
            await session.flush()
            tenant_id, project_id = tenant.id, project.id
        context = TenantContext(tenant_id, "activation-test", uuid4())
        probe = await _verified_probe(database, service, context)
        target = ProviderActivationTarget(
            project_id=project_id,
            starter_agent_name=f"cancelled-{suffix}",
            starter_agent_display_name="Not Created",
        )

        with pytest.raises(DomainConflict, match="preview changed"):
            await service.activate(
                context,
                ProviderActivationCreate(
                    probe_id=probe.id,
                    candidate_hash=probe.candidate_hash,
                    target=target,
                    preview_hash="0" * 64,
                ),
            )

        async with database.tenant_transaction(context) as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Agent)
                    .where(Agent.tenant_id == tenant_id, Agent.name == f"cancelled-{suffix}")
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(ModelEndpoint)
                    .where(
                        ModelEndpoint.tenant_id == tenant_id,
                        ModelEndpoint.verified_probe_id == probe.id,
                    )
                )
                == 0
            )
            stored = await service._probe(session, context, probe.id)
            assert stored.status == "succeeded"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_supplied_local_maintenance_attempt_must_still_be_live() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    service = ProviderOnboardingService(database)
    try:
        suffix = uuid4().hex[:10]
        async with database.admin_transaction() as session:
            tenant = Tenant(name=f"Lease {suffix}", slug=f"lease-activation-{suffix}")
            session.add(tenant)
            await session.flush()
            project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
            session.add(project)
            await session.flush()
            tenant_id, project_id = tenant.id, project.id
        context = TenantContext(tenant_id, "activation-test", uuid4())
        probe = await _verified_probe(
            database,
            service,
            context,
            candidate=_candidate(credential_ref="env:NICO_MODEL_SECRET_ACTIVATION_LEASE_TEST"),
        )
        target = ProviderActivationTarget(
            project_id=project_id,
            starter_agent_name=f"lease-{suffix}",
            starter_agent_display_name="Lease Protected",
        )
        preview = await service.preview_activation(
            context,
            ProviderPreviewCreate(
                probe_id=probe.id,
                candidate_hash=probe.candidate_hash,
                target=target,
            ),
        )

        with pytest.raises(DomainConflict, match="lease is no longer active"):
            await service.activate(
                context,
                ProviderActivationCreate(
                    probe_id=probe.id,
                    candidate_hash=probe.candidate_hash,
                    target=target,
                    preview_hash=preview.preview_hash,
                    maintenance_attempt_id=uuid4(),
                ),
            )

        async with database.tenant_transaction(context) as session:
            stored = await service._probe(session, context, probe.id)
            assert stored.status == "succeeded"
            assert (
                await session.scalar(
                    select(func.count()).select_from(Agent).where(Agent.tenant_id == tenant_id)
                )
                == 0
            )
    finally:
        await engine.dispose()
