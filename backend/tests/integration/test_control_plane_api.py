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


async def bootstrap(client: AsyncClient, label: str) -> tuple[str, dict[str, str]]:
    response = await client.post(
        "/api/v1/tenants/bootstrap",
        json={"name": label, "slug": f"{label.lower()}-{uuid4()}"},
        headers={"X-Actor-ID": "integration-bootstrap"},
    )
    assert response.status_code == 201, response.text
    tenant_id = response.json()["id"]
    return tenant_id, {"X-Tenant-ID": tenant_id, "X-Actor-ID": "integration-user"}


async def create_ready_agent(client: AsyncClient, headers: dict[str, str], suffix: str) -> dict:
    agent_response = await client.post(
        "/api/v1/agents",
        json={
            "name": f"researcher-{suffix}",
            "display_name": f"Researcher {suffix}",
            "description": "general-purpose research agent",
        },
        headers=headers,
    )
    assert agent_response.status_code == 201, agent_response.text
    agent = agent_response.json()
    version_response = await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json={
            "role": "researcher",
            "mandate": "Collect evidence and return structured findings",
            "boundaries": ["no external mutation"],
            "model_config": {"provider": "future-runtime"},
        },
        headers=headers,
    )
    assert version_response.status_code == 201, version_response.text
    version = version_response.json()
    assert version["model_config"] == {"provider": "future-runtime"}
    publish_response = await client.post(
        f"/api/v1/agents/{agent['id']}/versions/{version['id']}/publish",
        json={"expected_revision": agent["revision"]},
        headers=headers,
    )
    assert publish_response.status_code == 200, publish_response.text
    return publish_response.json()


