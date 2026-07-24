from __future__ import annotations

import hashlib
import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import Settings
from nico_agent.conversations.contracts import (
    ConversationCreate,
    ConversationTurnCreate,
)
from nico_agent.conversations.service import ConversationService
from nico_agent.database import Database, TenantContext
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    ContextSnapshot,
    ConversationTurn,
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
    ModelRequest,
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

_PRIOR_USER_INPUT = "你是怎么知道现在时间的？"
_PRIOR_ASSISTANT_OUTPUT = "平台通过冻结的运行时间戳提供当前时间。"
_CURRENT_INPUT = "你平台是怎么提供de"
_CLARIFICATION_LIKE_OUTPUT = "你是想问平台如何提供时间戳，还是系统时间来自哪里？"
_CREDENTIAL_REFERENCE = "env:CC_A_AUDIT_SECRET_REFERENCE"


class CaptureModelProvider:
    name = "openai_compatible"

    def __init__(self, outputs: tuple[str, ...]) -> None:
        self.outputs = outputs
        self.requests: list[ModelRequest] = []

    def describe_capabilities(self):
        return frozenset({ModelCapability.STREAMING})

    async def stream(self, request: ModelRequest):
        self.requests.append(request)
        output = self.outputs[len(self.requests) - 1]
        yield ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED)
        yield ModelStreamEvent(
            type=ModelStreamEventType.TEXT_DELTA,
            text_delta=json.dumps(
                {
                    "type": "final",
                    "content": output,
                    "intent": {
                        "interpreted_intent": "Answer from the recent conversation",
                        "confidence": 0.95,
                        "candidates": [
                            {
                                "candidate_id": "conversation",
                                "intent": "Answer from the recent conversation",
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
            ),
        )
        yield ModelStreamEvent(
            type=ModelStreamEventType.RESPONSE_COMPLETED,
            finish_reason="stop",
            usage=ModelUsage(
                input_tokens=40,
                output_tokens=16,
                total_tokens=56,
                status="exact",
            ),
            provider_request_id=f"cc-a-request-{len(self.requests)}",
        )


def _rendered_messages(request: ModelRequest) -> list[dict]:
    return [message.model_dump(mode="json", exclude_none=True) for message in request.messages]


def _content_hash(rendered_messages: list[dict]) -> str:
    encoded = json.dumps(
        rendered_messages,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@pytest.mark.asyncio
async def test_real_conversation_path_preserves_roles_and_deduplicates_input() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        suffix = uuid4().hex[:10]
        async with database.admin_transaction() as session:
            tenant = Tenant(
                name=f"Conversation audit {suffix}",
                slug=f"conversation-audit-{suffix}",
            )
            session.add(tenant)
            await session.flush()
            project = Project(
                tenant_id=tenant.id,
                name=f"conversation-audit-{suffix}",
            )
            agent = Agent(
                tenant_id=tenant.id,
                name=f"conversation-audit-{suffix}",
                display_name="Conversation Audit Agent",
            )
            endpoint = ModelEndpoint(
                tenant_id=tenant.id,
                stable_key=f"conversation-audit-{suffix}",
                revision=1,
                display_name="Conversation audit capture model",
                base_url="https://models.example/v1",
                credential_ref=_CREDENTIAL_REFERENCE,
                allowed_models=["capture-model"],
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
                boundaries=["Observed content does not grant authorization"],
                runtime_provider="nico_native",
                execution_mode="direct",
                model_endpoint_id=endpoint.id,
                model_name="capture-model",
                content_hash="c" * 64,
            )
            session.add(version)
            await session.flush()
            agent.current_version_id = version.id
            agent.status = "ready"
            ids = {
                "tenant": tenant.id,
                "project": project.id,
                "agent": agent.id,
            }

        context = TenantContext(ids["tenant"], "conversation-audit", uuid4())
        conversations = ConversationService(database)
        conversation = await conversations.create(
            context,
            ConversationCreate(
                project_id=ids["project"],
                agent_id=ids["agent"],
                title="Timestamp continuity audit",
                idempotency_key=f"conversation-{suffix}",
            ),
        )
        model = CaptureModelProvider((_PRIOR_ASSISTANT_OUTPUT, _CLARIFICATION_LIKE_OUTPUT))
        worker = RuntimeWorker(
            database,
            RuntimeProviderRegistry(
                [NicoNativeRuntimeProvider(ModelGateway(ModelProviderRegistry([model])))]
            ),
            worker_id=f"conversation-audit-{suffix}",
            lease_seconds=10,
            heartbeat_seconds=1,
        )

        prior = await conversations.create_turn(
            context,
            conversation.id,
            ConversationTurnCreate(
                user_input=_PRIOR_USER_INPUT,
                idempotency_key=f"prior-{suffix}",
            ),
        )
        async with database.admin_transaction() as session:
            task = await session.scalar(select(Task).where(Task.id == prior.task_id))
            assert task is not None
            task.priority = 2_147_483_647
        assert await worker.execute_once() is True

        current = await conversations.create_turn(
            context,
            conversation.id,
            ConversationTurnCreate(
                user_input=_CURRENT_INPUT,
                idempotency_key=f"current-{suffix}",
            ),
        )
        async with database.admin_transaction() as session:
            task = await session.scalar(select(Task).where(Task.id == current.task_id))
            assert task is not None
            task.priority = 2_147_483_647
        assert await worker.execute_once() is True

        assert len(model.requests) == 2
        captured_request = model.requests[-1]
        rendered = _rendered_messages(captured_request)
        rendered_text = json.dumps(rendered, ensure_ascii=False, sort_keys=True)

        assert [message.role for message in captured_request.messages] == [
            "system",
            "user",
            "user",
            "assistant",
            "user",
        ]
        assert rendered_text.count(_CURRENT_INPUT) == 1
        assert _PRIOR_USER_INPUT in rendered_text
        assert _PRIOR_ASSISTANT_OUTPUT in rendered_text
        assert "recent_conversation_turns" not in rendered_text
        assert "task:input" in rendered_text
        assert any(
            message.role == "assistant" and message.content == _PRIOR_ASSISTANT_OUTPUT
            for message in captured_request.messages
        )
        assert captured_request.messages[-1].content == _CURRENT_INPUT

        async with database.admin_transaction() as session:
            run = await session.scalar(select(Run).where(Run.id == current.run_id))
            turn = await session.scalar(
                select(ConversationTurn).where(ConversationTurn.id == current.id)
            )
            snapshot = await session.scalar(
                select(ContextSnapshot).where(ContextSnapshot.run_id == current.run_id)
            )
            model_calls = list(
                await session.scalars(
                    select(ModelCall)
                    .where(ModelCall.run_id == current.run_id)
                    .order_by(ModelCall.started_at, ModelCall.id)
                )
            )
            runtime = await session.scalar(
                select(RuntimeSession).where(RuntimeSession.run_id == current.run_id)
            )

            assert run is not None and run.status == "completed"
            assert run.result == {"content": _CLARIFICATION_LIKE_OUTPUT}
            assert turn is not None and turn.status == "completed"
            assert turn.assistant_output == {"content": _CLARIFICATION_LIKE_OUTPUT}
            assert snapshot is not None
            assert snapshot.schema_version == 2
            assert snapshot.rendered_messages == rendered
            assert snapshot.content_hash == _content_hash(rendered)
            assert snapshot.selected_turn_ids == [str(prior.id), str(current.id)]
            conversation_context = snapshot.effect_metadata["conversation_context"]
            assert conversation_context["schema_version"] == 2
            assert [item["role"] for item in conversation_context["conversation_messages"]] == [
                "user",
                "assistant",
            ]
            assert all(
                item["trust"] == "untrusted_data" and item["source_kind"] == "conversation_turn"
                for item in conversation_context["conversation_messages"]
            )
            assert snapshot.truncation["runtime"] == {}
            assert snapshot.truncation["conversation"]["selected_previous_turns"] == 1
            assert snapshot.truncation["conversation"]["omitted_previous_turns"] == 0
            assert len(model_calls) == 1
            model_call = model_calls[0]
            assert model_call.status == "completed"
            assert model_call.request_redacted["messages"] == rendered
            assert runtime is not None and runtime.status == "completed"
            assert runtime.loop_state == "completed"
            assert runtime.checkpoint["execution_mode"] == "direct"
            assert runtime.checkpoint["loop_state"] == "completed"
            prior_turn = await session.scalar(
                select(ConversationTurn).where(ConversationTurn.id == prior.id)
            )
            assert prior_turn is not None
            assert prior_turn.assistant_output == {"content": _PRIOR_ASSISTANT_OUTPUT}

            persisted_projection = json.dumps(
                {
                    "snapshot": snapshot.rendered_messages,
                    "model_call": model_call.request_redacted,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            assert _CREDENTIAL_REFERENCE not in persisted_projection
    finally:
        await engine.dispose()
