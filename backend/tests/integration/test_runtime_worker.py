from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.api import create_app
from nico_agent.config import Settings
from nico_agent.control_plane import ControlPlaneService
from nico_agent.database import Database, TenantContext
from nico_agent.domain.models import Agent, AgentVersion, Project, Run, Task, Tenant
from nico_agent.runtime import MockRuntimeProvider
from nico_agent.runtime.contracts import (
    RuntimeEvent,
    RuntimeEventType,
    RuntimeResult,
    RuntimeSessionStatus,
    RuntimeTrajectory,
)
from nico_agent.runtime.executor import RuntimeWorker
from nico_agent.runtime.registry import RuntimeProviderRegistry
from nico_agent.runtime.service import RuntimeExecutionService
from nico_agent.tools import ToolGateway, ToolRegistry
from nico_agent.tools.builtin import FileReadExecutor, FileWriteExecutor, WorkspaceManager

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with the Compose dependencies running",
)


async def seed_pending_run(
    database: Database,
    *,
    priority: int,
    run_config: dict,
    tenant_settings: dict | None = None,
    tool_policy: dict | None = None,
):
    suffix = uuid4().hex[:10]
    async with database.admin_transaction() as session:
        tenant = Tenant(
            name=f"Worker {suffix}",
            slug=f"worker-{suffix}",
            settings=tenant_settings or {},
        )
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Runtime Agent",
        )
        session.add_all([project, agent])
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="runtime-test",
            mandate="Execute the deterministic runtime test",
            run_config=run_config,
            tool_policy=tool_policy or {},
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
            title="Runtime acceptance",
            input={"question": "test"},
            status="running",
            priority=priority,
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
        return {"id": run.id, "tenant_id": tenant.id}


async def _row(database: Database, table: str, run_id):
    key = "id" if table == "runs" else "run_id"
    async with database.admin_transaction() as session:
        return (
            (
                await session.execute(
                    text(f"SELECT * FROM {table} WHERE {key} = :run_id"), {"run_id": run_id}
                )
            )
            .mappings()
            .one()
        )


