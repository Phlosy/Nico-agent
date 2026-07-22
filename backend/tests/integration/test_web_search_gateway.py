from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import Settings
from nico_agent.database import Database, TenantContext
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    AuditRecord,
    Event,
    Project,
    Run,
    Task,
    Tenant,
    ToolApprovalRequest,
    ToolCall,
)
from nico_agent.domain.states import ToolCallStatus
from nico_agent.runtime import MockRuntimeProvider
from nico_agent.runtime.registry import RuntimeProviderRegistry
from nico_agent.runtime.service import RuntimeExecutionService
from nico_agent.tool_approvals.contracts import ToolApprovalDecision
from nico_agent.tool_approvals.service import ToolApprovalService
from nico_agent.tools import ToolGateway, ToolGatewayRequest, ToolRegistry
from nico_agent.tools.builtin import WebSearchExecutor
from nico_agent.tools.contracts import canonical_hash
from nico_agent.tools.errors import ToolApprovalRequired
from nico_agent.tools.secrets import EnvironmentSecretResolver
from nico_agent.web.contracts import SearchPage, SearchResult
from nico_agent.web.registry import WebProviderRegistry

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


@dataclass
class FakeProvider:
    key: str
    calls: int = 0

    async def search(self, request, *, secret=None, config=None):
        self.calls += 1
        assert secret == "brave-test-secret"
        return SearchPage(
            provider="brave",
            results=(
                SearchResult(
                    title="Nico docs",
                    url="https://docs.example/nico",
                    snippet="Current docs",
                ),
            ),
        )


@dataclass
class UnusedProvider:
    key: str = "searxng"

    async def search(self, request, *, secret=None, config=None):
        raise AssertionError("explicit Brave configuration must not fall back")


async def _seed_run(database: Database, *, worker_id: str, query: str):
    suffix = uuid4().hex[:10]
    spec = WebSearchExecutor.spec
    tool_config = {
        "provider": "brave",
        "safe_search": "moderate",
        "cache_ttl_seconds": 60,
        "rate_limit_per_minute": 20,
    }
    tenant_policy = {
        "allow": [spec.reference],
        "permissions": [spec.permission],
        "secret_refs": {"web_search_brave_api_key": "env:NICO_TOOL_SECRET_WEB_SEARCH_BRAVE_TEST"},
        "tools": {spec.reference: tool_config},
    }
    agent_policy = {
        "allow": [spec.reference],
        "permissions": [spec.permission],
        "secrets": ["web_search_brave_api_key"],
        "tools": {spec.reference: tool_config},
    }
    async with database.admin_transaction() as session:
        tenant = Tenant(
            name=f"Web Search {suffix}",
            slug=f"web-search-{suffix}",
            settings={"tool_policy": tenant_policy},
        )
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Web Search Agent",
        )
        session.add_all([project, agent])
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="web-search-test",
            mandate="Search current public information",
            tool_policy=agent_policy,
            run_config={"runtime_provider": "mock"},
            content_hash="b" * 64,
        )
        session.add(version)
        await session.flush()
        agent.current_version_id = version.id
        agent.status = "ready"
        task = Task(
            tenant_id=tenant.id,
            project_id=project.id,
            assignee_agent_id=agent.id,
            title="Web Search Gateway",
            input={"query": query},
            status="running",
            priority=2_147_483_647,
        )
        session.add(task)
        await session.flush()
        run = Run(
            tenant_id=tenant.id,
            task_id=task.id,
            agent_id=agent.id,
            agent_version_id=version.id,
            attempt=1,
        )
        session.add(run)
        await session.flush()
        tenant_id = tenant.id
        run_id = run.id

    claim = await database.claim_next_run(worker_id, 5)
    assert claim is not None and claim.run_id == run_id
    await RuntimeExecutionService(database).prepare_claim(
        claim,
        worker_id=worker_id,
        registry=RuntimeProviderRegistry([MockRuntimeProvider()]),
    )
    return tenant_id, run_id, claim


