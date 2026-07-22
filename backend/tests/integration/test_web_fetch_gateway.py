from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import Settings
from nico_agent.database import Database
from nico_agent.domain.models import Agent, AgentVersion, Project, Run, Task, Tenant, ToolCall
from nico_agent.net.safe_http import PinnedRequest, RawHttpResponse, SafeHttpClient
from nico_agent.runtime import MockRuntimeProvider
from nico_agent.runtime.registry import RuntimeProviderRegistry
from nico_agent.runtime.service import RuntimeExecutionService
from nico_agent.tools import ToolExecutionContext, ToolGateway, ToolGatewayRequest, ToolRegistry
from nico_agent.tools.builtin import WebFetchExecutor, WebSearchExecutor
from nico_agent.tools.contracts import canonical_hash
from nico_agent.web.cache import RedisWebFetchCache
from nico_agent.web.contracts import SearchPage, SearchProviderError, SearchResult
from nico_agent.web.registry import WebProviderRegistry
from nico_agent.web.source_authorization import WebSourceAuthorizer, WebSourceDenied

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL and Redis running",
)


@dataclass
class FakeSearchProvider:
    key: str = "searxng"
    fail: bool = False

    async def search(self, request, *, secret=None, config=None):
        if self.fail:
            raise SearchProviderError(
                "WEB_PROVIDER_PROTOCOL_ERROR",
                "Synthetic Provider failure",
            )
        return SearchPage(
            provider="searxng",
            results=(
                SearchResult(
                    title="Nico guide",
                    url="https://docs.example/guide",
                    snippet="Current documentation",
                ),
                SearchResult(
                    title="Nico article CDN",
                    url="https://cdn.example/article",
                    snippet="Canonical article",
                ),
            ),
        )


class FakeTransport:
    def __init__(self, responses=()) -> None:
        self.responses = list(responses)
        self.requests: list[PinnedRequest] = []

    async def request(self, request: PinnedRequest) -> RawHttpResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("Web transport was called unexpectedly")
        return self.responses.pop(0)


class FailIfTouchedCache:
    async def get(self, *args, **kwargs):
        raise AssertionError("Web fetch cache was read before source authorization")

    async def set(self, *args, **kwargs):
        raise AssertionError("Web fetch cache was written for a denied source")


