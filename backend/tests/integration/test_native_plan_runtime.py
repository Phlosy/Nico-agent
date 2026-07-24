from __future__ import annotations

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
    ModelCall,
    ModelEndpoint,
    Plan,
    PlanStep,
    Project,
    Run,
    RunStep,
    RuntimeEvaluation,
    Task,
    Tenant,
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
from nico_agent.tools.builtin import FileWriteExecutor, WorkspaceManager

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


class ReplanningModelProvider:
    name = "openai_compatible"

    def describe_capabilities(self):
        return frozenset(
            {
                ModelCapability.STREAMING,
                ModelCapability.NATIVE_TOOL_CALLING,
                ModelCapability.JSON_SCHEMA,
            }
        )

    async def stream(self, request):
        call_key = request.metadata["call_key"]
        payload = {
            "planner:1": self._plan("draft"),
            "plan:1:step:draft:attempt:1:round:1": {"output": {"content": ""}},
            "reflection:1": {
                "decision": "replan",
                "reason": "Draft failed deterministic validation",
                "recovery_instruction": "Produce a non-empty corrected report",
            },
            "planner:2": self._plan("correct"),
            "plan:2:step:correct:attempt:2:round:1": {"output": {"content": "verified report"}},
        }[call_key]
        if call_key.startswith("plan:"):
            payload = _plan_step_action(payload)
        yield ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED)
        yield ModelStreamEvent(
            type=ModelStreamEventType.TEXT_DELTA,
            text_delta=json.dumps(payload, separators=(",", ":")),
        )
        yield ModelStreamEvent(
            type=ModelStreamEventType.RESPONSE_COMPLETED,
            finish_reason="stop",
            usage=ModelUsage(input_tokens=12, output_tokens=6, total_tokens=18, status="exact"),
            provider_request_id=f"provider-{call_key}",
        )

    @staticmethod
    def _plan(step_key: str) -> dict:
        return {
            "objective": "Produce a verified report",
            "steps": [
                {
                    "key": step_key,
                    "title": step_key.title(),
                    "instruction": f"Execute {step_key}",
                    "acceptance": {"non_empty": True},
                    "depends_on": [],
                }
            ],
        }


class PlanningToolModelProvider:
    name = "openai_compatible"

    def describe_capabilities(self):
        return frozenset(
            {
                ModelCapability.STREAMING,
                ModelCapability.NATIVE_TOOL_CALLING,
                ModelCapability.JSON_SCHEMA,
            }
        )

    async def stream(self, request):
        call_key = request.metadata["call_key"]
        if call_key == "planner:1":
            async for event in self._json(
                {
                    "objective": "Persist a report through the Tool Gateway",
                    "steps": [
                        {
                            "key": "persist",
                            "title": "Persist report",
                            "instruction": "Write the report and return its path",
                            "acceptance": {"non_empty": True, "required": ["path"]},
                            "depends_on": [],
                        }
                    ],
                },
                call_key,
            ):
                yield event
            return
        if call_key == "plan:1:step:persist:attempt:1:round:1":
            yield ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED)
            yield ModelStreamEvent(
                type=ModelStreamEventType.TOOL_CALL_DELTA,
                tool_index=0,
                tool_call_id="plan-write",
                tool_name="file.write",
                tool_arguments_delta=json.dumps(
                    {"path": "plan/report.txt", "content": "planned through gateway"},
                    separators=(",", ":"),
                ),
            )
            yield ModelStreamEvent(
                type=ModelStreamEventType.RESPONSE_COMPLETED,
                finish_reason="tool_calls",
                usage=ModelUsage(
                    input_tokens=8,
                    output_tokens=4,
                    total_tokens=12,
                    status="exact",
                ),
                provider_request_id="plan-tool-call",
            )
            return
        assert call_key == "plan:1:step:persist:attempt:1:round:2"
        assert any(message.role == "tool" for message in request.messages)
        async for event in self._json({"output": {"path": "plan/report.txt"}}, call_key):
            yield event

    @staticmethod
    async def _json(payload: dict, call_key: str):
        if call_key.startswith("plan:"):
            payload = _plan_step_action(payload)
        yield ModelStreamEvent(type=ModelStreamEventType.RESPONSE_STARTED)
        yield ModelStreamEvent(
            type=ModelStreamEventType.TEXT_DELTA,
            text_delta=json.dumps(payload, separators=(",", ":")),
        )
        yield ModelStreamEvent(
            type=ModelStreamEventType.RESPONSE_COMPLETED,
            finish_reason="stop",
            usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15, status="exact"),
            provider_request_id=f"provider-{call_key}",
        )


