from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.api import create_app
from nico_agent.config import Settings
from nico_agent.database import Database
from nico_agent.domain.models import ProjectSupervisionCycle, Run
from nico_agent.projects.worker import ProjectSupervisionWorker

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


async def _bootstrap(client: AsyncClient, label: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/tenants/bootstrap",
        json={"name": label, "slug": f"{label.lower()}-{uuid4()}"},
        headers={"X-Actor-ID": "project-bootstrap"},
    )
    assert response.status_code == 201, response.text
    tenant = response.json()
    headers = {
        "X-Tenant-ID": tenant["id"],
        "X-Actor-ID": "project-operator",
    }
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
    lead_capable: bool = False,
    runtime_provider: str = "nico_native",
) -> dict:
    created = await client.post(
        "/api/v1/agents",
        json={
            "name": f"{label}-{uuid4().hex[:8]}",
            "display_name": label.title(),
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    agent = created.json()
    version = await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json={
            "role": "project lead" if lead_capable else "project member",
            "mandate": "Coordinate bounded project work" if lead_capable else "Complete work",
            "runtime_provider": runtime_provider,
            "execution_mode": "plan_and_execute" if lead_capable else "direct",
            "coordination_policy": {
                "enabled": lead_capable,
                "allowed_target_scopes": ["project_members"] if lead_capable else [],
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


async def _create_project(
    client: AsyncClient,
    headers: dict[str, str],
    lead: dict,
    members: list[dict],
    *,
    key: str,
    name: str | None = None,
):
    return await client.post(
        "/api/v1/projects/collaboration",
        json={
            "name": name or f"project-{key}",
            "description": "Project collaboration API test",
            "goal": "Deliver a verified result",
            "acceptance": {"tests": "passing"},
            "lead_agent_id": lead["id"],
            "member_agent_ids": [member["id"] for member in members],
            "supervision_cadence_seconds": 3600,
        },
        headers={**headers, "Idempotency-Key": key},
    )


@pytest.mark.asyncio
async def test_create_project_is_atomic_idempotent_and_returns_stable_sessions(
    client: AsyncClient,
) -> None:
    headers = await _bootstrap(client, "CollaborationCreate")
    lead = await _ready_agent(client, headers, "lead", lead_capable=True)
    first = await _ready_agent(client, headers, "first")
    second = await _ready_agent(client, headers, "second")

    created = await _create_project(client, headers, lead, [first, second], key="project-create")
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["project"]["kind"] == "shared"
    assert payload["project"]["supervision_cadence_seconds"] == 3600
    assert [item["role"] for item in payload["members"]].count("lead") == 1
    assert len(payload["members"]) == 3
    assert len(payload["sessions"]) == 3
    assert all(item["current_conversation_id"] for item in payload["sessions"])

    replay = await _create_project(client, headers, lead, [first, second], key="project-create")
    assert replay.status_code == 201, replay.text
    assert replay.json()["project"]["id"] == payload["project"]["id"]

    failed_name = f"failed-{uuid4().hex[:8]}"
    failed = await client.post(
        "/api/v1/projects/collaboration",
        json={
            "name": failed_name,
            "goal": "Must roll back",
            "lead_agent_id": lead["id"],
            "member_agent_ids": [str(uuid4())],
        },
        headers={**headers, "Idempotency-Key": "project-failed"},
    )
    assert failed.status_code == 404
    projects = (await client.get("/api/v1/projects", headers=headers)).json()
    assert failed_name not in {item["name"] for item in projects}

    audit = (await client.get("/api/v1/audit", headers=headers)).json()
    assert any(item["action"] == "project.collaboration.create" for item in audit)
    assert any(item["action"] == "project_session.create" for item in audit)


@pytest.mark.asyncio
async def test_concurrent_project_create_replays_instead_of_leaking_unique_conflict(
    client: AsyncClient,
) -> None:
    headers = await _bootstrap(client, "CollaborationConcurrentCreate")
    lead = await _ready_agent(client, headers, "lead", lead_capable=True)

    first, second = await asyncio.gather(
        _create_project(client, headers, lead, [], key="concurrent-project-create"),
        _create_project(client, headers, lead, [], key="concurrent-project-create"),
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["project"]["id"] == second.json()["project"]["id"]


@pytest.mark.asyncio
async def test_managed_project_enforces_membership_and_archival(client: AsyncClient) -> None:
    headers = await _bootstrap(client, "CollaborationMembership")
    lead = await _ready_agent(client, headers, "lead", lead_capable=True)
    member = await _ready_agent(client, headers, "member")
    outsider = await _ready_agent(client, headers, "outsider")
    created = (await _create_project(client, headers, lead, [member], key="managed-project")).json()
    project = created["project"]

    denied = await client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Outsider work",
            "assignee_agent_id": outsider["id"],
        },
        headers=headers,
    )
    assert denied.status_code == 409, denied.text
    assert denied.json()["code"] == "PROJECT_MEMBER_REQUIRED"
    accepted = await client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Member work",
            "assignee_agent_id": member["id"],
        },
        headers=headers,
    )
    assert accepted.status_code == 201, accepted.text

    archived = await client.post(
        f"/api/v1/projects/{project['id']}/archive",
        json={"expected_revision": project["revision"]},
        headers=headers,
    )
    assert archived.status_code == 200, archived.text
    sessions = (
        await client.get(f"/api/v1/projects/{project['id']}/sessions", headers=headers)
    ).json()
    assert {item["status"] for item in sessions} == {"archived"}
    after_archive = await client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Too late",
            "assignee_agent_id": member["id"],
        },
        headers=headers,
    )
    assert after_archive.status_code == 409
    assert after_archive.json()["code"] == "PROJECT_ARCHIVED"


