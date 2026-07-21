from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from nico_agent.api import create_app
from nico_agent.config import Settings

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with the Compose dependencies running",
)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app(settings=Settings(environment="test", _env_file=None))
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as value:
            yield value


async def _bootstrap(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/tenants/bootstrap",
        json={"name": "Dual Mode E2E", "slug": f"dual-mode-{uuid4()}"},
        headers={"X-Actor-ID": "dual-mode-bootstrap"},
    )
    assert response.status_code == 201, response.text
    tenant = response.json()
    headers = {"X-Tenant-ID": tenant["id"], "X-Actor-ID": "dual-mode-operator"}
    updated = await client.patch(
        "/api/v1/tenant/settings",
        json={
            "expected_revision": tenant["revision"],
            "settings": {
                "coordination_policy": {
                    "enabled": True,
                    "allowed_target_scopes": ["project_members"],
                    "allowed_agent_version_ids": [],
                    "allowed_secret_refs": [],
                    "max_depth": 3,
                    "max_children": 8,
                    "max_parallelism": 4,
                }
            },
        },
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    return headers


async def _ready_agent(
    client: AsyncClient,
    headers: dict[str, str],
    name: str,
    *,
    lead: bool,
) -> dict:
    created = await client.post(
        "/api/v1/agents",
        json={"name": f"{name}-{uuid4().hex[:8]}", "display_name": name.title()},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    agent = created.json()
    version = await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json={
            "role": "project lead" if lead else "project member",
            "mandate": "Coordinate bounded work" if lead else "Complete assigned work",
            "runtime_provider": "nico_native",
            "execution_mode": "plan_and_execute" if lead else "react",
            "coordination_policy": {
                "enabled": lead,
                "allowed_target_scopes": ["project_members"] if lead else [],
                "allowed_agent_version_ids": [],
                "allowed_secret_refs": [],
                "max_depth": 3,
                "max_children": 8,
                "max_parallelism": 4,
            },
        },
        headers=headers,
    )
    assert version.status_code == 201, version.text
    published = await client.post(
        f"/api/v1/agents/{agent['id']}/versions/{version.json()['id']}/publish",
        json={"expected_revision": agent["revision"]},
        headers=headers,
    )
    assert published.status_code == 200, published.text
    return published.json()


@pytest.mark.asyncio
async def test_personal_and_project_session_lifecycles_share_execution_primitives(
    client: AsyncClient,
) -> None:
    headers = await _bootstrap(client)
    lead = await _ready_agent(client, headers, "lead", lead=True)
    member = await _ready_agent(client, headers, "member", lead=False)

    personal = await client.post(
        "/api/v1/conversations",
        json={"mode": "personal", "agent_id": member["id"], "title": "Private work"},
        headers={**headers, "Idempotency-Key": "personal-session"},
    )
    assert personal.status_code == 201, personal.text
    assert personal.json()["mode"] == "personal"
    visible_projects = (await client.get("/api/v1/projects", headers=headers)).json()
    assert personal.json()["project_id"] not in {item["id"] for item in visible_projects}

    created = await client.post(
        "/api/v1/projects/collaboration",
        json={
            "name": f"release-{uuid4().hex[:8]}",
            "goal": "Ship a verified release",
            "acceptance": {"tests": "passing"},
            "lead_agent_id": lead["id"],
            "member_agent_ids": [member["id"]],
            "supervision_cadence_seconds": 3600,
        },
        headers={**headers, "Idempotency-Key": "shared-project"},
    )
    assert created.status_code == 201, created.text
    project = created.json()["project"]
    member_session = next(
        item for item in created.json()["sessions"] if item["agent_id"] == member["id"]
    )

    opened = await client.post(
        f"/api/v1/projects/{project['id']}/sessions/{member_session['id']}/open",
        headers=headers,
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["id"] == member_session["current_conversation_id"]
    assert opened.json()["project_id"] == project["id"]

    turn = await client.post(
        f"/api/v1/projects/{project['id']}/sessions/{member_session['id']}/messages",
        json={"user_input": "Record one auditable project step"},
        headers={**headers, "Idempotency-Key": "member-message"},
    )
    assert turn.status_code == 202, turn.text
    timeline = await client.get(
        f"/api/v1/projects/{project['id']}/sessions/{member_session['id']}/timeline",
        headers=headers,
    )
    assert timeline.status_code == 200, timeline.text
    assert {item["kind"] for item in timeline.json()["entries"]} >= {"task", "run"}
    assert all("reasoning" not in item["facts"] for item in timeline.json()["entries"])

    sync = await client.post(
        f"/api/v1/projects/{project['id']}/supervision/sync",
        headers={**headers, "Idempotency-Key": "manual-sync"},
    )
    assert sync.status_code == 202, sync.text
    cycles = await client.get(
        f"/api/v1/projects/{project['id']}/supervision/cycles", headers=headers
    )
    assert cycles.status_code == 200, cycles.text
    assert sync.json()["id"] in {item["id"] for item in cycles.json()}

    project_change = await client.post(
        f"/api/v1/projects/{project['id']}/sessions/{member_session['id']}/project-changes",
        json={"content": "Reduce scope to the verified API surface"},
        headers={**headers, "Idempotency-Key": "scope-change"},
    )
    assert project_change.status_code == 202, project_change.text
    assert project_change.json()["run_id"]
