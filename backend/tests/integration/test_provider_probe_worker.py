from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import Settings
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainConflict, ResourceNotFound
from nico_agent.domain.models import Tenant
from nico_agent.models import (
    DiscoveredModel,
    ModelDiscoveryResult,
    ModelGateway,
    ModelProviderRegistry,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelUsage,
)
from nico_agent.models.contracts import ModelCapability
from nico_agent.models.errors import ModelProviderError
from nico_agent.provider_onboarding.contracts import (
    CandidateConfiguration,
    ProviderProbeCreate,
)
from nico_agent.provider_onboarding.service import ProviderOnboardingService
from nico_agent.provider_onboarding.worker import ProviderProbeWorker

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


class FakeProvider:
    name = "openai_compatible"

    def describe_capabilities(self):
        return frozenset({ModelCapability.STREAMING})

    async def stream(self, request):
        yield ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED)
        yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text_delta="private text")
        yield ModelStreamEvent(
            type=ModelStreamEventType.RESPONSE_COMPLETED,
            finish_reason="stop",
            usage=ModelUsage(
                input_tokens=3,
                output_tokens=2,
                total_tokens=5,
                status="exact",
            ),
            provider_request_id="fake-request",
        )

    async def discover(self, request):
        return ModelDiscoveryResult(
            models=(DiscoveredModel(id="model-a"), DiscoveredModel(id="model-b")),
        )


class FailingProvider(FakeProvider):
    async def stream(self, request):
        if False:
            yield
        raise ModelProviderError(
            "MODEL_AUTH_FAILED",
            "upstream body contained sk-canary-value",
        )


def _candidate(*, model: str | None = "model-a") -> CandidateConfiguration:
    return CandidateConfiguration(
        provider_key="openai",
        protocol="openai_compatible",
        base_url="https://api.openai.com/v1",
        credential_ref="env:NICO_MODEL_SECRET_TEST",
        model=model,
        catalog_revision="2026-07-20",
    )


@pytest.mark.asyncio
async def test_worker_verifies_without_persisting_generated_text_and_isolates_tenants() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    service = ProviderOnboardingService(database)
    try:
        suffix = uuid4().hex[:10]
        async with database.admin_transaction() as session:
            tenant = Tenant(name=f"Probe {suffix}", slug=f"probe-{suffix}")
            other = Tenant(name=f"Probe other {suffix}", slug=f"probe-other-{suffix}")
            session.add_all([tenant, other])
            await session.flush()
            tenant_id, other_id = tenant.id, other.id
        context = TenantContext(tenant_id, "probe-test", uuid4())
        command = ProviderProbeCreate(
            kind="verify_completion",
            candidate=_candidate(),
            idempotency_key=f"verify-{suffix}",
        )

        created = await service.create_probe(context, command)
        repeated = await service.create_probe(context, command)
        with pytest.raises(DomainConflict, match="idempotency"):
            await service.create_probe(
                context,
                command.model_copy(
                    update={
                        "candidate": _candidate(model="model-b"),
                    }
                ),
            )

        worker = ProviderProbeWorker(
            database,
            ModelGateway(ModelProviderRegistry([FakeProvider()]), max_attempts=1),
            worker_id=f"probe-worker-{suffix}",
            lease_seconds=90,
        )
        assert await worker.execute_once() is True
        result = await service.get_probe(context, created.id)

        assert repeated.id == created.id
        assert result.status == "succeeded"
        assert result.result["response_present"] is True
        assert result.result["usage"]["total_tokens"] == 5
        assert result.result["provider_request_id"] == "fake-request"
        assert "private text" not in str(result.result)
        assert result.verified_at is not None
        with pytest.raises(ResourceNotFound):
            await service.get_probe(
                TenantContext(other_id, "cross-tenant", uuid4()),
                created.id,
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_persists_bounded_discovery_metadata() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    service = ProviderOnboardingService(database)
    try:
        suffix = uuid4().hex[:10]
        async with database.admin_transaction() as session:
            tenant = Tenant(name=f"Discovery {suffix}", slug=f"discovery-{suffix}")
            session.add(tenant)
            await session.flush()
            tenant_id = tenant.id
        context = TenantContext(tenant_id, "probe-test", uuid4())
        created = await service.create_probe(
            context,
            ProviderProbeCreate(
                kind="discover_models",
                candidate=_candidate(model=None),
                idempotency_key=f"discover-{suffix}",
            ),
        )
        worker = ProviderProbeWorker(
            database,
            ModelGateway(ModelProviderRegistry([FakeProvider()]), max_attempts=1),
            worker_id=f"discovery-worker-{suffix}",
            lease_seconds=90,
        )

        assert await worker.execute_once() is True
        result = await service.get_probe(context, created.id)

        assert result.status == "succeeded"
        assert [item["id"] for item in result.result["models"]] == ["model-a", "model-b"]
        assert result.result["truncated"] is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_persists_stable_error_category_without_upstream_body() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    service = ProviderOnboardingService(database)
    try:
        suffix = uuid4().hex[:10]
        async with database.admin_transaction() as session:
            tenant = Tenant(name=f"Failure {suffix}", slug=f"failure-{suffix}")
            session.add(tenant)
            await session.flush()
            tenant_id = tenant.id
        context = TenantContext(tenant_id, "probe-test", uuid4())
        created = await service.create_probe(
            context,
            ProviderProbeCreate(
                kind="verify_completion",
                candidate=_candidate(),
                idempotency_key=f"failure-{suffix}",
            ),
        )
        worker = ProviderProbeWorker(
            database,
            ModelGateway(ModelProviderRegistry([FailingProvider()]), max_attempts=1),
            worker_id=f"failure-worker-{suffix}",
            lease_seconds=90,
        )

        assert await worker.execute_once() is True
        result = await service.get_probe(context, created.id)

        assert result.status == "failed"
        assert result.error_code == "PROVIDER_AUTH_FAILED"
        assert result.error_detail == "Provider authentication failed."
        assert "sk-canary-value" not in str(result.result)
        assert "sk-canary-value" not in result.error_detail
    finally:
        await engine.dispose()