@pytest.mark.asyncio
async def test_agent_version_clone_archive_restore_and_delete(client: AsyncClient) -> None:
    _, headers = await bootstrap(client, "Lifecycle")
    agent = await create_ready_agent(client, headers, uuid4().hex[:8])

    second_version_response = await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json={"role": "reviewer", "mandate": "Review evidence"},
        headers=headers,
    )
    second_version = second_version_response.json()
    publish_second = await client.post(
        f"/api/v1/agents/{agent['id']}/versions/{second_version['id']}/publish",
        json={"expected_revision": agent["revision"]},
        headers=headers,
    )
    assert publish_second.status_code == 200
    agent = publish_second.json()

    versions = (await client.get(f"/api/v1/agents/{agent['id']}/versions", headers=headers)).json()
    first_version = versions[0]
    rollback = await client.post(
        f"/api/v1/agents/{agent['id']}/rollback",
        json={"expected_revision": agent["revision"], "version_id": first_version["id"]},
        headers=headers,
    )
    assert rollback.status_code == 200, rollback.text
    agent = rollback.json()
    assert agent["current_version_id"] == first_version["id"]

    clone_response = await client.post(
        f"/api/v1/agents/{agent['id']}/clone",
        json={"name": f"clone-{uuid4().hex[:8]}", "display_name": "Cloned researcher"},
        headers=headers,
    )
    assert clone_response.status_code == 201
    clone = clone_response.json()
    clone_versions = (
        await client.get(f"/api/v1/agents/{clone['id']}/versions", headers=headers)
    ).json()
    assert len(clone_versions) == 1
    assert clone_versions[0]["status"] == "draft"

    publish_clone = await client.post(
        f"/api/v1/agents/{clone['id']}/versions/{clone_versions[0]['id']}/publish",
        json={"expected_revision": clone["revision"]},
        headers=headers,
    )
    clone = publish_clone.json()
    archived = await client.post(
        f"/api/v1/agents/{clone['id']}/archive",
        json={"expected_revision": clone["revision"]},
        headers=headers,
    )
    assert archived.json()["status"] == "archived"
    restored = await client.post(
        f"/api/v1/agents/{clone['id']}/restore",
        json={"expected_revision": archived.json()["revision"]},
        headers=headers,
    )
    assert restored.json()["status"] == "ready"

    draft_response = await client.post(
        "/api/v1/agents",
        json={
            "name": f"delete-{uuid4().hex[:8]}",
            "display_name": "Disposable draft",
        },
        headers=headers,
    )
    draft = draft_response.json()
    deleted = await client.delete(
        f"/api/v1/agents/{draft['id']}?expected_revision=1", headers=headers
    )
    assert deleted.status_code == 204
    assert (await client.get(f"/api/v1/agents/{draft['id']}", headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_task_multiple_runs_steps_events_and_audit(client: AsyncClient) -> None:
    _, headers = await bootstrap(client, "Execution")
    project_response = await client.post(
        "/api/v1/projects",
        json={"name": f"project-{uuid4().hex[:8]}", "metadata": {"purpose": "test"}},
        headers=headers,
    )
    assert project_response.status_code == 201
    project = project_response.json()
    agent = await create_ready_agent(client, headers, uuid4().hex[:8])
    task_response = await client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Produce a structured report",
            "input": {"topic": "tenant isolation"},
            "acceptance": {"format": "json"},
            "assignee_agent_id": agent["id"],
        },
        headers=headers,
    )
    assert task_response.status_code == 201
    task = task_response.json()
    assert task["status"] == "assigned"

    first_run_response = await client.post(
        f"/api/v1/tasks/{task['id']}/runs",
        json={"max_steps": 5, "token_budget": 1000, "timeout_seconds": 60},
        headers=headers,
    )
    assert first_run_response.status_code == 201, first_run_response.text
    first_run = first_run_response.json()
    assert first_run["attempt"] == 1
    planning = await client.post(
        f"/api/v1/runs/{first_run['id']}/transition",
        json={"target": "planning", "expected_revision": first_run["revision"]},
        headers=headers,
    )
    running = await client.post(
        f"/api/v1/runs/{first_run['id']}/transition",
        json={"target": "running", "expected_revision": planning.json()["revision"]},
        headers=headers,
    )
    step_response = await client.post(
        f"/api/v1/runs/{first_run['id']}/steps",
        json={"sequence": 1, "kind": "reasoning", "input": {"question": "why"}},
        headers=headers,
    )
    assert step_response.status_code == 201
    step = step_response.json()
    step_running = await client.post(
        f"/api/v1/runs/{first_run['id']}/steps/{step['id']}/transition",
        json={"target": "running", "expected_revision": step["revision"]},
        headers=headers,
    )
    step_completed = await client.post(
        f"/api/v1/runs/{first_run['id']}/steps/{step['id']}/transition",
        json={
            "target": "completed",
            "expected_revision": step_running.json()["revision"],
            "output": {"answer": "RLS plus application context"},
        },
        headers=headers,
    )
    assert step_completed.json()["status"] == "completed"
    failed = await client.post(
        f"/api/v1/runs/{first_run['id']}/transition",
        json={
            "target": "failed",
            "expected_revision": running.json()["revision"],
            "error": {"code": "PROVIDER_UNAVAILABLE"},
        },
        headers=headers,
    )
    assert failed.json()["status"] == "failed"

    retry_response = await client.post(
        f"/api/v1/runs/{first_run['id']}/retry",
        json={"max_steps": 8, "token_budget": 1500},
        headers=headers,
    )
    assert retry_response.status_code == 201, retry_response.text
    retry = retry_response.json()
    assert retry["attempt"] == 2
    assert retry["retry_of_run_id"] == first_run["id"]
    retry_planning = await client.post(
        f"/api/v1/runs/{retry['id']}/transition",
        json={"target": "planning", "expected_revision": retry["revision"]},
        headers=headers,
    )
    retry_running = await client.post(
        f"/api/v1/runs/{retry['id']}/transition",
        json={"target": "running", "expected_revision": retry_planning.json()["revision"]},
        headers=headers,
    )
    completed = await client.post(
        f"/api/v1/runs/{retry['id']}/transition",
        json={
            "target": "completed",
            "expected_revision": retry_running.json()["revision"],
            "result": {"report": "complete"},
        },
        headers=headers,
    )
    assert completed.json()["status"] == "completed"
    task_after = (await client.get(f"/api/v1/tasks/{task['id']}", headers=headers)).json()
    assert task_after["status"] == "waiting_for_review"
    approved = await client.post(
        f"/api/v1/tasks/{task['id']}/transition",
        json={"target": "completed", "expected_revision": task_after["revision"]},
        headers=headers,
    )
    assert approved.json()["status"] == "completed"

    events_response = await client.get(f"/api/v1/runs/{first_run['id']}/events", headers=headers)
    events = events_response.json()
    assert [item["sequence"] for item in events] == sorted(item["sequence"] for item in events)
    assert {item["event_type"] for item in events} >= {
        "RunCreated",
        "RunPlanningStarted",
        "RunStarted",
        "RunStepCreated",
        "StepStarted",
        "StepCompleted",
        "RunFailed",
    }
    audit = (await client.get("/api/v1/audit?limit=100", headers=headers)).json()
    assert {item["action"] for item in audit} >= {
        "task.create",
        "run.create",
        "run.retry",
        "run_step.create",
    }


