from __future__ import annotations

import hashlib
import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import Settings
from nico_agent.conversations.contracts import (
    ConversationCompact,
    ConversationCreate,
    ConversationTurnCreate,
)
from nico_agent.conversations.service import ConversationService
from nico_agent.database import Database, TenantContext
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    ContextSnapshot,
    Conversation,
    ModelCall,
    ModelEndpoint,
    Project,
    Run,
    RuntimeSession,
    Task,
    Tenant,
)
from nico_agent.models.contracts import (
    ModelCapability,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelUsage,
)
from nico_agent.models.gateway import ModelGateway
from nico_agent.models.registry import ModelProviderRegistry
from nico_agent.runtime import NicoNativeRuntimeProvider, RuntimeProviderRegistry
from nico_agent.runtime.executor import RuntimeWorker

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


class FakeModelProvider:
    name = "openai_compatible"

    def describe_capabilities(self):
        return frozenset({ModelCapability.STREAMING})

    async def stream(self, request):
        yield ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED)
        yield ModelStreamEvent(
            type=ModelStreamEventType.TEXT_DELTA,
            text_delta=_final_action("native"),
        )
        yield ModelStreamEvent(
            type=ModelStreamEventType.RESPONSE_COMPLETED,
            finish_reason="stop",
            usage=ModelUsage(
                input_tokens=4,
                output_tokens=1,
                total_tokens=5,
                status="exact",
            ),
            provider_request_id="fake-request-1",
        )


def _final_action(content: str) -> str:
    return json.dumps(
        {
            "type": "final",
            "content": content,
            "intent": {
                "interpreted_intent": "Answer the current task",
                "confidence": 0.95,
                "candidates": [
                    {
                        "candidate_id": "answer",
                        "intent": "Answer the current task",
                        "confidence": 0.95,
                    }
                ],
                "ambiguity": 0.05,
                "risk": "low",
                "risk_reasons": [],
                "missing_information": [],
                "safe_partial_answer_possible": True,
            },
            "completion": {
                "answered_user_intent": True,
                "requires_user_response": False,
            },
        },
        separators=(",", ":"),
    )


