from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from nico_agent.api import create_app
from nico_agent.config import Settings
from nico_agent.database import TenantContext
from nico_agent.domain.models import Event

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with the Compose dependencies running",
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


async def _bootstrap(client: AsyncClient, label: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/tenants/bootstrap",
        json={"name": label, "slug": f"{label.lower()}-{uuid4()}"},
        headers={"X-Actor-ID": "session-bootstrap"},
    )
    assert response.status_code == 201, response.text
    tenant = response.json()
    headers = {"X-Tenant-ID": tenant["id"], "X-Actor-ID": "session-operator"}
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
    label: str,
    *,
    lead: bool,
) -> tuple[dict, dict]:
    created = await client.post(
        "/api/v1/agents",
        json={"name": f"{label}-{uuid4().hex[:8]}", "display_name": label.title()},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    agent = created.json()
    version = await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json={
            "role": "lead" if lead else "member",
            "mandate": "Coordinate project work" if lead else "Complete assigned work",
            "runtime_provider": "nico_native",
            "execution_mode": "plan_and_execute" if lead else "direct",
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
    return published.json(), version.json()


async def _managed_project(
    client: AsyncClient,
    headers: dict[str, str],
    lead: dict,
    member: dict,
) -> dict:
    response = await client.post(
        "/api/v1/projects/collaboration",
        json={
            "name": f"session-project-{uuid4().hex[:8]}",
            "goal": "Provide a complete auditable result",
            "lead_agent_id": lead["id"],
            "member_agent_ids": [member["id"]],
            "supervision_cadence_seconds": 3600,
        },
        headers={**headers, "Idempotency-Key": "session-project"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _contains_key(value, forbidden: set[str]) -> bool:
    if isinstance(value, dict):
        return any(
            key in forbidden or _contains_key(item, forbidden)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_key(item, forbidden) for item in value)
    return False


@pytest.mark.asyncio
async def test_session_message_rotation_timeline_pagination_and_read_only_history(
    app_client,
) -> None:
    app, client = app_client
    headers = await _bootstrap(client, "ProjectSessionTimeline")
    lead, _ = await _ready_agent(client, headers, "timeline-lead", lead=True)
    member, first_version = await _ready_agent(
        client,
        headers,
        "timeline-member",
        lead=False,
    )
    collaboration = await _managed_project(client, headers, lead, member)
    project = collaboration["project"]
    member_session = next(
        item for item in collaboration["sessions"] if item["agent_id"] == member["id"]
    )
    session_id = member_session["id"]
    first_conversation_id = member_session["current_conversation_id"]

    accepted = await client.post(
        f"/api/v1/projects/{project['id']}/sessions/{session_id}/messages",
        json={"user_input": "Create a verifiable result"},
        headers={**headers, "Idempotency-Key": "session-turn-1"},
    )
    assert accepted.status_code == 202, accepted.text
    first_turn = accepted.json()
    task = (await client.get(f"/api/v1/tasks/{first_turn['task_id']}", headers=headers)).json()
    assert task["project_session_id"] == session_id
    assert (
        await client.post(
            f"/api/v1/conversation-turns/{first_turn['id']}/cancel",
            json={"expected_revision": first_turn["run_revision"]},
            headers=headers,
        )
    ).status_code == 200

    context = TenantContext(
        tenant_id=UUID(headers["X-Tenant-ID"]),
        actor_id=headers["X-Actor-ID"],
        correlation_id=uuid4(),
    )
    artifact_id = uuid4()
    async with app.state.database.tenant_transaction(context) as session:
        session.add(
            Event(
                tenant_id=context.tenant_id,
                event_type="ArtifactAvailable",
                aggregate_type="artifact",
                aggregate_id=artifact_id,
                run_id=UUID(first_turn["run_id"]),
                actor_id=context.actor_id,
                payload={
                    "name": "result.txt",
                    "status": "available",
                    "object_key": "private/storage/key",
                    "trajectory": [{"thought": "private"}],
                },
                correlation_id=context.correlation_id,
            )
        )

    latest_agent = (await client.get(f"/api/v1/agents/{member['id']}", headers=headers)).json()
    next_version = await client.post(
        f"/api/v1/agents/{member['id']}/versions",
        json={
            "role": "member-v2",
            "mandate": "Complete assigned work with updated instructions",
            "runtime_provider": "nico_native",
            "execution_mode": "direct",
        },
        headers=headers,
    )
    assert next_version.status_code == 201, next_version.text
    published = await client.post(
        f"/api/v1/agents/{member['id']}/versions/{next_version.json()['id']}/publish",
        json={"expected_revision": latest_agent["revision"]},
        headers=headers,
    )
    assert published.status_code == 200, published.text

    second = await client.post(
        f"/api/v1/projects/{project['id']}/sessions/{session_id}/messages",
        json={"user_input": "Use the new version"},
        headers={**headers, "Idempotency-Key": "session-turn-2"},
    )
    assert second.status_code == 202, second.text
    second_turn = second.json()
    second_run = (
        await client.get(f"/api/v1/runs/{second_turn['run_id']}", headers=headers)
    ).json()
    assert second_run["agent_version_id"] == next_version.json()["id"]
    assert second_run["agent_version_id"] != first_version["id"]
    current_session = (
        await client.get(
            f"/api/v1/projects/{project['id']}/sessions/{session_id}", headers=headers
        )
    ).json()
    assert current_session["current_conversation_id"] != first_conversation_id
    assert (
        await client.get(f"/api/v1/conversations/{first_conversation_id}", headers=headers)
    ).status_code == 200
    stale = await client.post(
        f"/api/v1/conversations/{first_conversation_id}/turns",
        json={"user_input": "Do not append here"},
        headers={**headers, "Idempotency-Key": "stale-turn"},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "PROJECT_SESSION_CONVERSATION_STALE"

    latest_second = (
        await client.get(
            f"/api/v1/conversation-turns/{second_turn['id']}",
            headers=headers,
        )
    ).json()
    if latest_second["run_status"] not in {"completed", "failed", "cancelled", "timed_out"}:
        cleanup = await client.post(
            f"/api/v1/conversation-turns/{second_turn['id']}/cancel",
            json={"expected_revision": latest_second["run_revision"]},
            headers=headers,
        )
        assert cleanup.status_code == 200, cleanup.text

    sequences: list[int] = []
    entries: list[dict] = []
    cursor = 0
    while True:
        page_response = await client.get(
            f"/api/v1/projects/{project['id']}/sessions/{session_id}/timeline",
            params={"after_sequence": cursor, "limit": 3},
            headers=headers,
        )
        assert page_response.status_code == 200, page_response.text
        page = page_response.json()
        entries.extend(page["entries"])
        sequences.extend(item["sequence"] for item in page["entries"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        assert page["next_cursor"] == page["entries"][-1]["sequence"]
        cursor = page["next_cursor"]
    assert sequences == sorted(set(sequences))
    assert {item["kind"] for item in entries} >= {"artifact", "conversation", "run", "task"}
    artifact = next(item for item in entries if item["resource_id"] == str(artifact_id))
    assert artifact["facts"] == {"name": "result.txt", "status": "available"}
    assert artifact["links"]["content"].endswith(f"/artifacts/{artifact_id}/content")
    assert not _contains_key(
        entries,
        {"trajectory", "raw_reasoning", "object_key", "arguments", "authorization"},
    )

    foreign_headers = await _bootstrap(client, "ProjectSessionForeign")
    assert (
        await client.get(
            f"/api/v1/projects/{project['id']}/sessions/{session_id}/timeline",
            headers=foreign_headers,
        )
    ).status_code == 404

    archived = await client.post(
        f"/api/v1/projects/{project['id']}/archive",
        json={"expected_revision": project["revision"]},
        headers=headers,
    )
    assert archived.status_code == 200, archived.text
    assert (
        await client.get(
            f"/api/v1/projects/{project['id']}/sessions/{session_id}/timeline",
            headers=headers,
        )
    ).status_code == 200
    denied = await client.post(
        f"/api/v1/projects/{project['id']}/sessions/{session_id}/messages",
        json={"user_input": "No writes after archive"},
        headers={**headers, "Idempotency-Key": "archived-session-turn"},
    )
    assert denied.status_code == 409
    assert denied.json()["code"] == "PROJECT_ARCHIVED"