async def _seed_run(database: Database, *, worker_id: str):
    suffix = uuid4().hex[:10]
    search_config = {
        "provider": "searxng",
        "safe_search": "moderate",
        "cache_ttl_seconds": 60,
        "rate_limit_per_minute": 20,
    }
    fetch_config = {
        "allowed_domains": ["docs.example"],
        "cache_ttl_seconds": 60,
        "max_download_bytes": 100_000,
        "max_chars": 5_000,
        "max_redirects": 3,
    }
    tenant_policy = {
        "allow": ["web.search@1.0.0", "web.fetch@1.1.0"],
        "permissions": ["network.web.search", "network.web.fetch"],
        "tools": {
            "web.search@1.0.0": search_config,
            "web.fetch@1.1.0": fetch_config,
        },
    }
    agent_policy = {
        **tenant_policy,
        "tools": {
            "web.search@1.0.0": search_config,
            "web.fetch@1.1.0": fetch_config,
        },
    }
    async with database.admin_transaction() as session:
        tenant = Tenant(
            name=f"Web Fetch {suffix}",
            slug=f"web-fetch-{suffix}",
            settings={"tool_policy": tenant_policy},
        )
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Web Fetch Agent",
        )
        session.add_all([project, agent])
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="web-fetch-test",
            mandate="Search and fetch current public information",
            tool_policy=agent_policy,
            run_config={"runtime_provider": "mock"},
            content_hash="f" * 64,
        )
        session.add(version)
        await session.flush()
        agent.current_version_id = version.id
        agent.status = "ready"
        task = Task(
            tenant_id=tenant.id,
            project_id=project.id,
            assignee_agent_id=agent.id,
            title="Web Fetch Gateway",
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

    claim = await database.claim_next_run(worker_id, 10)
    assert claim is not None and claim.run_id == run_id
    await RuntimeExecutionService(database).prepare_claim(
        claim,
        worker_id=worker_id,
        registry=RuntimeProviderRegistry([MockRuntimeProvider()]),
    )
    return tenant_id, run_id, claim


def _checkpoint(tool_name: str) -> dict:
    value = {
        "schema_version": 2,
        "execution_mode": "react",
        "loop_state": "waiting_for_tool",
        "pending_actions": [{"kind": "tool", "name": tool_name}],
    }
    return {**value, "checkpoint_hash": canonical_hash(value)}


def _request(tool: str, arguments: dict, key: str) -> ToolGatewayRequest:
    return ToolGatewayRequest(
        tool_name=tool,
        tool_version="1.1.0" if tool == "web.fetch" else "1.0.0",
        arguments=arguments,
        idempotency_key=key,
        caller="runtime:nico_native",
        checkpoint=_checkpoint(tool),
    )


@pytest.mark.asyncio
async def test_fetch_uses_durable_search_provenance_cache_and_redirect_authorization() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    prefix = f"nico:test:web-fetch:{uuid4()}"
    cache = RedisWebFetchCache(redis, prefix=prefix)
    provider = FakeSearchProvider()
    transport = FakeTransport(
        [
            RawHttpResponse(
                302,
                (("Location", "https://cdn.example/article"),),
                b"",
            ),
            RawHttpResponse(
                200,
                (("Content-Type", "text/html; charset=utf-8"),),
                (
                    b"<html><head><title>Nico Article</title></head><body><main>"
                    b"<p>Durable current Web evidence.</p></main></body></html>"
                ),
            ),
        ]
    )
    worker_id = f"web-fetch-{uuid4()}"
    try:
        tenant_id, run_id, claim = await _seed_run(database, worker_id=worker_id)
        authorizer = WebSourceAuthorizer(database)
        fetch = WebFetchExecutor(
            authorizer,
            http=SafeHttpClient(
                transport=transport,
                resolver=lambda host, port: ["93.184.216.34"],
            ),
            cache=cache,
        )
        search = WebSearchExecutor(
            WebProviderRegistry([provider]),
            environment="test",
        )
        gateway = ToolGateway(
            database,
            ToolRegistry([search, fetch]),
            approval_required_risks=frozenset(),
        )
        search_result = await gateway.execute(
            claim,
            worker_id=worker_id,
            request=_request(
                "web.search",
                {"query": "current Nico evidence", "count": 2},
                "search-source",
            ),
        )
        assert search_result.status.value == "succeeded"

        fetched = await gateway.execute(
            claim,
            worker_id=worker_id,
            request=_request(
                "web.fetch",
                {
                    "url": "https://docs.example/guide",
                    "search_tool_call_id": str(search_result.tool_call_id),
                    "extract_mode": "text",
                },
                "fetch-source",
            ),
        )
        assert fetched.status.value == "succeeded"
        assert fetched.output is not None
        assert fetched.output["final_url"] == "https://cdn.example/article"
        assert fetched.output["redirects"] == 1
        assert fetched.output["content"] == "Durable current Web evidence."
        assert fetched.output["cached"] is False
        assert len(transport.requests) == 2

        # A fresh executor/Gateway instance represents a Worker process restart.
        restarted_transport = FakeTransport()
        restarted = ToolGateway(
            database,
            ToolRegistry(
                [
                    WebFetchExecutor(
                        WebSourceAuthorizer(database),
                        http=SafeHttpClient(
                            transport=restarted_transport,
                            resolver=lambda host, port: ["93.184.216.34"],
                        ),
                        cache=RedisWebFetchCache(redis, prefix=prefix),
                    )
                ]
            ),
            approval_required_risks=frozenset(),
        )
        cached = await restarted.execute(
            claim,
            worker_id=worker_id,
            request=_request(
                "web.fetch",
                {
                    "url": "https://docs.example/guide",
                    "search_tool_call_id": str(search_result.tool_call_id),
                    "extract_mode": "text",
                },
                "fetch-source-after-restart",
            ),
        )
        assert cached.status.value == "succeeded"
        assert cached.output is not None and cached.output["cached"] is True
        assert restarted_transport.requests == []

        allowlist_transport = FakeTransport(
            [
                RawHttpResponse(
                    200,
                    (("Content-Type", "text/plain; charset=utf-8"),),
                    b"Allowlisted operator documentation",
                )
            ]
        )
        allowlist_gateway = ToolGateway(
            database,
            ToolRegistry(
                [
                    WebFetchExecutor(
                        WebSourceAuthorizer(database),
                        http=SafeHttpClient(
                            transport=allowlist_transport,
                            resolver=lambda host, port: ["93.184.216.34"],
                        ),
                        cache=RedisWebFetchCache(redis, prefix=prefix),
                    )
                ]
            ),
            approval_required_risks=frozenset(),
        )
        direct = await allowlist_gateway.execute(
            claim,
            worker_id=worker_id,
            request=_request(
                "web.fetch",
                {"url": "https://docs.example/operator", "extract_mode": "text"},
                "fetch-direct-allowlist",
            ),
        )
        assert direct.status.value == "succeeded"
        assert direct.output is not None
        assert direct.output["content"] == "Allowlisted operator documentation"
        assert len(allowlist_transport.requests) == 1

        context = ToolExecutionContext(
            tenant_id=tenant_id,
            run_id=run_id,
            run_step_id=uuid4(),
            actor_id="runtime:test",
            correlation_id=uuid4(),
        )
        for call_id, url, context_update in (
            (uuid4(), "https://docs.example/guide", {}),
            (search_result.tool_call_id, "https://docs.example/other", {}),
            (fetched.tool_call_id, "https://docs.example/guide", {}),
            (search_result.tool_call_id, "https://docs.example/guide", {"run_id": uuid4()}),
            (
                search_result.tool_call_id,
                "https://docs.example/guide",
                {"tenant_id": uuid4()},
            ),
        ):
            with pytest.raises(WebSourceDenied):
                await authorizer.authorize(
                    context.model_copy(update=context_update),
                    url,
                    search_tool_call_id=call_id,
                    allowed_domains=("docs.example",),
                )

        provider.fail = True
        failed_search = await gateway.execute(
            claim,
            worker_id=worker_id,
            request=_request(
                "web.search",
                {"query": "failed source"},
                "failed-search-source",
            ),
        )
        assert failed_search.status.value == "failed"
        with pytest.raises(WebSourceDenied):
            await authorizer.authorize(
                context,
                "https://docs.example/guide",
                search_tool_call_id=failed_search.tool_call_id,
                allowed_domains=("docs.example",),
            )

        denied_gateway = ToolGateway(
            database,
            ToolRegistry(
                [
                    WebFetchExecutor(
                        WebSourceAuthorizer(database),
                        http=SafeHttpClient(
                            transport=FakeTransport(),
                            resolver=lambda host, port: ["93.184.216.34"],
                        ),
                        cache=FailIfTouchedCache(),
                    )
                ]
            ),
            approval_required_risks=frozenset(),
        )
        denied = await denied_gateway.execute(
            claim,
            worker_id=worker_id,
            request=_request(
                "web.fetch",
                {
                    "url": "https://docs.example/not-a-result",
                    "search_tool_call_id": str(search_result.tool_call_id),
                },
                "fetch-denied-before-cache",
            ),
        )
        assert denied.status.value == "failed"
        assert denied.error == {
            "code": "WEB_FETCH_SOURCE_DENIED",
            "message": "Web fetch source is not authorized",
        }

        async with database.admin_transaction() as session:
            calls = list(
                await session.scalars(
                    select(ToolCall).where(
                        ToolCall.run_id == run_id,
                        ToolCall.tool_name == "web.fetch",
                    )
                )
            )
        assert sum(call.status == "succeeded" for call in calls) == 3
        assert sum(call.status == "failed" for call in calls) == 1
    finally:
        keys = [key async for key in redis.scan_iter(match=f"{prefix}*")]
        if keys:
            await redis.delete(*keys)
        await redis.aclose()
        await engine.dispose()