@pytest.mark.asyncio
async def test_invalid_transitions_and_revisions_are_stable_and_atomic(
    client: AsyncClient,
) -> None:
    _, headers = await bootstrap(client, "Errors")
    project = (
        await client.post(
            "/api/v1/projects",
            json={"name": f"errors-{uuid4().hex[:8]}"},
            headers=headers,
        )
    ).json()
    draft = (
        await client.post(
            "/api/v1/agents",
            json={"name": f"draft-{uuid4().hex[:8]}", "display_name": "Draft"},
            headers=headers,
        )
    ).json()
    audit_before = len((await client.get("/api/v1/audit", headers=headers)).json())
    revision_conflict = await client.patch(
        f"/api/v1/agents/{draft['id']}",
        json={"expected_revision": 99, "display_name": "Should not persist"},
        headers=headers,
    )
    assert revision_conflict.status_code == 409
    assert revision_conflict.json()["code"] == "REVISION_CONFLICT"
    unchanged = (await client.get(f"/api/v1/agents/{draft['id']}", headers=headers)).json()
    assert unchanged["display_name"] == "Draft"
    assert len((await client.get("/api/v1/audit", headers=headers)).json()) == audit_before

    task = (
        await client.post(
            "/api/v1/tasks",
            json={"project_id": project["id"], "title": "Unassigned"},
            headers=headers,
        )
    ).json()
    invalid = await client.post(
        f"/api/v1/tasks/{task['id']}/transition",
        json={"target": "completed", "expected_revision": task["revision"]},
        headers=headers,
    )
    assert invalid.status_code == 409
    assert invalid.json()["code"] == "INVALID_STATE_TRANSITION"
    not_runnable = await client.post(f"/api/v1/tasks/{task['id']}/runs", json={}, headers=headers)
    assert not_runnable.status_code == 400
    assert not_runnable.json()["code"] == "TASK_NOT_RUNNABLE"

    archived = await client.post(
        f"/api/v1/agents/{draft['id']}/archive",
        json={"expected_revision": draft["revision"]},
        headers=headers,
    )
    restore_without_version = await client.post(
        f"/api/v1/agents/{draft['id']}/restore",
        json={"expected_revision": archived.json()["revision"]},
        headers=headers,
    )
    assert restore_without_version.status_code == 400
    assert restore_without_version.json()["code"] == "AGENT_VERSION_REQUIRED"


@pytest.mark.asyncio
async def test_api_tenant_context_cannot_read_or_link_another_tenant(client: AsyncClient) -> None:
    _, headers_a = await bootstrap(client, "IsolationA")
    _, headers_b = await bootstrap(client, "IsolationB")
    project_a = (
        await client.post(
            "/api/v1/projects",
            json={"name": f"private-{uuid4().hex[:8]}"},
            headers=headers_a,
        )
    ).json()
    agent_a = await create_ready_agent(client, headers_a, uuid4().hex[:8])

    assert (await client.get("/api/v1/agents", headers=headers_b)).json() == []
    assert (
        await client.get(f"/api/v1/agents/{agent_a['id']}", headers=headers_b)
    ).status_code == 404
    cross_tenant_task = await client.post(
        "/api/v1/tasks",
        json={"project_id": project_a["id"], "title": "must fail"},
        headers=headers_b,
    )
    assert cross_tenant_task.status_code == 404
    assert cross_tenant_task.json()["code"] == "RESOURCE_NOT_FOUND"
    assert (await client.get("/api/v1/agents")).status_code == 422
