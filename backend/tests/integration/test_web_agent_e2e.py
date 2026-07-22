from __future__ import annotations

import os
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from sqlalchemy import select
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
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    ModelEndpoint,
    Project,
    ProviderProbe,
    Run,
    Task,
    Tenant,
    ToolApprovalRequest,
    ToolCall,
)
from nico_agent.guided_setup.contracts import SetupProofCreate
from nico_agent.guided_setup.service import GuidedSetupService
from nico_agent.models.contracts import ModelCapability
from nico_agent.models.gateway import ModelGateway
from nico_agent.models.registry import ModelProviderRegistry
from nico_agent.net.safe_http import SafeHttpClient
from nico_agent.provider_onboarding.worker import ProviderProbeWorker
from nico_agent.runtime import NicoNativeRuntimeProvider, RuntimeProviderRegistry
from nico_agent.runtime.executor import RuntimeWorker
from nico_agent.testing.fake_web import (
    EVIDENCE_URL,
    FakeWebTransport,
    fake_web_resolver,
)
from nico_agent.tool_approvals.contracts import ToolApprovalDecision
from nico_agent.tool_approvals.service import ToolApprovalService
from nico_agent.tools import ToolGateway, ToolRegistry
from nico_agent.tools.builtin import WebFetchExecutor, WebSearchExecutor
from nico_agent.web.providers.searxng import SearxngSearchProvider
from nico_agent.web.registry import WebProviderRegistry
from nico_agent.web.source_authorization import WebSourceAuthorizer
from nico_agent.web_onboarding.catalog import get_web_provider_catalog
from nico_agent.web_onboarding.contracts import (
    WebActivationCreate,
    WebPreviewCreate,
    WebProbeCreate,
    WebProviderCandidate,
)
from nico_agent.web_onboarding.service import WebOnboardingService

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


class UnexpectedModelProvider:
    name = "openai_compatible"

    def __init__(self) -> None:
        self.requests = []

    def describe_capabilities(self):
        return frozenset({ModelCapability.STREAMING, ModelCapability.TOOLS})

    async def stream(self, request):
        self.requests.append(request)
        raise AssertionError("setup proof must not call the model")
        yield  # pragma: no cover


async def _seed_agent(database: Database):
    suffix = uuid4().hex[:10]
    now = datetime.now(UTC)
    async with database.admin_transaction() as session:
        tenant = Tenant(
            name=f"Web E2E {suffix}",
            slug=f"web-e2e-{suffix}",
            settings={
                "tool_policy": {
                    "allow": [],
                    "permissions": [],
                    "tools": {},
                }
            },
        )
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Offline Web Agent",
            status="ready",
        )
        model_probe = ProviderProbe(
            tenant_id=tenant.id,
            kind="verify_completion",
            status="succeeded",
            provider_key="openai",
            protocol="openai_compatible",
            base_url="https://api.openai.com/v1",
            credential_ref="env:NICO_MODEL_SECRET_TEST",
            model_name="web-e2e-model",
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
            stable_key=f"web-e2e-{suffix}",
            revision=1,
            display_name="Offline Web model",
            base_url="https://api.openai.com/v1",
            credential_ref="env:NICO_MODEL_SECRET_TEST",
            allowed_models=["web-e2e-model"],
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
            role="web researcher",
            mandate="Search, read, and cite current public evidence",
            runtime_provider="nico_native",
            execution_mode="react",
            model_endpoint_id=endpoint.id,
            model_name="web-e2e-model",
            tool_policy={"allow": [], "permissions": [], "tools": {}},
            budgets={"max_iterations": 5, "max_tool_calls": 2},
            content_hash="b" * 64,
        )
        session.add(version)
        await session.flush()
        agent.current_version_id = version.id
        await session.flush()
        return (
            tenant.id,
            tenant.revision,
            project.id,
            agent.id,
            agent.revision,
            version.id,
        )


