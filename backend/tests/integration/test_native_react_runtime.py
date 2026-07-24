from __future__ import annotations

import asyncio
import json
import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.api import create_app
from nico_agent.config import Settings
from nico_agent.database import Database
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    AuditRecord,
    ContextSnapshot,
    Event,
    ModelCall,
    ModelEndpoint,
    Project,
    Run,
    RunStep,
    RuntimeSession,
    Task,
    Tenant,
    ToolApprovalRequest,
    ToolCall,
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
from nico_agent.tools import ToolGateway, ToolRegistry
from nico_agent.tools.builtin import (
    FileReadExecutor,
    FileWriteExecutor,
    WebSearchExecutor,
    WorkspaceManager,
)
from nico_agent.web.contracts import SearchPage, SearchResult
from nico_agent.web.registry import WebProviderRegistry

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
                "interpreted_intent": "Report the observed tool result",
                "confidence": 0.95,
                "candidates": [
                    {
                        "candidate_id": "report",
                        "intent": "Report the observed tool result",
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


class SequencedToolModelProvider:
    name = "openai_compatible"

    def describe_capabilities(self):
        return frozenset(
            {
                ModelCapability.STREAMING,
                ModelCapability.NATIVE_TOOL_CALLING,
                ModelCapability.JSON_OBJECT,
            }
        )

    async def stream(self, request):
        observations = sum(message.role == "tool" for message in request.messages)
        if observations == 0:
            async for event in self._tool(
                "write-call",
                "file.write",
                {"path": "react/proof.txt", "content": "gateway-only"},
            ):
                yield event
            return
        if observations == 1:
            async for event in self._tool(
                "read-call",
                "file.read",
                {"path": "react/proof.txt"},
            ):
                yield event
            return
        yield ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED)
        yield ModelStreamEvent(
            type=ModelStreamEventType.TEXT_DELTA,
            text_delta=_final_action("react complete"),
        )
        yield ModelStreamEvent(
            type=ModelStreamEventType.RESPONSE_COMPLETED,
            finish_reason="stop",
            usage=ModelUsage(input_tokens=10, output_tokens=2, total_tokens=12, status="exact"),
            provider_request_id="react-final",
        )

    @staticmethod
    async def _tool(call_id: str, name: str, arguments: dict):
        yield ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED)
        yield ModelStreamEvent(
            type=ModelStreamEventType.TOOL_CALL_DELTA,
            tool_index=0,
            tool_call_id=call_id,
            tool_name=name,
            tool_arguments_delta=json.dumps(arguments, separators=(",", ":")),
        )
        yield ModelStreamEvent(
            type=ModelStreamEventType.RESPONSE_COMPLETED,
            finish_reason="tool_calls",
            usage=ModelUsage(input_tokens=8, output_tokens=4, total_tokens=12, status="exact"),
            provider_request_id=f"react-{call_id}",
        )


class WebSearchModelProvider:
    name = "openai_compatible"

    def __init__(self) -> None:
        self.requests = []

    def describe_capabilities(self):
        return frozenset(
            {
                ModelCapability.STREAMING,
                ModelCapability.NATIVE_TOOL_CALLING,
                ModelCapability.JSON_OBJECT,
            }
        )

    async def stream(self, request):
        self.requests.append(request)
        call_key = str(request.metadata.get("call_key", ""))
        if call_key.startswith("citation-repair:react"):
            yield ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED)
            yield ModelStreamEvent(
                type=ModelStreamEventType.TEXT_DELTA,
                text_delta=_final_action("Found current documentation: https://docs.example/nico"),
            )
            yield ModelStreamEvent(
                type=ModelStreamEventType.RESPONSE_COMPLETED,
                finish_reason="stop",
                usage=ModelUsage(
                    input_tokens=10,
                    output_tokens=5,
                    total_tokens=15,
                    status="exact",
                ),
                provider_request_id="web-repair",
            )
            return
        observations = [message for message in request.messages if message.role == "tool"]
        if not observations:
            async for event in SequencedToolModelProvider._tool(
                "model-web-call",
                "web.search",
                {"query": "current Nico documentation", "count": 1},
            ):
                yield event
            return
        yield ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED)
        yield ModelStreamEvent(
            type=ModelStreamEventType.TEXT_DELTA,
            text_delta=_final_action("Found current documentation."),
        )
        yield ModelStreamEvent(
            type=ModelStreamEventType.RESPONSE_COMPLETED,
            finish_reason="stop",
            usage=ModelUsage(input_tokens=10, output_tokens=3, total_tokens=13, status="exact"),
            provider_request_id="web-final",
        )


