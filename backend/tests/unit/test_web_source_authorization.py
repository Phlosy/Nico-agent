from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from nico_agent.tools import ToolExecutionContext
from nico_agent.web.source_authorization import (
    WebSourceAuthorizer,
    WebSourceDenied,
    configured_allowed_domains,
)


class FakeSession:
    def __init__(self, call) -> None:
        self.call = call

    async def scalar(self, statement):
        return self.call


class FakeDatabase:
    def __init__(self, call) -> None:
        self.call = call
        self.contexts = []

    @asynccontextmanager
    async def tenant_transaction(self, context):
        self.contexts.append(context)
        yield FakeSession(self.call)


def _context(*, tenant_id=None, run_id=None):
    return ToolExecutionContext(
        tenant_id=tenant_id or uuid4(),
        run_id=run_id or uuid4(),
        run_step_id=uuid4(),
        actor_id="runtime:test",
        correlation_id=uuid4(),
    )


def _call(context, **updates):
    values = {
        "id": uuid4(),
        "tenant_id": context.tenant_id,
        "run_id": context.run_id,
        "tool_name": "web.search",
        "tool_version": "1.0.0",
        "status": "succeeded",
        "result": {
            "results": [
                {
                    "title": "Nico",
                    "url": "https://Docs.Example:443/current",
                    "snippet": "Current docs",
                }
            ]
        },
    }
    values.update(updates)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_search_source_requires_same_run_successful_exact_tool_and_url() -> None:
    context = _context()
    call = _call(context)
    database = FakeDatabase(call)
    authorizer = WebSourceAuthorizer(database)

    allowed = await authorizer.authorize(
        context,
        "https://docs.example/current",
        search_tool_call_id=call.id,
        allowed_domains=(),
    )

    assert allowed.target.url == "https://docs.example/current"
    assert allowed.basis == "search"
    assert allowed.search_tool_call_id == call.id
    assert database.contexts[0].tenant_id == context.tenant_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates",
    [
        {"status": "failed"},
        {"tool_name": "web.fetch"},
        {"tool_version": "2.0.0"},
        {"run_id": uuid4()},
        {"tenant_id": uuid4()},
        {"result": None},
    ],
)
async def test_search_source_rejects_non_authoritative_call_shapes(updates) -> None:
    context = _context()
    call = _call(context, **updates)

    with pytest.raises(WebSourceDenied):
        await WebSourceAuthorizer(FakeDatabase(call)).authorize(
            context,
            "https://docs.example/current",
            search_tool_call_id=call.id,
            allowed_domains=("docs.example",),
        )


@pytest.mark.asyncio
async def test_search_source_rejects_forged_id_and_url_mismatch_without_allowlist_fallback() -> (
    None
):
    context = _context()
    call = _call(context)
    authorizer = WebSourceAuthorizer(FakeDatabase(call))

    for call_id, url in (
        ("not-a-uuid", "https://docs.example/current"),
        (call.id, "https://docs.example/other"),
    ):
        with pytest.raises(WebSourceDenied):
            await authorizer.authorize(
                context,
                url,
                search_tool_call_id=call_id,
                allowed_domains=("docs.example",),
            )


@pytest.mark.asyncio
async def test_direct_source_uses_only_normalized_frozen_domain_allowlist() -> None:
    context = _context()
    authorizer = WebSourceAuthorizer(FakeDatabase(None))
    domains = configured_allowed_domains(["docs.example", "*.public.example"])

    exact = await authorizer.authorize(
        context,
        "https://docs.example/guide",
        search_tool_call_id=None,
        allowed_domains=domains,
    )
    wildcard = await authorizer.authorize(
        context,
        "https://api.public.example/guide",
        search_tool_call_id=None,
        allowed_domains=domains,
    )

    assert exact.basis == wildcard.basis == "allowlist"
    for denied in (
        "https://public.example/guide",
        "https://evil.example/guide",
    ):
        with pytest.raises(WebSourceDenied):
            await authorizer.authorize(
                context,
                denied,
                search_tool_call_id=None,
                allowed_domains=domains,
            )


@pytest.mark.parametrize(
    "value",
    [None, "docs.example", ["https://docs.example"], [""], ["*."]],
)
def test_invalid_domain_policy_fails_closed(value) -> None:
    with pytest.raises(WebSourceDenied):
        configured_allowed_domains(value)
