from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text, update
from sqlalchemy.exc import ProgrammingError

from nico_agent.api import create_app
from nico_agent.config import Settings
from nico_agent.domain.models import Run

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


@pytest.fixture
async def app_client() -> AsyncIterator[tuple[object, AsyncClient]]:
    app = create_app(settings=Settings(environment="test", _env_file=None))
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield app, client


async def _runnable_task(client: AsyncClient) -> tuple[dict[str, str], str]:
    suffix = uuid4().hex[:10]
    tenant = await client.post(
        "/api/v1/tenants/bootstrap",
        json={"name": f"Maintenance {suffix}", "slug": f"maintenance-{suffix}"},
        headers={"X-Actor-ID": "maintenance-bootstrap"},
    )
    assert tenant.status_code == 201, tenant.text
    headers = {
        "X-Tenant-ID": tenant.json()["id"],
        "X-Actor-ID": "maintenance-test",
    }
    project = await client.post(
        "/api/v1/projects",
        json={"name": f"maintenance-{suffix}"},
        headers=headers,
    )
    agent = await client.post(
        "/api/v1/agents",
        json={"name": f"maintenance-{suffix}", "display_name": "Maintenance"},
        headers=headers,
    )
    version = await client.post(
        f"/api/v1/agents/{agent.json()['id']}/versions",
        json={"role": "assistant", "mandate": "test maintenance"},
        headers=headers,
    )
    published = await client.post(
        f"/api/v1/agents/{agent.json()['id']}/versions/{version.json()['id']}/publish",
        json={"expected_revision": agent.json()["revision"]},
        headers=headers,
    )
    assert published.status_code == 200, published.text
    task = await client.post(
        "/api/v1/tasks",
        json={
            "project_id": project.json()["id"],
            "title": "maintenance race",
            "assignee_agent_id": agent.json()["id"],
        },
        headers=headers,
    )
    assert task.status_code == 201, task.text
    return headers, task.json()["id"]


@pytest.mark.asyncio
async def test_maintenance_blocks_run_insert_and_claim_but_not_probe_claiming(app_client) -> None:
    app, client = app_client
    database = app.state.database
    headers, task_id = await _runnable_task(client)
    async with database.admin_transaction() as session:
        await session.execute(
            update(Run)
            .where(Run.status.not_in({"completed", "failed", "cancelled", "timed_out"}))
            .values(status="cancelled", revision=Run.revision + 1)
        )
    attempt_id = uuid4()
    token = "maintenance-capability-" + uuid4().hex
    lease = await database.acquire_runtime_maintenance(attempt_id, token, 120)
    assert lease.acquired is True
    try:
        blocked = await client.post(
            f"/api/v1/tasks/{task_id}/runs",
            json={},
            headers=headers,
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["code"] == "RUNTIME_MAINTENANCE"
        assert await database.claim_next_run("maintenance-worker", 30) is None
        catalog = (await client.get("/api/v1/provider-catalog")).json()
        provider = next(item for item in catalog["providers"] if item["key"] == "openai")
        probe = await client.post(
            "/api/v1/provider-probes",
            headers=headers,
            json={
                "kind": "verify_completion",
                "candidate": {
                    "provider_key": "openai",
                    "protocol": provider["protocol"],
                    "base_url": provider["locations"][0]["base_url"],
                    "credential_ref": "env:NICO_MODEL_SECRET_MAINTENANCE",
                    "model": provider["recommended_models"][0],
                    "catalog_revision": catalog["catalog_revision"],
                },
                "idempotency_key": f"maintenance-probe-{uuid4().hex}",
            },
        )
        assert probe.status_code == 201, probe.text
        probe_claim = await database.claim_next_provider_probe("maintenance-probe-worker", 90)
        assert probe_claim is not None
        assert str(probe_claim.probe_id) == probe.json()["id"]
        cancelled = await client.post(
            f"/api/v1/provider-probes/{probe.json()['id']}/cancel",
            headers=headers,
        )
        assert cancelled.status_code == 200
        assert await database.renew_runtime_maintenance(attempt_id, "x" * 32, 120) is False
        assert await database.release_runtime_maintenance(attempt_id, "x" * 32) is False
    finally:
        assert await database.release_runtime_maintenance(attempt_id, token) is True

    accepted = await client.post(
        f"/api/v1/tasks/{task_id}/runs",
        json={},
        headers=headers,
    )
    assert accepted.status_code == 201, accepted.text
    refused = await database.acquire_runtime_maintenance(uuid4(), "y" * 40, 120)
    assert refused.acquired is False
    assert refused.active_run_count >= 1
    async with database.admin_transaction() as session:
        await session.execute(
            update(Run)
            .where(Run.id == accepted.json()["id"])
            .values(status="cancelled", revision=Run.revision + 1)
        )


@pytest.mark.asyncio
async def test_runtime_role_cannot_inspect_or_control_global_maintenance(app_client) -> None:
    app, _client = app_client
    database = app.state.database
    with pytest.raises(ProgrammingError):
        async with database.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE nico_runtime"))
            await session.execute(text("SELECT * FROM deployment_maintenance"))

    with pytest.raises(ProgrammingError):
        async with database.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE nico_runtime"))
            await session.execute(
                text("SELECT acquire_runtime_maintenance(:attempt_id, :token, 120)"),
                {"attempt_id": uuid4(), "token": "z" * 40},
            )