@pytest.mark.asyncio
async def test_configure_publish_approve_search_fetch_and_cite_offline() -> None:
    settings = Settings(
        environment="test",
        web_provider_writes_enabled=True,
        web_searxng_endpoint="http://fake-web:8120/search",
        web_searxng_allow_private=True,
        _env_file=None,
    )
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    transport = FakeWebTransport()
    http = SafeHttpClient(transport=transport, resolver=fake_web_resolver)
    providers = WebProviderRegistry(
        [
            SearxngSearchProvider(
                endpoint=settings.web_searxng_endpoint,
                http=http,
                allow_private=True,
            )
        ]
    )
    onboarding = WebOnboardingService(database, settings)
    try:
        (
            tenant_id,
            tenant_revision,
            project_id,
            agent_id,
            agent_revision,
            old_version_id,
        ) = await _seed_agent(database)
        context = TenantContext(tenant_id, "web-e2e-operator", uuid4())
        candidate = WebProviderCandidate(
            provider="searxng",
            endpoint_key="configured",
            catalog_revision=get_web_provider_catalog(settings).catalog_revision,
        )
        probe = await onboarding.create_probe(
            context,
            WebProbeCreate(
                candidate=candidate,
                idempotency_key=f"web-e2e-probe-{uuid4().hex}",
            ),
        )
        probe_worker = ProviderProbeWorker(
            database,
            None,  # type: ignore[arg-type]
            worker_id=f"web-e2e-probe-{uuid4().hex[:8]}",
            web_providers=providers,
        )
        for _ in range(10):
            completed_probe = await onboarding.get_probe(context, probe.id)
            if completed_probe.status != "pending":
                break
            assert await probe_worker.execute_once() is True
        completed_probe = await onboarding.get_probe(context, probe.id)
        assert completed_probe.status == "succeeded"

        preview = await onboarding.preview_activation(
            context,
            WebPreviewCreate(
                probe_id=probe.id,
                candidate_hash=probe.candidate_hash,
                scope="provider_only",
                expected_tenant_revision=tenant_revision,
            ),
        )
        web_activation = await onboarding.activate(
            context,
            WebActivationCreate(
                probe_id=probe.id,
                candidate_hash=probe.candidate_hash,
                scope="provider_only",
                expected_tenant_revision=tenant_revision,
                preview_hash=preview.preview_hash,
            ),
        )
        assert web_activation.agent_version_id is None
        capabilities = AgentCapabilityService(database)
        target = CapabilityTarget(
            agent_id=agent_id,
            expected_agent_revision=agent_revision,
        )
        selection = CapabilitySelection(profile="web_research")
        capability_preview = await capabilities.preview(
            context,
            CapabilityPreviewCreate(
                expected_tenant_revision=web_activation.tenant_revision,
                target=target,
                selection=selection,
            ),
        )
        activated = await capabilities.activate(
            context,
            CapabilityActivationCreate(
                expected_tenant_revision=web_activation.tenant_revision,
                target=target,
                selection=selection,
                preview_hash=capability_preview.preview_hash,
                accepted_risks=("medium",),
            ),
        )
        assert activated.agent_version == 2
        guided_setup = GuidedSetupService(database, settings)

        async with database.admin_transaction() as session:
            old_version = await session.get(AgentVersion, old_version_id)
            version = await session.get(AgentVersion, activated.agent_version_id)
            assert old_version is not None and old_version.status == "superseded"
            assert version is not None and version.status == "published"
            task = Task(
                tenant_id=tenant_id,
                project_id=project_id,
                assignee_agent_id=agent_id,
                title="Offline Web acceptance",
                input={"prompt": "Find, read, and cite current Nico evidence"},
                status="running",
                priority=2_147_483_647,
            )
            session.add(task)
            await session.flush()
            run = Run(
                tenant_id=tenant_id,
                task_id=task.id,
                agent_id=agent_id,
                agent_version_id=version.id,
                attempt=1,
                max_steps=5,
                token_budget=1000,
                timeout_seconds=120,
                budgets={
                    "tool_allow": ["web.search@1.0.0", "web.fetch@1.1.0"],
                    "setup_proof": True,
                },
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        model = UnexpectedModelProvider()
        worker = RuntimeWorker(
            database,
            RuntimeProviderRegistry(
                [NicoNativeRuntimeProvider(ModelGateway(ModelProviderRegistry([model])))]
            ),
            worker_id=f"web-e2e-runtime-{uuid4().hex[:8]}",
            lease_seconds=10,
            heartbeat_seconds=1,
            tool_gateway=ToolGateway(
                database,
                ToolRegistry(
                    [
                        WebSearchExecutor(providers, environment="test"),
                        WebFetchExecutor(WebSourceAuthorizer(database), http=http),
                    ]
                ),
            ),
        )
        approval_service = ToolApprovalService(database)
        approved_ids = []
        for _ in range(5):
            assert await worker.execute_once() is True
            async with database.admin_transaction() as session:
                run = await session.get(Run, run_id)
                requested = await session.scalar(
                    select(ToolApprovalRequest)
                    .where(
                        ToolApprovalRequest.run_id == run_id,
                        ToolApprovalRequest.status == "requested",
                    )
                    .order_by(ToolApprovalRequest.created_at.desc())
                )
            assert run is not None
            if run.status == "completed":
                break
            assert run.status == "waiting_for_approval" and requested is not None
            await approval_service.decide(
                TenantContext(tenant_id, "web-e2e-approver", uuid4()),
                requested.id,
                ToolApprovalDecision(
                    expected_revision=requested.revision,
                    decision="approve",
                    allowed_scope="run",
                ),
                idempotency_key=f"approve-{requested.id}",
            )
            approved_ids.append(requested.id)

        async with database.admin_transaction() as session:
            completed = await session.get(Run, run_id)
            calls = list(
                await session.scalars(
                    select(ToolCall)
                    .where(ToolCall.run_id == run_id)
                    .order_by(ToolCall.created_at, ToolCall.id)
                )
            )
            approvals = list(
                await session.scalars(
                    select(ToolApprovalRequest).where(ToolApprovalRequest.run_id == run_id)
                )
            )

        assert completed is not None and completed.status == "completed"
        assert completed.result == {
            "content": f"Online verification succeeded. Source: {EVIDENCE_URL}",
            "verification": {
                "search": "completed",
                "fetch": "completed",
                "source_url": EVIDENCE_URL,
            },
        }
        assert [call.tool_name for call in calls] == ["web.search", "web.fetch"]
        assert all(call.status == "succeeded" and len(call.attempts) == 1 for call in calls)
        assert calls[1].arguments["search_tool_call_id"] == str(calls[0].id)
        assert model.requests == []
        assert calls[0].result["external_content"]["untrusted"] is True
        assert calls[1].result["external_content"]["untrusted"] is True
        assert {approval.id for approval in approvals} == set(approved_ids)
        assert all(approval.status == "approved" for approval in approvals)
        assert [
            (request.hostname, urlsplit(request.target).path) for request in transport.requests
        ] == [
            ("fake-web", "/search"),
            ("fake-web", "/search"),
            ("evidence.example", "/article"),
        ]
        proof = await guided_setup.validate_proof(
            context,
            SetupProofCreate(
                expected_tenant_revision=activated.tenant_revision,
                run_id=run_id,
            ),
        )
        assert proof.state == "succeeded", proof
        readiness = await guided_setup.readiness(context)
        assert readiness.overall == "full"
        assert readiness.target is not None
        assert readiness.target.agent_version_id == activated.agent_version_id
        assert {area.key: area.state for area in readiness.areas} == {
            "model": "ready",
            "web": "ready",
            "capabilities": "ready",
            "verification": "ready",
        }
    finally:
        await engine.dispose()