@pytest.mark.asyncio
async def test_replace_lead_and_pause_restore_member_are_revision_safe(
    client: AsyncClient,
) -> None:
    headers = await _bootstrap(client, "CollaborationLifecycle")
    lead = await _ready_agent(client, headers, "lead", lead_capable=True)
    replacement = await _ready_agent(client, headers, "replacement", lead_capable=True)
    created = (
        await _create_project(client, headers, lead, [replacement], key="lifecycle-project")
    ).json()
    project = created["project"]
    replacement_member = next(
        item for item in created["members"] if item["agent_id"] == replacement["id"]
    )

    replaced = await client.post(
        f"/api/v1/projects/{project['id']}/lead",
        json={
            "new_lead_agent_id": replacement["id"],
            "expected_project_revision": project["revision"],
        },
        headers={**headers, "Idempotency-Key": "replace-lead"},
    )
    assert replaced.status_code == 200, replaced.text
    replaced_payload = replaced.json()
    assert replaced_payload["lead_agent_id"] == replacement["id"]

    replace_replay = await client.post(
        f"/api/v1/projects/{project['id']}/lead",
        json={
            "new_lead_agent_id": replacement["id"],
            "expected_project_revision": project["revision"],
        },
        headers={**headers, "Idempotency-Key": "replace-lead"},
    )
    assert replace_replay.status_code == 200, replace_replay.text
    assert replace_replay.json()["lead_agent_id"] == replacement["id"]

    replace_mismatch = await client.post(
        f"/api/v1/projects/{project['id']}/lead",
        json={
            "new_lead_agent_id": lead["id"],
            "expected_project_revision": project["revision"],
        },
        headers={**headers, "Idempotency-Key": "replace-lead"},
    )
    assert replace_mismatch.status_code == 409
    assert replace_mismatch.json()["code"] == "IDEMPOTENCY_CONFLICT"

    stale = await client.post(
        f"/api/v1/projects/{project['id']}/lead",
        json={
            "new_lead_agent_id": lead["id"],
            "expected_project_revision": project["revision"],
        },
        headers={**headers, "Idempotency-Key": "stale-lead"},
    )
    assert stale.status_code == 409

    current_project = (
        await client.get(f"/api/v1/projects/{project['id']}", headers=headers)
    ).json()
    paused = await client.post(
        f"/api/v1/projects/{project['id']}/members/{lead['id']}/state",
        json={
            "target": "paused",
            "expected_project_revision": current_project["revision"],
            "expected_member_revision": next(
                item["revision"]
                for item in replaced_payload["members"]
                if item["agent_id"] == lead["id"]
            ),
            "reason": "Focus elsewhere",
        },
        headers={**headers, "Idempotency-Key": "pause-old-lead"},
    )
    assert paused.status_code == 200, paused.text
    stable_session_id = paused.json()["session"]["id"]
    pause_replay = await client.post(
        f"/api/v1/projects/{project['id']}/members/{lead['id']}/state",
        json={
            "target": "paused",
            "expected_project_revision": current_project["revision"],
            "expected_member_revision": next(
                item["revision"]
                for item in replaced_payload["members"]
                if item["agent_id"] == lead["id"]
            ),
            "reason": "Focus elsewhere",
        },
        headers={**headers, "Idempotency-Key": "pause-old-lead"},
    )
    assert pause_replay.status_code == 200, pause_replay.text
    assert pause_replay.json()["session"]["id"] == stable_session_id

    pause_mismatch = await client.post(
        f"/api/v1/projects/{project['id']}/members/{lead['id']}/state",
        json={
            "target": "removed",
            "expected_project_revision": current_project["revision"],
            "expected_member_revision": next(
                item["revision"]
                for item in replaced_payload["members"]
                if item["agent_id"] == lead["id"]
            ),
            "reason": "Different request",
        },
        headers={**headers, "Idempotency-Key": "pause-old-lead"},
    )
    assert pause_mismatch.status_code == 409
    assert pause_mismatch.json()["code"] == "IDEMPOTENCY_CONFLICT"
    restored_project = (
        await client.get(f"/api/v1/projects/{project['id']}", headers=headers)
    ).json()
    restored = await client.post(
        f"/api/v1/projects/{project['id']}/members/{lead['id']}/state",
        json={
            "target": "active",
            "expected_project_revision": restored_project["revision"],
            "expected_member_revision": paused.json()["member"]["revision"],
            "reason": "Available again",
        },
        headers={**headers, "Idempotency-Key": "restore-old-lead"},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["session"]["id"] == stable_session_id
    assert replacement_member["id"] != restored.json()["member"]["id"]

    remove_project = (await client.get(f"/api/v1/projects/{project['id']}", headers=headers)).json()
    removed = await client.post(
        f"/api/v1/projects/{project['id']}/members/{lead['id']}/state",
        json={
            "target": "removed",
            "expected_project_revision": remove_project["revision"],
            "expected_member_revision": restored.json()["member"]["revision"],
            "reason": "Temporary reassignment",
        },
        headers={**headers, "Idempotency-Key": "remove-old-lead"},
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["session"]["status"] == "paused"
    restore_project = (
        await client.get(f"/api/v1/projects/{project['id']}", headers=headers)
    ).json()
    restored_after_remove = await client.post(
        f"/api/v1/projects/{project['id']}/members/{lead['id']}/state",
        json={
            "target": "active",
            "expected_project_revision": restore_project["revision"],
            "expected_member_revision": removed.json()["member"]["revision"],
            "reason": "Reassigned back",
        },
        headers={**headers, "Idempotency-Key": "restore-removed-member"},
    )
    assert restored_after_remove.status_code == 200, restored_after_remove.text
    assert restored_after_remove.json()["session"]["id"] == stable_session_id
    assert restored_after_remove.json()["session"]["status"] == "active"

    reject_project = (await client.get(f"/api/v1/projects/{project['id']}", headers=headers)).json()
    current_lead = next(
        item for item in replaced_payload["members"] if item["agent_id"] == replacement["id"]
    )
    reject_lead_removal = await client.post(
        f"/api/v1/projects/{project['id']}/members/{replacement['id']}/state",
        json={
            "target": "removed",
            "expected_project_revision": reject_project["revision"],
            "expected_member_revision": current_lead["revision"],
            "reason": "Must replace the Lead first",
        },
        headers={**headers, "Idempotency-Key": "remove-current-lead"},
    )
    assert reject_lead_removal.status_code == 409
    assert reject_lead_removal.json()["code"] == "PROJECT_LEAD_REQUIRED"


@pytest.mark.asyncio
async def test_member_add_replays_before_revision_check_and_rejects_key_reuse(
    client: AsyncClient,
) -> None:
    headers = await _bootstrap(client, "CollaborationMemberReplay")
    lead = await _ready_agent(client, headers, "lead", lead_capable=True)
    first = await _ready_agent(client, headers, "first")
    second = await _ready_agent(client, headers, "second")
    created = (await _create_project(client, headers, lead, [], key="member-replay-project")).json()
    project = created["project"]
    request = {
        "agent_id": first["id"],
        "expected_project_revision": project["revision"],
    }
    request_headers = {**headers, "Idempotency-Key": "member-add-replay"}

    added = await client.post(
        f"/api/v1/projects/{project['id']}/members",
        json=request,
        headers=request_headers,
    )
    assert added.status_code == 201, added.text
    replay = await client.post(
        f"/api/v1/projects/{project['id']}/members",
        json=request,
        headers=request_headers,
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["member"]["id"] == added.json()["member"]["id"]

    mismatch = await client.post(
        f"/api/v1/projects/{project['id']}/members",
        json={**request, "agent_id": second["id"]},
        headers=request_headers,
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_lead_preflight_rejects_hermes_without_writing_project(client: AsyncClient) -> None:
    headers = await _bootstrap(client, "CollaborationPreflight")
    hermes = await _ready_agent(
        client,
        headers,
        "hermes-lead",
        lead_capable=True,
        runtime_provider="hermes_cli",
    )
    preflight = await client.post(
        "/api/v1/projects/collaboration/preflight",
        json={"lead_agent_id": hermes["id"], "member_agent_ids": []},
        headers=headers,
    )
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["compatible"] is False
    assert "native runtime" in " ".join(preflight.json()["issues"]).lower()
    rejected = await _create_project(client, headers, hermes, [], key="hermes-project")
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "PROJECT_LEAD_INCOMPATIBLE"


@pytest.mark.asyncio
async def test_supervision_api_replays_updates_cadence_and_archive_cancels_run_tree(
    client: AsyncClient,
) -> None:
    headers = await _bootstrap(client, "CollaborationSupervision")
    lead = await _ready_agent(client, headers, "lead", lead_capable=True)
    member = await _ready_agent(client, headers, "member")
    created = (
        await _create_project(client, headers, lead, [member], key="supervision-project")
    ).json()
    project = created["project"]

    sync_headers = {**headers, "Idempotency-Key": "manual-sync"}
    first = await client.post(
        f"/api/v1/projects/{project['id']}/supervision/sync",
        headers=sync_headers,
    )
    assert first.status_code == 202, first.text
    replay = await client.post(
        f"/api/v1/projects/{project['id']}/supervision/sync",
        headers=sync_headers,
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == first.json()["id"]

    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        assert await ProjectSupervisionWorker(
            database, worker_id="api-supervision-worker"
        ).execute_once()
        cycle_id = first.json()["id"]
        cycle = await client.get(
            f"/api/v1/projects/{project['id']}/supervision/cycles/{cycle_id}",
            headers=headers,
        )
        assert cycle.status_code == 200, cycle.text
        assert cycle.json()["status"] == "running"

        cadence = await client.patch(
            f"/api/v1/projects/{project['id']}/supervision/cadence",
            json={
                "cadence_seconds": None,
                "expected_project_revision": project["revision"],
                "reason": "manual-only test",
            },
            headers=headers,
        )
        assert cadence.status_code == 200, cadence.text
        assert cadence.json()["supervision_cadence_seconds"] is None
        assert cadence.json()["next_supervision_at"] is None

        archived = await client.post(
            f"/api/v1/projects/{project['id']}/archive",
            json={"expected_revision": cadence.json()["revision"]},
            headers=headers,
        )
        assert archived.status_code == 200, archived.text
        after = await client.get(
            f"/api/v1/projects/{project['id']}/supervision/cycles/{cycle_id}",
            headers=headers,
        )
        assert after.status_code == 200, after.text
        assert after.json()["status"] == "cancelled"
        async with database.admin_transaction() as session:
            stored_cycle = await session.scalar(
                select(ProjectSupervisionCycle).where(ProjectSupervisionCycle.id == cycle_id)
            )
            assert stored_cycle is not None and stored_cycle.run_id is not None
            run = await session.get(Run, stored_cycle.run_id)
            assert run is not None and run.status == "cancelled"
    finally:
        await engine.dispose()