class FakeNativeSearchProvider:
    key = "searxng"

    async def search(self, request, *, secret=None, config=None):
        assert request.query == "current Nico documentation"
        assert secret is None
        return SearchPage(
            provider="searxng",
            results=(
                SearchResult(
                    title="Nico documentation",
                    url="https://docs.example/nico",
                    snippet="Current platform documentation",
                ),
            ),
        )


@pytest.mark.asyncio
async def test_native_react_persists_multi_round_tool_and_checkpoint_graph(tmp_path) -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    policy = {
        "allow": ["file.write@1.0.0", "file.read@1.0.0"],
        "permissions": ["filesystem.write", "filesystem.read"],
    }
    try:
        suffix = uuid4().hex[:10]
        async with database.admin_transaction() as session:
            tenant = Tenant(
                name=f"ReAct {suffix}",
                slug=f"react-{suffix}",
                settings={"tool_policy": policy},
            )
            session.add(tenant)
            await session.flush()
            project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
            agent = Agent(
                tenant_id=tenant.id,
                name=f"agent-{suffix}",
                display_name="ReAct Agent",
            )
            endpoint = ModelEndpoint(
                tenant_id=tenant.id,
                stable_key="react-fake",
                revision=1,
                display_name="ReAct fake model",
                base_url="https://models.example/v1",
                credential_ref="env:NICO_MODEL_SECRET_TEST",
                allowed_models=["react-model"],
                capabilities={
                    "streaming": True,
                    "native_tool_calling": True,
                    "json_object": True,
                },
            )
            session.add_all([project, agent, endpoint])
            await session.flush()
            version = AgentVersion(
                tenant_id=tenant.id,
                agent_id=agent.id,
                version=1,
                status="published",
                role="tool user",
                mandate="Use authorized platform tools and report the result",
                runtime_provider="nico_native",
                execution_mode="react",
                model_endpoint_id=endpoint.id,
                model_name="react-model",
                tool_policy=policy,
                budgets={"max_iterations": 4, "max_tool_calls": 2},
                content_hash="c" * 64,
            )
            session.add(version)
            await session.flush()
            agent.current_version_id = version.id
            agent.status = "ready"
            task = Task(
                tenant_id=tenant.id,
                project_id=project.id,
                assignee_agent_id=agent.id,
                title="ReAct persistence",
                input={"prompt": "write then read"},
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
                max_steps=4,
                token_budget=1000,
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        model_gateway = ModelGateway(ModelProviderRegistry([SequencedToolModelProvider()]))
        workspace = WorkspaceManager(tmp_path / "workspaces")
        tool_gateway = ToolGateway(
            database,
            ToolRegistry([FileReadExecutor(workspace), FileWriteExecutor(workspace)]),
            approval_required_risks=frozenset(),
        )
        worker = RuntimeWorker(
            database,
            RuntimeProviderRegistry([NicoNativeRuntimeProvider(model_gateway)]),
            worker_id=f"react-{suffix}",
            lease_seconds=10,
            heartbeat_seconds=1,
            tool_gateway=tool_gateway,
        )

        assert await worker.execute_once() is True

        async with database.admin_transaction() as session:
            run_row = await session.scalar(select(Run).where(Run.id == run_id))
            runtime = await session.scalar(
                select(RuntimeSession).where(RuntimeSession.run_id == run_id)
            )
            calls = list(
                await session.scalars(
                    select(ModelCall)
                    .where(ModelCall.run_id == run_id)
                    .order_by(ModelCall.started_at, ModelCall.id)
                )
            )
            contexts = list(
                await session.scalars(
                    select(ContextSnapshot)
                    .where(ContextSnapshot.run_id == run_id)
                    .order_by(ContextSnapshot.version)
                )
            )
            tool_calls = list(
                await session.scalars(
                    select(ToolCall)
                    .where(ToolCall.run_id == run_id)
                    .order_by(ToolCall.started_at, ToolCall.id)
                )
            )
            steps = list(
                await session.scalars(
                    select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.sequence)
                )
            )

        assert run_row is not None and run_row.status == "completed"
        assert run_row.result == {"content": "react complete"}
        assert run_row.checkpoint_schema_version == 2
        assert run_row.checkpoint_revision >= 5
        assert run_row.checkpoint_hash == run_row.checkpoint["checkpoint_hash"]
        assert runtime is not None and runtime.loop_state == "completed"
        assert runtime.checkpoint_schema_version == 2
        assert runtime.checkpoint_hash == runtime.checkpoint["checkpoint_hash"]
        assert [call.call_key for call in calls] == ["model:1", "model:2", "model:3"]
        assert all(call.status == "completed" for call in calls)
        assert all(call.ended_at is not None and call.ended_at >= call.started_at for call in calls)
        assert [snapshot.version for snapshot in contexts] == [1, 2, 3]
        assert [call.tool_name for call in tool_calls] == ["file.write", "file.read"]
        assert all(call.status == "succeeded" and len(call.attempts) == 1 for call in tool_calls)
        assert tool_calls[1].result["content"] == "gateway-only"
        assert [step.sequence for step in steps] == list(range(1, len(steps) + 1))
        assert {step.step_type for step in steps} == {"reasoning", "tool"}
        assert all(step.context_snapshot_id is not None for step in steps)
        assert all(step.model_call_id is not None for step in steps)
        assert all(step.status == "completed" for step in steps)
        context_by_version = {snapshot.version: snapshot.id for snapshot in contexts}
        call_by_iteration = {int(call.call_key.removeprefix("model:")): call.id for call in calls}
        for step in steps:
            assert step.iteration is not None
            assert step.context_snapshot_id == context_by_version[step.iteration]
            assert step.model_call_id == call_by_iteration[step.iteration]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_native_react_observes_web_search_with_platform_tool_call_id() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    tool_config = {
        "provider": "searxng",
        "safe_search": "moderate",
        "cache_ttl_seconds": 60,
        "rate_limit_per_minute": 20,
    }
    policy = {
        "allow": ["web.search@1.0.0"],
        "permissions": ["network.web.search"],
        "tools": {"web.search@1.0.0": tool_config},
    }
    try:
        suffix = uuid4().hex[:10]
        async with database.admin_transaction() as session:
            tenant = Tenant(
                name=f"Native Web {suffix}",
                slug=f"native-web-{suffix}",
                settings={"tool_policy": policy},
            )
            session.add(tenant)
            await session.flush()
            project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
            agent = Agent(
                tenant_id=tenant.id,
                name=f"agent-{suffix}",
                display_name="Native Web Agent",
            )
            endpoint = ModelEndpoint(
                tenant_id=tenant.id,
                stable_key=f"native-web-{suffix}",
                revision=1,
                display_name="Native Web fake model",
                base_url="https://models.example/v1",
                credential_ref="env:NICO_MODEL_SECRET_TEST",
                allowed_models=["web-model"],
                capabilities={
                    "streaming": True,
                    "native_tool_calling": True,
                    "json_object": True,
                },
            )
            session.add_all([project, agent, endpoint])
            await session.flush()
            version = AgentVersion(
                tenant_id=tenant.id,
                agent_id=agent.id,
                version=1,
                status="published",
                role="web researcher",
                mandate="Search current public information",
                runtime_provider="nico_native",
                execution_mode="react",
                model_endpoint_id=endpoint.id,
                model_name="web-model",
                tool_policy=policy,
                budgets={"max_iterations": 3, "max_tool_calls": 1},
                content_hash="e" * 64,
            )
            session.add(version)
            await session.flush()
            agent.current_version_id = version.id
            agent.status = "ready"
            task = Task(
                tenant_id=tenant.id,
                project_id=project.id,
                assignee_agent_id=agent.id,
                title="Current Nico docs",
                input={"prompt": "Find current Nico documentation"},
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
                max_steps=3,
                token_budget=1000,
            )
            session.add(run)
            await session.flush()
            run_id = run.id

        model = WebSearchModelProvider()
        search = WebSearchExecutor(
            WebProviderRegistry([FakeNativeSearchProvider()]),
            environment="test",
        )
        worker = RuntimeWorker(
            database,
            RuntimeProviderRegistry(
                [NicoNativeRuntimeProvider(ModelGateway(ModelProviderRegistry([model])))]
            ),
            worker_id=f"native-web-{suffix}",
            lease_seconds=10,
            heartbeat_seconds=1,
            tool_gateway=ToolGateway(
                database,
                ToolRegistry([search]),
                approval_required_risks=frozenset(),
            ),
        )

        assert await worker.execute_once() is True

        async with database.admin_transaction() as session:
            completed = await session.get(Run, run_id)
            tool_calls = list(
                await session.scalars(select(ToolCall).where(ToolCall.run_id == run_id))
            )
        assert completed is not None and completed.status == "completed"
        assert completed.result == {
            "content": "Found current documentation: https://docs.example/nico"
        }
        assert len(tool_calls) == 1 and tool_calls[0].status == "succeeded"
        assert len(model.requests) == 3
        assert [tool.name for tool in model.requests[0].tools] == ["web.search"]
        observation = model.requests[1].messages[-1]
        assert observation.role == "tool"
        assert observation.tool_call_id == "model-web-call"
        payload = json.loads(observation.content or "{}")
        assert payload["tool_call_id"] == str(tool_calls[0].id)
        assert payload["output"]["provider"] == "searxng"
        assert payload["output"]["results"] == [
            {
                "published_at": None,
                "site_name": None,
                "snippet": "Current platform documentation",
                "title": "Nico documentation",
                "url": "https://docs.example/nico",
            }
        ]
        assert model.requests[2].tools == ()
        assert model.requests[2].metadata["call_key"].startswith("citation-repair:react")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sensitive_tool_approval_suspends_decides_and_recovers_exactly_once(
    tmp_path,
) -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    policy = {
        "allow": ["file.write@1.0.0", "file.read@1.0.0"],
        "permissions": ["filesystem.write", "filesystem.read"],
    }
    try:
        suffix = uuid4().hex[:10]
        async with database.admin_transaction() as session:
            tenant = Tenant(
                name=f"Approval {suffix}",
                slug=f"approval-{suffix}",
                settings={"tool_policy": policy},
            )
            session.add(tenant)
            await session.flush()
            project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
            agent = Agent(
                tenant_id=tenant.id,
                name=f"agent-{suffix}",
                display_name="Approval Agent",
            )
            endpoint = ModelEndpoint(
                tenant_id=tenant.id,
                stable_key=f"approval-fake-{suffix}",
                revision=1,
                display_name="Approval fake model",
                base_url="https://models.example/v1",
                credential_ref="env:NICO_MODEL_SECRET_TEST",
                allowed_models=["react-model"],
                capabilities={
                    "streaming": True,
                    "native_tool_calling": True,
                    "json_object": True,
                },
            )
            session.add_all([project, agent, endpoint])
            await session.flush()
            version = AgentVersion(
                tenant_id=tenant.id,
                agent_id=agent.id,
                version=1,
                status="published",
                role="sensitive tool user",
                mandate="Use authorized tools only after approval",
                runtime_provider="nico_native",
                execution_mode="react",
                model_endpoint_id=endpoint.id,
                model_name="react-model",
                tool_policy=policy,
                budgets={"max_iterations": 4, "max_tool_calls": 2},
                content_hash="d" * 64,
            )
            session.add(version)
            await session.flush()
            agent.current_version_id = version.id
            agent.status = "ready"
            task = Task(
                tenant_id=tenant.id,
                project_id=project.id,
                assignee_agent_id=agent.id,
                title="Approval recovery",
                input={"prompt": "write then read"},
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
                max_steps=4,
                token_budget=1000,
            )
            session.add(run)
            await session.flush()
            run_id = run.id
            tenant_id = tenant.id

        workspace_root = tmp_path / "approval-workspaces"
        model_gateway = ModelGateway(ModelProviderRegistry([SequencedToolModelProvider()]))
        tool_gateway = ToolGateway(
            database,
            ToolRegistry(
                [
                    FileReadExecutor(WorkspaceManager(workspace_root)),
                    FileWriteExecutor(WorkspaceManager(workspace_root)),
                ]
            ),
            approval_required_risks=frozenset({"medium", "high"}),
        )
        worker = RuntimeWorker(
            database,
            RuntimeProviderRegistry([NicoNativeRuntimeProvider(model_gateway)]),
            worker_id=f"approval-{suffix}",
            lease_seconds=10,
            heartbeat_seconds=1,
            tool_gateway=tool_gateway,
        )

        assert await worker.execute_once() is True

        async with database.admin_transaction() as session:
            waiting_run = await session.scalar(select(Run).where(Run.id == run_id))
            runtime = await session.scalar(
                select(RuntimeSession).where(RuntimeSession.run_id == run_id)
            )
            approval = await session.scalar(
                select(ToolApprovalRequest).where(ToolApprovalRequest.run_id == run_id)
            )
            pending_call = await session.scalar(select(ToolCall).where(ToolCall.run_id == run_id))
        assert waiting_run is not None and waiting_run.status == "waiting_for_approval"
        assert waiting_run.lease_owner is None
        assert runtime is not None and runtime.status == "suspended"
        assert approval is not None and approval.status == "requested"
        assert pending_call is not None and pending_call.status == "pending"
        assert not (workspace_root / str(tenant_id) / str(run_id) / "react/proof.txt").exists()

        app = create_app(settings=settings, database=database)
        headers = {
            "X-Tenant-ID": str(tenant_id),
            "X-Actor-ID": "approval-operator",
        }
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://approval.test"
        ) as client:
            listed = await client.get(
                "/api/v1/tool-approval-requests",
                params={"run_id": str(run_id), "status": "requested"},
                headers=headers,
            )
            assert listed.status_code == 200
            assert [item["id"] for item in listed.json()] == [str(approval.id)]
            isolated = await client.get(
                f"/api/v1/tool-approval-requests/{approval.id}",
                headers={**headers, "X-Tenant-ID": str(uuid4())},
            )
            assert isolated.status_code == 404
            decided = await client.post(
                f"/api/v1/tool-approval-requests/{approval.id}/decision",
                headers={**headers, "Idempotency-Key": f"decision-{suffix}"},
                json={
                    "expected_revision": approval.revision,
                    "decision": "approve",
                    "allowed_scope": "once",
                },
            )
            assert decided.status_code == 200
            assert decided.json()["status"] == "approved"
            replay = await client.post(
                f"/api/v1/tool-approval-requests/{approval.id}/decision",
                headers={**headers, "Idempotency-Key": f"decision-{suffix}"},
                json={
                    "expected_revision": approval.revision,
                    "decision": "approve",
                    "allowed_scope": "once",
                },
            )
            assert replay.status_code == 200
            conflict = await client.post(
                f"/api/v1/tool-approval-requests/{approval.id}/decision",
                headers={**headers, "Idempotency-Key": f"different-{suffix}"},
                json={
                    "expected_revision": approval.revision,
                    "decision": "reject",
                },
            )
            assert conflict.status_code == 409
            assert conflict.json()["code"] == "TOOL_APPROVAL_ALREADY_DECIDED"

        assert await worker.execute_once() is True

        async with database.admin_transaction() as session:
            completed_run = await session.scalar(select(Run).where(Run.id == run_id))
            calls = list(
                await session.scalars(
                    select(ToolCall)
                    .where(ToolCall.run_id == run_id)
                    .order_by(ToolCall.created_at, ToolCall.id)
                )
            )
            final_approval = await session.scalar(
                select(ToolApprovalRequest).where(ToolApprovalRequest.id == approval.id)
            )
            approval_events = list(
                await session.scalars(
                    select(Event).where(
                        Event.aggregate_type == "tool_approval_request",
                        Event.aggregate_id == approval.id,
                    )
                )
            )
            approval_audits = list(
                await session.scalars(
                    select(AuditRecord).where(
                        AuditRecord.resource_type == "tool_approval_request",
                        AuditRecord.resource_id == approval.id,
                    )
                )
            )
        assert completed_run is not None and completed_run.status == "completed"
        assert final_approval is not None and final_approval.status == "approved"
        assert {event.event_type for event in approval_events} == {
            "ApprovalRequested",
            "ToolApprovalApproved",
        }
        assert {record.action for record in approval_audits} == {
            "tool.approval.request",
            "tool.approval.approved",
        }
        assert [call.status for call in calls] == ["succeeded", "succeeded"]
        assert [len(call.attempts) for call in calls] == [1, 1]
        assert (
            workspace_root / str(tenant_id) / str(run_id) / "react/proof.txt"
        ).read_text() == "gateway-only"

        async with database.admin_transaction() as session:
            expiring_task = Task(
                tenant_id=tenant_id,
                project_id=project.id,
                assignee_agent_id=agent.id,
                title="Approval expiry",
                input={"prompt": "write then read"},
                status="running",
                priority=2_147_483_647,
            )
            session.add(expiring_task)
            await session.flush()
            expiring_run = Run(
                tenant_id=tenant_id,
                task_id=expiring_task.id,
                agent_id=agent.id,
                agent_version_id=version.id,
                attempt=1,
                max_steps=4,
                token_budget=1000,
            )
            session.add(expiring_run)
            await session.flush()
            expiring_run_id = expiring_run.id

        expiry_gateway = ToolGateway(
            database,
            ToolRegistry(
                [
                    FileReadExecutor(WorkspaceManager(workspace_root)),
                    FileWriteExecutor(WorkspaceManager(workspace_root)),
                ]
            ),
            approval_required_risks=frozenset({"medium", "high"}),
            approval_ttl_seconds=1,
        )
        expiry_worker = RuntimeWorker(
            database,
            RuntimeProviderRegistry([NicoNativeRuntimeProvider(model_gateway)]),
            worker_id=f"approval-expiry-{suffix}",
            lease_seconds=10,
            heartbeat_seconds=1,
            tool_gateway=expiry_gateway,
        )
        assert await expiry_worker.execute_once() is True
        await asyncio.sleep(1.05)
        assert await database.reconcile_expired_tool_approvals() == 1
        async with database.admin_transaction() as session:
            expired = await session.scalar(
                select(ToolApprovalRequest).where(ToolApprovalRequest.run_id == expiring_run_id)
            )
            woken = await session.scalar(select(Run).where(Run.id == expiring_run_id))
            expired_call = await session.scalar(
                select(ToolCall).where(ToolCall.run_id == expiring_run_id)
            )
        assert expired is not None and expired.status == "expired"
        assert woken is not None and woken.status == "running"
        assert expired_call is not None and expired_call.status == "failed"
        assert await expiry_worker.execute_once() is True
        async with database.admin_transaction() as session:
            recovered = await session.scalar(select(Run).where(Run.id == expiring_run_id))
        assert recovered is not None and recovered.status == "completed"
    finally:
        await engine.dispose()