def _checkpoint() -> dict:
    value = {
        "schema_version": 2,
        "execution_mode": "react",
        "loop_state": "waiting_for_tool",
        "pending_actions": [{"kind": "tool", "name": "web.search"}],
    }
    return {**value, "checkpoint_hash": canonical_hash(value)}


@pytest.mark.asyncio
async def test_web_search_approval_resume_is_idempotent_and_audit_safe() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    worker_id = f"web-search-{uuid4()}"
    query = f"sensitive current query {uuid4()}"
    provider = FakeProvider("brave")
    executor = WebSearchExecutor(
        WebProviderRegistry([provider, UnusedProvider()]),
        environment="test",
    )
    gateway = ToolGateway(
        database,
        ToolRegistry([executor]),
        secret_resolver=EnvironmentSecretResolver(
            {"NICO_TOOL_SECRET_WEB_SEARCH_BRAVE_TEST": "brave-test-secret"}
        ),
    )
    try:
        tenant_id, run_id, claim = await _seed_run(
            database,
            worker_id=worker_id,
            query=query,
        )
        authorized = await gateway.list_authorized(claim, worker_id=worker_id)
        assert [spec.reference for spec in authorized] == [executor.spec.reference]
        request = ToolGatewayRequest(
            tool_name="web.search",
            tool_version="1.0.0",
            arguments={"query": query, "count": 3},
            idempotency_key="web-search-call",
            caller="runtime:nico_native",
            checkpoint=_checkpoint(),
        )

        with pytest.raises(ToolApprovalRequired) as pending:
            await gateway.execute(claim, worker_id=worker_id, request=request)

        async with database.admin_transaction() as session:
            approval = await session.get(ToolApprovalRequest, pending.value.approval_id)
            requested_event = await session.scalar(
                select(Event).where(
                    Event.event_type == "ApprovalRequested",
                    Event.aggregate_id == pending.value.approval_id,
                )
            )
            requested_audit = await session.scalar(
                select(AuditRecord).where(
                    AuditRecord.action == "tool.approval.request",
                    AuditRecord.resource_id == pending.value.approval_id,
                )
            )
        assert approval is not None and approval.arguments_redacted == {
            "query": query,
            "count": 3,
        }
        assert requested_event is not None and query not in str(requested_event.payload)
        assert requested_audit is not None and query not in str(requested_audit.details)

        # RuntimeExecutionService.suspend_claim normally clears this lease after
        # the provider returns its approval wake condition.  This gateway-level
        # test has no provider loop, so reproduce that durable suspension edge
        # before the operator decision wakes the Run.
        async with database.admin_transaction() as session:
            run = await session.get(Run, run_id)
            assert run is not None
            run.lease_owner = None
            run.lease_token = None
            run.lease_expires_at = None
            run.heartbeat_at = None

        await ToolApprovalService(database).decide(
            TenantContext(tenant_id, "operator:test", uuid4()),
            pending.value.approval_id,
            ToolApprovalDecision(
                expected_revision=approval.revision,
                decision="approve",
                allowed_scope="once",
            ),
            idempotency_key="approve-web-search",
        )
        resumed_claim = await database.claim_next_run(worker_id, 5)
        assert resumed_claim is not None and resumed_claim.run_id == run_id
        result = await gateway.execute(resumed_claim, worker_id=worker_id, request=request)
        replay = await gateway.execute(resumed_claim, worker_id=worker_id, request=request)

        assert result.status is ToolCallStatus.SUCCEEDED
        assert result.output is not None
        assert result.output["query"] == query
        assert result.output["external_content"]["untrusted"] is True
        assert replay.cached is True
        assert replay.tool_call_id == result.tool_call_id
        assert provider.calls == 1

        async with database.admin_transaction() as session:
            call_count = await session.scalar(
                select(func.count()).select_from(ToolCall).where(ToolCall.run_id == run_id)
            )
            events = list(await session.scalars(select(Event).where(Event.run_id == run_id)))
            audits = list(
                await session.scalars(select(AuditRecord).where(AuditRecord.tenant_id == tenant_id))
            )
        assert call_count == 1
        assert all(query not in str(event.payload) for event in events)
        assert all(query not in str(audit.details) for audit in audits)
    finally:
        await engine.dispose()