@pytest.mark.asyncio
async def test_mock_worker_persists_complete_runtime_trajectory() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        seeded = await seed_pending_run(
            database,
            priority=100,
            run_config={
                "runtime_provider": "mock",
                "mock": {"steps": ["plan", "execute"], "output": {"answer": 42}},
            },
        )
        worker = RuntimeWorker(
            database,
            RuntimeProviderRegistry([MockRuntimeProvider()]),
            worker_id="mock-success",
            lease_seconds=5,
            heartbeat_seconds=0.05,
        )

        assert await worker.execute_once() is True
        run = await _row(database, "runs", seeded["id"])
        runtime_session = await _row(database, "runtime_sessions", seeded["id"])
        async with database.admin_transaction() as session:
            step_count = await session.scalar(
                text("SELECT count(*) FROM run_steps WHERE run_id = :run_id"),
                {"run_id": seeded["id"]},
            )
            event_count = await session.scalar(
                text("SELECT count(*) FROM events WHERE run_id = :run_id"),
                {"run_id": seeded["id"]},
            )
            audit_count = await session.scalar(
                text(
                    "SELECT count(*) FROM audit_records "
                    "WHERE details->>'provider_sequence' IS NOT NULL"
                )
            )

        assert run["status"] == "completed"
        assert run["result"] == {"answer": 42}
        assert run["lease_token"] is None
        assert runtime_session["status"] == "completed"
        assert runtime_session["provider_name"] == "mock"
        assert runtime_session["provider_resolution_source"] == "legacy_run_config"
        assert runtime_session["legacy_resolver_used"] is True
        assert runtime_session["provider_compatibility"]["implementation"] == "test"
        assert runtime_session["trajectory"]["events"][-1]["type"] == "run.completed"
        assert step_count == 2
        assert event_count >= 10
        assert audit_count >= 1
        async with database.admin_transaction() as session:
            telemetry = (
                (
                    await session.execute(
                        text(
                            "SELECT event_type, payload FROM events "
                            "WHERE run_id = :run_id AND event_type = "
                            "'LegacyRuntimeProviderResolved'"
                        ),
                        {"run_id": seeded["id"]},
                    )
                )
                .mappings()
                .one()
            )
        assert telemetry["payload"]["source"] == "legacy_run_config"
        assert telemetry["payload"]["removal_version"] == "0.4.0"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_mock_runtime_executes_real_tools_only_through_gateway(tmp_path) -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    references = ["file.write@1.0.0", "file.read@1.0.0"]
    permissions = ["filesystem.write", "filesystem.read"]
    policy = {"allow": references, "permissions": permissions}
    try:
        seeded = await seed_pending_run(
            database,
            priority=200,
            tenant_settings={"tool_policy": policy},
            tool_policy=policy,
            run_config={
                "runtime_provider": "mock",
                "mock": {
                    "steps": ["use-tools"],
                    "tool_calls": [
                        {
                            "name": "file.write",
                            "version": "1.0.0",
                            "arguments": {"path": "runtime/result.txt", "content": "gateway"},
                        },
                        {
                            "name": "file.read",
                            "version": "1.0.0",
                            "arguments": {"path": "runtime/result.txt"},
                        },
                    ],
                },
            },
        )
        workspace = WorkspaceManager(tmp_path / "workspaces")
        tool_registry = ToolRegistry([FileReadExecutor(workspace), FileWriteExecutor(workspace)])
        worker = RuntimeWorker(
            database,
            RuntimeProviderRegistry([MockRuntimeProvider()]),
            worker_id="mock-tools",
            lease_seconds=5,
            heartbeat_seconds=0.05,
            tool_gateway=ToolGateway(database, tool_registry, approval_required_risks=frozenset()),
        )

        assert await worker.execute_once() is True
        run = await _row(database, "runs", seeded["id"])
        runtime_session = await _row(database, "runtime_sessions", seeded["id"])
        async with database.admin_transaction() as session:
            calls = (
                (
                    await session.execute(
                        text(
                            "SELECT tool_name, status, result FROM tool_calls "
                            "WHERE run_id = :run_id ORDER BY started_at"
                        ),
                        {"run_id": seeded["id"]},
                    )
                )
                .mappings()
                .all()
            )

        assert run["status"] == "completed"
        assert [call["tool_name"] for call in calls] == ["file.write", "file.read"]
        assert all(call["status"] == "succeeded" for call in calls)
        assert calls[1]["result"]["content"] == "gateway"
        assert run["result"]["tool_results"][1]["output"]["content"] == "gateway"
        event_types = [event["type"] for event in runtime_session["trajectory"]["events"]]
        assert "tool.call.started" in event_types
        assert "tool.call.completed" in event_types

        app = create_app(settings=settings, health_service=object(), database=database)
        headers = {
            "X-Tenant-ID": str(seeded["tenant_id"]),
            "X-Actor-ID": "tool-api-test",
        }
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            definitions_response = await client.get("/api/v1/tool-definitions", headers=headers)
            calls_response = await client.get(
                f"/api/v1/runs/{seeded['id']}/tool-calls", headers=headers
            )
        assert definitions_response.status_code == 200
        assert [item["name"] for item in definitions_response.json()] == [
            "file.read",
            "file.write",
        ]
        assert calls_response.status_code == 200
        assert [item["status"] for item in calls_response.json()] == [
            "succeeded",
            "succeeded",
        ]
        assert all("execution_lease_token" not in item for item in calls_response.json())
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_expired_run_recovers_from_checkpoint_and_rejects_old_result() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    registry = RuntimeProviderRegistry([MockRuntimeProvider()])
    service = RuntimeExecutionService(database)
    try:
        seeded = await seed_pending_run(
            database,
            priority=100,
            run_config={
                "runtime_provider": "mock",
                "mock": {"steps": ["first", "second"], "output": {"recovered": True}},
            },
        )
        old_claim = await database.claim_next_run("crashed-worker", 5)
        assert old_claim is not None and old_claim.run_id == seeded["id"]
        prepared = await service.prepare_claim(
            old_claim, worker_id="crashed-worker", registry=registry
        )
        provider = registry.get("mock")
        handle = await provider.create_session(prepared.request)
        await service.bind_session(prepared, worker_id="crashed-worker", handle=handle)
        for event in (
            RuntimeEvent(sequence=1, type=RuntimeEventType.SESSION_CREATED),
            RuntimeEvent(sequence=2, type=RuntimeEventType.RUN_STARTED),
            RuntimeEvent(
                sequence=3,
                type=RuntimeEventType.STEP_STARTED,
                payload={"index": 1, "name": "first"},
            ),
            RuntimeEvent(
                sequence=4,
                type=RuntimeEventType.STEP_COMPLETED,
                payload={"index": 1, "name": "first"},
            ),
            RuntimeEvent(
                sequence=5,
                type=RuntimeEventType.CHECKPOINT_SAVED,
                payload={"completed_steps": 1},
            ),
        ):
            await service.record_event(prepared, worker_id="crashed-worker", event=event)

        async with database.admin_transaction() as session:
            await session.execute(
                text("UPDATE runs SET lease_expires_at = :expired WHERE id = :run_id"),
                {
                    "expired": datetime.now(UTC) - timedelta(seconds=1),
                    "run_id": seeded["id"],
                },
            )

        replacement = RuntimeWorker(
            database,
            RuntimeProviderRegistry([MockRuntimeProvider()]),
            worker_id="recovery-worker",
            lease_seconds=5,
            heartbeat_seconds=0.05,
        )
        assert await replacement.execute_once() is True

        stale_result = RuntimeResult(
            status=RuntimeSessionStatus.COMPLETED,
            output={"stale": True},
        )
        stale_trajectory = RuntimeTrajectory(
            provider="mock",
            provider_version="1.0",
            external_session_id=handle.external_session_id,
            status=RuntimeSessionStatus.COMPLETED,
            events=[],
        )
        assert (
            await service.complete_claim(
                prepared,
                worker_id="crashed-worker",
                result=stale_result,
                trajectory=stale_trajectory,
            )
            is False
        )
        run = await _row(database, "runs", seeded["id"])
        runtime_session = await _row(database, "runtime_sessions", seeded["id"])
        async with database.admin_transaction() as session:
            steps = (
                await session.execute(
                    text(
                        "SELECT sequence, status FROM run_steps "
                        "WHERE run_id = :run_id ORDER BY sequence"
                    ),
                    {"run_id": seeded["id"]},
                )
            ).all()

        assert run["status"] == "completed"
        assert run["result"] == {"recovered": True}
        assert runtime_session["last_event_sequence"] > 5
        assert steps == [(1, "completed"), (2, "completed")]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_authoritative_cancel_prevents_late_worker_completion() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        seeded = await seed_pending_run(
            database,
            priority=100,
            run_config={
                "runtime_provider": "mock",
                "mock": {"steps": ["slow"], "delay_seconds": 0.4},
            },
        )
        worker = RuntimeWorker(
            database,
            RuntimeProviderRegistry([MockRuntimeProvider()]),
            worker_id="cancel-worker",
            lease_seconds=5,
            heartbeat_seconds=0.05,
        )
        execution = asyncio.create_task(worker.execute_once())
        for _ in range(50):
            async with database.admin_transaction() as session:
                run = (
                    (
                        await session.execute(
                            text("SELECT status, revision FROM runs WHERE id = :run_id"),
                            {"run_id": seeded["id"]},
                        )
                    )
                    .mappings()
                    .one()
                )
            if run["status"] == "running":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("worker did not enter running state")

        context = TenantContext(seeded["tenant_id"], "cancel-test", uuid4())
        await ControlPlaneService(database).cancel_run(
            context, seeded["id"], expected_revision=run["revision"]
        )
        assert await execution is True

        final_run = await _row(database, "runs", seeded["id"])
        runtime_session = await _row(database, "runtime_sessions", seeded["id"])
        assert final_run["status"] == "cancelled"
        assert final_run["result"] is None
        assert final_run["lease_token"] is None
        assert runtime_session["status"] == "cancelled"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_mock_failure_and_unknown_provider_become_terminal() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        failed = await seed_pending_run(
            database,
            priority=100,
            run_config={
                "runtime_provider": "mock",
                "mock": {"fail": True, "error_code": "MODEL_FAILED"},
            },
        )
        worker = RuntimeWorker(
            database,
            RuntimeProviderRegistry([MockRuntimeProvider()]),
            worker_id="failure-worker",
            lease_seconds=5,
            heartbeat_seconds=0.05,
        )
        assert await worker.execute_once() is True
        failed_run = await _row(database, "runs", failed["id"])
        failed_session = await _row(database, "runtime_sessions", failed["id"])
        assert failed_run["status"] == "failed"
        assert failed_run["error"]["code"] == "MODEL_FAILED"
        assert failed_session["status"] == "failed"
        assert failed_run["lease_token"] is None

        missing = await seed_pending_run(
            database,
            priority=100,
            run_config={"runtime_provider": "not-registered"},
        )
        assert await worker.execute_once() is True
        missing_run = await _row(database, "runs", missing["id"])
        assert missing_run["status"] == "failed"
        assert missing_run["error"]["code"] == "RUNTIME_PROVIDER_NOT_FOUND"
        assert missing_run["lease_token"] is None
    finally:
        await engine.dispose()