def _plan_step_action(payload: dict) -> dict:
    return {
        "type": "final",
        "content": json.dumps(payload, separators=(",", ":")),
        "intent": {
            "interpreted_intent": "Execute the current Plan step",
            "confidence": 0.95,
            "candidates": [
                {
                    "candidate_id": "execute-step",
                    "intent": "Execute the current Plan step",
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
    }


@pytest.mark.asyncio
async def test_native_plan_runtime_persists_revisions_evaluations_and_read_api() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        suffix = uuid4().hex[:10]
        async with database.admin_transaction() as session:
            tenant = Tenant(name=f"Plan {suffix}", slug=f"plan-{suffix}")
            session.add(tenant)
            await session.flush()
            project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
            agent = Agent(
                tenant_id=tenant.id,
                name=f"agent-{suffix}",
                display_name="Planning Agent",
            )
            endpoint = ModelEndpoint(
                tenant_id=tenant.id,
                stable_key="plan-fake",
                revision=1,
                display_name="Plan fake model",
                base_url="https://models.example/v1",
                credential_ref="env:NICO_MODEL_SECRET_TEST",
                allowed_models=["plan-model"],
                capabilities={"streaming": True, "json_schema": True},
            )
            session.add_all([project, agent, endpoint])
            await session.flush()
            version = AgentVersion(
                tenant_id=tenant.id,
                agent_id=agent.id,
                version=1,
                status="published",
                role="planner",
                mandate="Plan, validate, reflect, and complete",
                runtime_provider="nico_native",
                execution_mode="plan_and_execute",
                model_endpoint_id=endpoint.id,
                model_name="plan-model",
                budgets={"max_plan_steps": 4, "max_reflections": 2, "max_replans": 1},
                content_hash="p" * 64,
            )
            session.add(version)
            await session.flush()
            agent.current_version_id = version.id
            agent.status = "ready"
            task = Task(
                tenant_id=tenant.id,
                project_id=project.id,
                assignee_agent_id=agent.id,
                title="Plan persistence",
                input={"prompt": "produce report"},
                acceptance={"non_empty": True, "required": ["content"]},
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
                max_steps=8,
                token_budget=2000,
            )
            session.add(run)
            await session.flush()
            tenant_id, run_id = tenant.id, run.id

        gateway = ModelGateway(ModelProviderRegistry([ReplanningModelProvider()]))
        worker = RuntimeWorker(
            database,
            RuntimeProviderRegistry([NicoNativeRuntimeProvider(gateway)]),
            worker_id=f"plan-{suffix}",
            lease_seconds=10,
            heartbeat_seconds=1,
        )
        assert await worker.execute_once() is True

        async with database.admin_transaction() as session:
            run_row = await session.scalar(select(Run).where(Run.id == run_id))
            plans = list(
                await session.scalars(
                    select(Plan).where(Plan.run_id == run_id).order_by(Plan.revision)
                )
            )
            steps = list(
                await session.scalars(
                    select(PlanStep)
                    .where(PlanStep.run_id == run_id)
                    .order_by(PlanStep.created_at, PlanStep.position)
                )
            )
            evaluations = list(
                await session.scalars(
                    select(RuntimeEvaluation)
                    .where(RuntimeEvaluation.run_id == run_id)
                    .order_by(RuntimeEvaluation.sequence)
                )
            )
            calls = list(
                await session.scalars(
                    select(ModelCall)
                    .where(ModelCall.run_id == run_id)
                    .order_by(ModelCall.started_at, ModelCall.id)
                )
            )
            run_steps = list(
                await session.scalars(
                    select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.sequence)
                )
            )

        assert run_row is not None and run_row.status == "completed"
        assert run_row.result["result"] == {"content": "verified report"}
        assert run_row.checkpoint_schema_version == 3
        assert [plan.status for plan in plans] == ["superseded", "completed"]
        assert plans[1].supersedes_plan_id == plans[0].id
        step_by_plan = {step.plan_id: step for step in steps}
        assert step_by_plan[plans[0].id].status == "failed"
        assert step_by_plan[plans[1].id].status == "completed"
        assert [evaluation.evaluation_type for evaluation in evaluations] == [
            "step_validation",
            "reflection",
            "step_validation",
            "completion",
        ]
        assert evaluations[1].method == "model"
        assert evaluations[1].model_call_id is not None
        assert [call.call_key for call in calls] == [
            "planner:1",
            "plan:1:step:draft:attempt:1:round:1",
            "reflection:1",
            "planner:2",
            "plan:2:step:correct:attempt:2:round:1",
        ]
        assert [step.status for step in run_steps] == ["failed", "completed"]

        app = create_app(settings=settings, health_service=object(), database=database)
        headers = {"X-Tenant-ID": str(tenant_id), "X-Actor-ID": "plan-api-test"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            plan_response = await client.get(f"/api/v1/runs/{run_id}/plans", headers=headers)
            assert plan_response.status_code == 200
            assert [item["revision"] for item in plan_response.json()] == [1, 2]
            first_plan_id = plan_response.json()[0]["id"]
            step_response = await client.get(
                f"/api/v1/runs/{run_id}/plans/{first_plan_id}/steps", headers=headers
            )
            assert step_response.status_code == 200
            assert step_response.json()[0]["status"] == "failed"
            evaluation_response = await client.get(
                f"/api/v1/runs/{run_id}/runtime-evaluations", headers=headers
            )
            assert evaluation_response.status_code == 200
            assert len(evaluation_response.json()) == 4
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_native_plan_step_uses_real_tool_gateway_and_parent_trace(tmp_path) -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    policy = {
        "allow": ["file.write@1.0.0"],
        "permissions": ["filesystem.write"],
    }
    try:
        suffix = uuid4().hex[:10]
        async with database.admin_transaction() as session:
            tenant = Tenant(
                name=f"Plan tool {suffix}",
                slug=f"plan-tool-{suffix}",
                settings={"tool_policy": policy},
            )
            session.add(tenant)
            await session.flush()
            project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
            agent = Agent(
                tenant_id=tenant.id,
                name=f"agent-{suffix}",
                display_name="Planning Tool Agent",
            )
            endpoint = ModelEndpoint(
                tenant_id=tenant.id,
                stable_key="plan-tool-fake",
                revision=1,
                display_name="Plan tool fake model",
                base_url="https://models.example/v1",
                credential_ref="env:NICO_MODEL_SECRET_TEST",
                allowed_models=["plan-tool-model"],
                capabilities={"streaming": True, "json_schema": True, "native_tool_calling": True},
            )
            session.add_all([project, agent, endpoint])
            await session.flush()
            version = AgentVersion(
                tenant_id=tenant.id,
                agent_id=agent.id,
                version=1,
                status="published",
                role="planning tool user",
                mandate="Execute a bounded Plan through authorized tools",
                runtime_provider="nico_native",
                execution_mode="plan_and_execute",
                model_endpoint_id=endpoint.id,
                model_name="plan-tool-model",
                tool_policy=policy,
                budgets={
                    "max_plan_steps": 2,
                    "max_step_iterations": 3,
                    "max_tool_calls": 1,
                },
                content_hash="t" * 64,
            )
            session.add(version)
            await session.flush()
            agent.current_version_id = version.id
            agent.status = "ready"
            task = Task(
                tenant_id=tenant.id,
                project_id=project.id,
                assignee_agent_id=agent.id,
                title="Plan tool persistence",
                input={"prompt": "write the report"},
                acceptance={"non_empty": True, "required": ["path"]},
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

        model_gateway = ModelGateway(ModelProviderRegistry([PlanningToolModelProvider()]))
        workspace = WorkspaceManager(tmp_path / "workspaces")
        tool_gateway = ToolGateway(
            database,
            ToolRegistry([FileWriteExecutor(workspace)]),
            approval_required_risks=frozenset(),
        )
        worker = RuntimeWorker(
            database,
            RuntimeProviderRegistry([NicoNativeRuntimeProvider(model_gateway)]),
            worker_id=f"plan-tool-{suffix}",
            lease_seconds=10,
            heartbeat_seconds=1,
            tool_gateway=tool_gateway,
        )

        assert await worker.execute_once() is True

        async with database.admin_transaction() as session:
            run_row = await session.scalar(select(Run).where(Run.id == run_id))
            tool_call = await session.scalar(select(ToolCall).where(ToolCall.run_id == run_id))
            steps = list(
                await session.scalars(
                    select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.sequence)
                )
            )
            calls = list(
                await session.scalars(
                    select(ModelCall)
                    .where(ModelCall.run_id == run_id)
                    .order_by(ModelCall.started_at, ModelCall.id)
                )
            )

        assert run_row is not None and run_row.status == "completed"
        assert run_row.result["result"] == {"path": "plan/report.txt"}
        assert run_row.checkpoint["usage"]["tool_calls"] == 1
        assert tool_call is not None and tool_call.status == "succeeded"
        assert tool_call.tool_name == "file.write"
        assert [call.call_key for call in calls] == [
            "planner:1",
            "plan:1:step:persist:attempt:1:round:1",
            "plan:1:step:persist:attempt:1:round:2",
        ]
        plan_step = next(step for step in steps if step.step_key == "plan:1:persist:attempt:1")
        tool_step = next(step for step in steps if step.step_type == "tool")
        assert plan_step.status == "completed"
        assert tool_step.status == "completed"
        assert tool_step.parent_step_id == plan_step.id
        assert tool_step.context_snapshot_id is not None
        assert tool_step.model_call_id is not None
        assert (
            tmp_path / "workspaces" / str(tool_call.tenant_id) / str(run_id) / "plan" / "report.txt"
        ).read_text() == "planned through gateway"
    finally:
        await engine.dispose()
