from __future__ import annotations

import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.api import create_app
from nico_agent.config import Settings
from nico_agent.database import Database
from nico_agent.domain.models import Agent, AgentVersion, Event, Project, Run, Task, Tenant

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


@pytest.mark.asyncio
async def test_model_endpoint_default_native_version_and_resumable_sse() -> None:
    settings = Settings(
        environment="test",
        model_endpoint_writes_enabled=True,
        _env_file=None,
    )
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        suffix = uuid4().hex[:10]
        async with database.admin_transaction() as session:
            tenant = Tenant(name=f"API {suffix}", slug=f"model-api-{suffix}")
            session.add(tenant)
            await session.flush()
            project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
            agent = Agent(
                tenant_id=tenant.id,
                name=f"agent-{suffix}",
                display_name="Model API Agent",
            )
            session.add_all([project, agent])
            await session.flush()
            legacy = AgentVersion(
                tenant_id=tenant.id,
                agent_id=agent.id,
                version=1,
                status="published",
                role="legacy",
                mandate="Legacy fixture",
                content_hash="d" * 64,
            )
            session.add(legacy)
            await session.flush()
            agent.current_version_id = legacy.id
            agent.status = "ready"
            task = Task(
                tenant_id=tenant.id,
                project_id=project.id,
                assignee_agent_id=agent.id,
                title="SSE fixture",
                status="completed",
            )
            session.add(task)
            await session.flush()
            run = Run(
                tenant_id=tenant.id,
                task_id=task.id,
                agent_id=agent.id,
                agent_version_id=legacy.id,
                attempt=1,
                status="completed",
                result={"content": "done"},
            )
            session.add(run)
            await session.flush()
            session.add_all(
                [
                    Event(
                        tenant_id=tenant.id,
                        event_type="First",
                        aggregate_type="run",
                        aggregate_id=run.id,
                        run_id=run.id,
                        actor_id="fixture",
                        payload={"value": 1},
                        correlation_id=uuid4(),
                    ),
                    Event(
                        tenant_id=tenant.id,
                        event_type="Second",
                        aggregate_type="run",
                        aggregate_id=run.id,
                        run_id=run.id,
                        actor_id="fixture",
                        payload={"value": 2},
                        correlation_id=uuid4(),
                    ),
                ]
            )
            await session.flush()
            tenant_id = tenant.id
            agent_id = agent.id
            run_id = run.id

        app = create_app(
            settings=settings,
            health_service=object(),
            database=database,
        )
        headers = {"X-Tenant-ID": str(tenant_id), "X-Actor-ID": "model-api-test"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            endpoint_response = await client.post(
                "/api/v1/model-endpoints",
                headers=headers,
                json={
                    "stable_key": "primary-model",
                    "display_name": "Primary model",
                    "base_url": "https://models.example/v1",
                    "credential_ref": "env:NICO_MODEL_SECRET_TEST",
                    "allowed_models": ["test-model"],
                },
            )
            assert endpoint_response.status_code == 201
            endpoint = endpoint_response.json()
            assert endpoint["revision"] == 1
            assert "canary" not in str(endpoint).lower()

            version_response = await client.post(
                f"/api/v1/agents/{agent_id}/versions",
                headers=headers,
                json={
                    "role": "native",
                    "mandate": "Use Nico native runtime",
                    "model_endpoint_id": endpoint["id"],
                    "model_name": "test-model",
                },
            )
            assert version_response.status_code == 201
            version = version_response.json()
            assert version["runtime_provider"] == "nico_native"
            assert version["execution_mode"] == "direct"

            stream_response = await client.get(
                f"/api/v1/runs/{run_id}/events/stream",
                headers={**headers, "Last-Event-ID": "0"},
            )
            assert stream_response.status_code == 200
            assert "event: First" in stream_response.text
            assert "event: Second" in stream_response.text
    finally:
        await engine.dispose()
