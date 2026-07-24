from __future__ import annotations

import json
import os
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.api import create_app
from nico_agent.artifacts.minio import MinioArtifactStore
from nico_agent.artifacts.service import ArtifactService
from nico_agent.config import Settings
from nico_agent.database import Database, TenantContext
from nico_agent.domain.models import (
    Agent,
    AgentMessage,
    AgentVersion,
    Artifact,
    Delegation,
    ModelEndpoint,
    Project,
    Run,
    RunBudgetLedger,
    SharedArtifactLink,
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
from nico_agent.runtime import (
    MockRuntimeProvider,
    NicoNativeRuntimeProvider,
    RuntimeProviderRegistry,
)
from nico_agent.runtime.executor import RuntimeWorker

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


def _final_action(content: str) -> str:
    return json.dumps(
        {
            "type": "final",
            "content": content,
            "intent": {
                "interpreted_intent": "Report the completed delegated work",
                "confidence": 0.95,
                "candidates": [
                    {
                        "candidate_id": "report",
                        "intent": "Report the completed delegated work",
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


class MultiAgentModelProvider:
    name = "openai_compatible"

    def __init__(self, parent_run_id: UUID, targets: tuple[UUID, UUID]) -> None:
        self.parent_run_id = str(parent_run_id)
        self.targets = tuple(str(value) for value in targets)
        self.requests = []

    def describe_capabilities(self):
        return frozenset({ModelCapability.STREAMING, ModelCapability.TOOLS})

    async def stream(self, request):
        self.requests.append(request)
        run_id = request.metadata["run_id"]
        call_key = request.metadata["call_key"]
        yield ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED)
        if run_id == self.parent_run_id and call_key == "model:1":
            assert {tool.name for tool in request.tools} == {
                "delegate_agent",
                "store_artifact",
            }
            for index, target in enumerate(self.targets):
                yield ModelStreamEvent(
                    type=ModelStreamEventType.TOOL_CALL_DELTA,
                    tool_index=index,
                    tool_call_id=f"branch-{index}",
                    tool_name="delegate_agent",
                    tool_arguments_delta=json.dumps(
                        {
                            "target_agent_version_id": target,
                            "objective": f"Research independent branch {index}",
                            "acceptance": {"required": ["summary"]},
                            "context_refs": ["task:input"],
                            "budget": {
                                "token_limit": 200,
                                "cost_limit_microunits": 0,
                                "tool_call_limit": 0,
                            },
                        },
                        separators=(",", ":"),
                    ),
                )
            finish_reason = "tool_calls"
            text = None
        elif run_id == self.parent_run_id:
            tool_messages = [message for message in request.messages if message.role == "tool"]
            assert len(tool_messages) == 2
            assert all("child-result" in (message.content or "") for message in tool_messages)
            assert all("artifact:" in (message.content or "") for message in tool_messages)
            finish_reason = "stop"
            text = "Aggregated two child results."
        elif call_key == "model:1":
            assert "store_artifact" in {tool.name for tool in request.tools}
            yield ModelStreamEvent(
                type=ModelStreamEventType.TOOL_CALL_DELTA,
                tool_index=0,
                tool_call_id=f"artifact-{run_id[-8:]}",
                tool_name="store_artifact",
                tool_arguments_delta=json.dumps(
                    {
                        "name": f"finding-{run_id[-8:]}.txt",
                        "content_type": "text/plain",
                        "content_text": f"artifact-result:{run_id[-8:]}",
                        "share_with_parent": True,
                    },
                    separators=(",", ":"),
                ),
            )
            finish_reason = "tool_calls"
            text = None
        else:
            finish_reason = "stop"
            text = f"child-result:{run_id[-8:]}"
        if text:
            yield ModelStreamEvent(
                type=ModelStreamEventType.TEXT_DELTA,
                text_delta=_final_action(text),
            )
        yield ModelStreamEvent(
            type=ModelStreamEventType.RESPONSE_COMPLETED,
            finish_reason=finish_reason,
            usage=ModelUsage(input_tokens=10, output_tokens=10, total_tokens=20, status="exact"),
            provider_request_id=f"provider:{run_id}:{call_key}",
        )


async def _seed(database: Database):
    suffix = uuid4().hex[:10]
    async with database.admin_transaction() as session:
        tenant = Tenant(name=f"Multi {suffix}", slug=f"multi-{suffix}")
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        endpoint = ModelEndpoint(
            tenant_id=tenant.id,
            stable_key=f"multi-{suffix}",
            revision=1,
            display_name="Multi-agent fake",
            base_url="https://models.example/v1",
            credential_ref="env:NICO_MODEL_SECRET_TEST",
            allowed_models=["multi-model"],
            capabilities={"streaming": True, "tools": True},
        )
        agents = [
            Agent(
                tenant_id=tenant.id,
                name=f"{role}-{suffix}",
                display_name=f"{role.title()} Agent",
            )
            for role in ("parent", "child-a", "child-b")
        ]
        session.add_all([project, endpoint, *agents])
        await session.flush()
        versions = [
            AgentVersion(
                tenant_id=tenant.id,
                agent_id=agent.id,
                version=1,
                status="published",
                role=role,
                mandate=f"Complete {role} work",
                runtime_provider="nico_native",
                execution_mode="react",
                model_endpoint_id=endpoint.id,
                model_name="multi-model",
                budgets={"max_iterations": 4, "max_tool_calls": 0},
                content_hash=character * 64,
            )
            for agent, role, character in zip(
                agents,
                ("parent", "child-a", "child-b"),
                ("p", "a", "b"),
                strict=True,
            )
        ]
        session.add_all(versions)
        await session.flush()
        allowed = [str(versions[1].id), str(versions[2].id)]
        policy = {
            "enabled": True,
            "max_depth": 2,
            "max_children": 2,
            "max_parallelism": 2,
            "allowed_agent_version_ids": allowed,
            "allowed_secret_refs": [],
        }
        tenant.settings = {"coordination_policy": policy, "tool_policy": {}}
        versions[0].coordination_policy = policy
        for agent, version in zip(agents, versions, strict=True):
            agent.current_version_id = version.id
            agent.status = "ready"
        task = Task(
            tenant_id=tenant.id,
            project_id=project.id,
            assignee_agent_id=agents[0].id,
            title="Coordinate two research branches",
            input={"topic": "multi-agent recovery"},
            acceptance={"required": ["content"]},
            status="running",
            priority=2_147_483_647,
        )
        session.add(task)
        await session.flush()
        run = Run(
            tenant_id=tenant.id,
            task_id=task.id,
            agent_id=agents[0].id,
            agent_version_id=versions[0].id,
            attempt=1,
            max_steps=8,
            token_budget=1000,
        )
        session.add(run)
        await session.flush()
        return tenant.id, run.id, (versions[1].id, versions[2].id)


@pytest.mark.asyncio
async def test_parent_delegates_two_children_releases_lease_and_recovers_to_aggregate() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        tenant_id, parent_run_id, targets = await _seed(database)
        model = MultiAgentModelProvider(parent_run_id, targets)
        artifact_service = ArtifactService(
            database,
            MinioArtifactStore(
                settings.minio_url,
                access_key=settings.minio_access_key,
                secret_key=settings.minio_secret_key,
                bucket=settings.minio_bucket,
            ),
            max_bytes=settings.artifact_max_bytes,
        )
        worker = RuntimeWorker(
            database,
            RuntimeProviderRegistry(
                [
                    MockRuntimeProvider(),
                    NicoNativeRuntimeProvider(ModelGateway(ModelProviderRegistry([model]))),
                ]
            ),
            worker_id=f"multi-{uuid4().hex[:8]}",
            lease_seconds=10,
            heartbeat_seconds=1,
            artifact_service=artifact_service,
        )

        for _ in range(20):
            assert await worker.execute_once() is True
            async with database.admin_transaction() as session:
                suspended = await session.get(Run, parent_run_id)
            if suspended is not None and suspended.status == "waiting_for_subagent":
                break
        else:
            pytest.fail("parent Run did not reach durable subagent suspension")
        async with database.admin_transaction() as session:
            delegations = list(
                await session.scalars(
                    select(Delegation)
                    .where(Delegation.tenant_id == tenant_id)
                    .order_by(Delegation.created_at)
                )
            )
        assert suspended is not None and suspended.status == "waiting_for_subagent"
        assert suspended.lease_owner is None and suspended.lease_token is None
        assert len(delegations) == 2

        for _ in range(20):
            assert await worker.execute_once() is True
            async with database.admin_transaction() as session:
                current = await session.get(Run, parent_run_id)
            if current is not None and current.status == "completed":
                break
        else:
            pytest.fail("parent Run did not recover after both Child Runs became terminal")

        async with database.admin_transaction() as session:
            parent = await session.get(Run, parent_run_id)
            delegations = list(
                await session.scalars(
                    select(Delegation)
                    .where(Delegation.tenant_id == tenant_id)
                    .order_by(Delegation.created_at)
                )
            )
            messages = list(
                await session.scalars(
                    select(AgentMessage).where(
                        AgentMessage.tenant_id == tenant_id,
                        AgentMessage.receiver_run_id == parent_run_id,
                        AgentMessage.message_type == "result",
                    )
                )
            )
            ledger = await session.scalar(
                select(RunBudgetLedger).where(
                    RunBudgetLedger.tenant_id == tenant_id,
                    RunBudgetLedger.run_id == parent_run_id,
                )
            )
            stored_artifacts = list(
                await session.scalars(select(Artifact).where(Artifact.tenant_id == tenant_id))
            )
            shared_links = list(
                await session.scalars(
                    select(SharedArtifactLink).where(SharedArtifactLink.tenant_id == tenant_id)
                )
            )
        assert parent is not None and parent.status == "completed"
        assert parent.result == {"content": "Aggregated two child results."}
        assert {delegation.status for delegation in delegations} == {"completed"}
        assert len(messages) == 2
        assert {message.status for message in messages} == {"acknowledged"}
        assert all(len(message.refs) == 1 for message in messages)
        assert len(stored_artifacts) == 2
        assert {artifact.status for artifact in stored_artifacts} == {"available"}
        assert len(shared_links) == 2
        visible = await artifact_service.list_for_run(
            TenantContext(tenant_id, "test:parent", uuid4()), parent_run_id
        )
        assert {artifact.id for artifact in visible} == {
            artifact.id for artifact in stored_artifacts
        }
        app = create_app(settings=settings, health_service=object(), database=database)
        headers = {"X-Tenant-ID": str(tenant_id), "X-Actor-ID": "artifact-api-test"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            listed = await client.get(f"/api/v1/runs/{parent_run_id}/artifacts", headers=headers)
            assert listed.status_code == 200
            assert len(listed.json()) == 2
            assert all("object_key" not in item for item in listed.json())
            downloaded = await client.get(
                f"/api/v1/runs/{parent_run_id}/artifacts/{stored_artifacts[0].id}/content",
                headers=headers,
            )
            assert downloaded.status_code == 200
            assert downloaded.content.startswith(b"artifact-result:")

            first_child = delegations[0].child_run_id
            foreign_artifact = next(
                artifact for artifact in stored_artifacts if artifact.owner_run_id != first_child
            )
            forbidden = await client.get(
                f"/api/v1/runs/{first_child}/artifacts/{foreign_artifact.id}/content",
                headers=headers,
            )
            assert forbidden.status_code == 403

            uploaded = await client.post(
                f"/api/v1/runs/{parent_run_id}/artifacts",
                params={
                    "name": "api-note.txt",
                    "content_type": "text/plain",
                    "idempotency_key": "api-note-1",
                    "share_with_parent": "false",
                },
                content=b"parent note",
                headers=headers,
            )
            assert uploaded.status_code == 201
            assert uploaded.json()["status"] == "available"
            assert "object_key" not in uploaded.json()

        limited_app = create_app(
            settings=settings.model_copy(update={"artifact_max_bytes": 4}),
            health_service=object(),
            database=database,
        )
        async with AsyncClient(
            transport=ASGITransport(app=limited_app), base_url="http://test"
        ) as client:
            too_large = await client.post(
                f"/api/v1/runs/{parent_run_id}/artifacts",
                params={"name": "large.bin", "idempotency_key": "large-1"},
                content=b"12345",
                headers=headers,
            )
            assert too_large.status_code == 413
            assert too_large.json()["code"] == "ARTIFACT_TOO_LARGE"
        assert ledger is not None
        assert ledger.token_child_reserved == 0
        assert ledger.token_child_consumed == 80
    finally:
        await engine.dispose()
