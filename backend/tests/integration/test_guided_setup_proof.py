from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import Settings
from nico_agent.database import Database, TenantContext
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    ModelEndpoint,
    Project,
    ProviderProbe,
    Run,
    Task,
    Tenant,
)
from nico_agent.guided_setup.contracts import SetupProofCreate
from nico_agent.guided_setup.service import GuidedSetupService, merge_guided_setup_ledger
from nico_agent.local_defaults import local_tenant_settings

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


async def _seed_pending_proof(database: Database):
    suffix = uuid4().hex[:10]
    now = datetime.now(UTC)
    candidate_hash = "c" * 64
    async with database.admin_transaction() as session:
        tenant = Tenant(
            name=f"Guided proof {suffix}",
            slug=f"guided-proof-{suffix}",
            settings=local_tenant_settings(),
        )
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Guided Proof Agent",
            status="ready",
        )
        probe = ProviderProbe(
            tenant_id=tenant.id,
            kind="verify_completion",
            status="succeeded",
            provider_key="openai",
            protocol="openai_compatible",
            base_url="https://models.example/v1",
            credential_ref="env:NICO_MODEL_SECRET_TEST",
            model_name="proof-model",
            catalog_revision="test",
            candidate_hash="a" * 64,
            idempotency_key=f"model-{suffix}",
            result={"response_present": True},
            completed_at=now,
            verified_at=now,
        )
        session.add_all([project, agent, probe])
        await session.flush()
        endpoint = ModelEndpoint(
            tenant_id=tenant.id,
            stable_key=f"proof-{suffix}",
            revision=1,
            display_name="Proof model",
            base_url="https://models.example/v1",
            credential_ref="env:NICO_MODEL_SECRET_TEST",
            allowed_models=["proof-model"],
            capabilities={"streaming": True, "tools": True},
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
            role="researcher",
            mandate="Verify Web evidence",
            runtime_provider="nico_native",
            execution_mode="react",
            model_endpoint_id=endpoint.id,
            model_name="proof-model",
            tool_policy={},
            content_hash="b" * 64,
        )
        session.add(version)
        await session.flush()
        agent.current_version_id = version.id
        tenant.settings = merge_guided_setup_ledger(
            {
                **tenant.settings,
                "web_provider": {
                    "enabled": True,
                    "provider": "searxng",
                    "candidate_hash": candidate_hash,
                },
            },
            web_intent="enabled",
            selected_agent_id=agent.id,
            selected_profile="web_research",
            capability_agent_version_id=version.id,
        )
        task = Task(
            tenant_id=tenant.id,
            project_id=project.id,
            assignee_agent_id=agent.id,
            title="Pending proof",
            input={"prompt": "Search and fetch"},
            status="running",
        )
        session.add(task)
        await session.flush()
        run = Run(
            tenant_id=tenant.id,
            task_id=task.id,
            agent_id=agent.id,
            agent_version_id=version.id,
            attempt=1,
            status="running",
            max_steps=8,
            token_budget=4_000,
            timeout_seconds=120,
        )
        session.add(run)
        await session.flush()
        return tenant.id, tenant.revision, run.id


@pytest.mark.asyncio
async def test_proof_failure_is_classified_and_run_lookup_is_tenant_scoped() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    service = GuidedSetupService(database, settings)
    try:
        first_tenant, first_revision, first_run = await _seed_pending_proof(database)
        second_tenant, second_revision, _second_run = await _seed_pending_proof(database)

        pending = await service.validate_proof(
            TenantContext(first_tenant, "proof-operator", uuid4()),
            SetupProofCreate(
                expected_tenant_revision=first_revision,
                run_id=first_run,
            ),
        )
        assert pending.state == "failed"
        assert pending.failure_class == "run_not_completed"

        isolated = await service.validate_proof(
            TenantContext(second_tenant, "proof-operator", uuid4()),
            SetupProofCreate(
                expected_tenant_revision=second_revision,
                run_id=first_run,
            ),
        )
        assert isolated.state == "failed"
        assert isolated.failure_class == "run_not_found"
    finally:
        await engine.dispose()