@pytest.mark.asyncio
async def test_native_worker_persists_context_model_call_and_terminal_guards() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        suffix = uuid4().hex[:10]
        async with database.admin_transaction() as session:
            tenant = Tenant(name=f"Native {suffix}", slug=f"native-{suffix}")
            other = Tenant(name=f"Other {suffix}", slug=f"other-{suffix}")
            session.add_all([tenant, other])
            await session.flush()
            project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
            agent = Agent(
                tenant_id=tenant.id,
                name=f"agent-{suffix}",
                display_name="Native Agent",
            )
            endpoint = ModelEndpoint(
                tenant_id=tenant.id,
                stable_key="fake",
                revision=1,
                display_name="Fake model",
                base_url="https://models.example/v1",
                credential_ref="env:NICO_MODEL_SECRET_TEST",
                allowed_models=["fake-model"],
                capabilities={"streaming": True},
            )
            session.add_all([project, agent, endpoint])
            await session.flush()
            version = AgentVersion(
                tenant_id=tenant.id,
                agent_id=agent.id,
                version=1,
                status="published",
                role="assistant",
                mandate="Answer",
                runtime_provider="nico_native",
                execution_mode="direct",
                model_endpoint_id=endpoint.id,
                model_name="fake-model",
                content_hash="a" * 64,
            )
            session.add(version)
            await session.flush()
            agent.current_version_id = version.id
            agent.status = "ready"
            task = Task(
                tenant_id=tenant.id,
                project_id=project.id,
                assignee_agent_id=agent.id,
                title="Native persistence",
                input={"prompt": "hello"},
                status="running",
                # The integration suite intentionally leaves recoverable Runs
                # in the shared queue.  Use the PostgreSQL integer maximum so
                # this test always exercises the Run it just created.
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
            ids = {
                "tenant": tenant.id,
                "other": other.id,
                "run": run.id,
            }

        gateway = ModelGateway(ModelProviderRegistry([FakeModelProvider()]))
        worker = RuntimeWorker(
            database,
            RuntimeProviderRegistry([NicoNativeRuntimeProvider(gateway)]),
            worker_id=f"native-{suffix}",
            lease_seconds=10,
            heartbeat_seconds=1,
        )
        assert await worker.execute_once() is True

        async with database.admin_transaction() as session:
            run_row = await session.scalar(select(Run).where(Run.id == ids["run"]))
            call = await session.scalar(select(ModelCall).where(ModelCall.run_id == ids["run"]))
            context = await session.scalar(
                select(ContextSnapshot).where(ContextSnapshot.run_id == ids["run"])
            )
            runtime = await session.scalar(
                select(RuntimeSession).where(RuntimeSession.run_id == ids["run"])
            )
            assert run_row is not None and run_row.status == "completed"
            assert run_row.result == {"content": "native"}
            assert call is not None and call.status == "completed"
            assert call.usage_status == "exact"
            assert call.provider_request_id == "fake-request-1"
            assert context is not None and len(context.content_hash) == 64
            assert runtime is not None and runtime.loop_state == "completed"
            assert runtime.provider_resolution_source == "agent_version"
            assert runtime.legacy_resolver_used is False
            assert runtime.provider_compatibility["implementation"] == "native"
            call_id = call.id

        other_context = TenantContext(ids["other"], "integration-test", uuid4())
        async with database.tenant_transaction(other_context) as session:
            assert await session.scalar(select(ModelCall).where(ModelCall.id == call_id)) is None

        with pytest.raises(DBAPIError):
            async with database.admin_transaction() as session:
                await session.execute(
                    text("UPDATE model_calls SET status = 'failed' WHERE id = :id"),
                    {"id": call_id},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_conversation_compaction_freezes_summary_and_selects_bounded_context() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        suffix = uuid4().hex[:10]
        async with database.admin_transaction() as session:
            tenant = Tenant(name=f"Context {suffix}", slug=f"context-{suffix}")
            session.add(tenant)
            await session.flush()
            project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
            agent = Agent(
                tenant_id=tenant.id,
                name=f"agent-{suffix}",
                display_name="Context Agent",
            )
            endpoint = ModelEndpoint(
                tenant_id=tenant.id,
                stable_key="context-fake",
                revision=1,
                display_name="Context fake model",
                base_url="https://models.example/v1",
                credential_ref="env:NICO_MODEL_SECRET_TEST",
                allowed_models=["fake-model"],
                capabilities={"streaming": True},
            )
            session.add_all([project, agent, endpoint])
            await session.flush()
            version = AgentVersion(
                tenant_id=tenant.id,
                agent_id=agent.id,
                version=1,
                status="published",
                role="assistant",
                mandate="Maintain conversation continuity",
                runtime_provider="nico_native",
                execution_mode="direct",
                model_endpoint_id=endpoint.id,
                model_name="fake-model",
                content_hash="b" * 64,
            )
            session.add(version)
            await session.flush()
            agent.current_version_id = version.id
            agent.status = "ready"
            ids = {"tenant": tenant.id, "project": project.id, "agent": agent.id}
        tenant_context = TenantContext(ids["tenant"], "context-test", uuid4())
        conversations = ConversationService(database)
        conversation = await conversations.create(
            tenant_context,
            ConversationCreate(
                project_id=ids["project"],
                agent_id=ids["agent"],
                title="Bounded context",
                idempotency_key="bounded-context",
            ),
        )
        worker = RuntimeWorker(
            database,
            RuntimeProviderRegistry(
                [
                    NicoNativeRuntimeProvider(
                        ModelGateway(ModelProviderRegistry([FakeModelProvider()]))
                    )
                ]
            ),
            worker_id=f"context-{suffix}",
            lease_seconds=10,
            heartbeat_seconds=1,
        )
        first = await conversations.create_turn(
            tenant_context,
            conversation.id,
            ConversationTurnCreate(user_input="remember alpha", idempotency_key="first"),
        )
        async with database.admin_transaction() as session:
            task = await session.scalar(select(Task).where(Task.id == first.task_id))
            assert task is not None
            task.priority = 2_147_483_647
        assert await worker.execute_once() is True

        compact = await conversations.compact(
            tenant_context,
            conversation.id,
            ConversationCompact(idempotency_key="compact"),
        )
        async with database.admin_transaction() as session:
            task = await session.scalar(select(Task).where(Task.id == compact.task_id))
            assert task is not None
            task.priority = 2_147_483_647
        assert await worker.execute_once() is True

        second = await conversations.create_turn(
            tenant_context,
            conversation.id,
            ConversationTurnCreate(user_input="continue beta", idempotency_key="second"),
        )
        async with database.admin_transaction() as session:
            task = await session.scalar(select(Task).where(Task.id == second.task_id))
            assert task is not None
            task.priority = 2_147_483_647
        assert await worker.execute_once() is True

        async with database.admin_transaction() as session:
            compacted = await session.scalar(
                select(Conversation).where(Conversation.id == conversation.id)
            )
            compact_call = await session.scalar(
                select(ModelCall).where(ModelCall.run_id == compact.run_id)
            )
            snapshot = await session.scalar(
                select(ContextSnapshot).where(ContextSnapshot.run_id == second.run_id)
            )
            assert compacted is not None
            assert compacted.summary == "native"
            assert compacted.summary_through_sequence == 1
            assert compacted.summary_model_call_id == compact_call.id
            assert snapshot is not None
            assert snapshot.selected_turn_ids == [str(second.id)]
            assert snapshot.conversation_summary_hash == hashlib.sha256(b"native").hexdigest()
            assert snapshot.token_budget is None
            assert "conversation_summary" in str(snapshot.rendered_messages)
            assert snapshot.schema_version == 2
            assert [item["role"] for item in snapshot.rendered_messages] == [
                "system",
                "user",
                "user",
            ]
            assert snapshot.rendered_messages[-1]["content"] == "continue beta"
            assert not any(
                item["role"] == "assistant" and item["content"] == "native"
                for item in snapshot.rendered_messages
            )
    finally:
        await engine.dispose()
