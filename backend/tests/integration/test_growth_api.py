import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from nico_agent.api import create_app
from nico_agent.config import Settings
from nico_agent.runtime import MockRuntimeProvider, RuntimeProviderRegistry
from nico_agent.runtime.executor import RuntimeWorker

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
            value.database = app.state.database  # type: ignore[attr-defined]
            yield value


async def _bootstrap(client: AsyncClient, label: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/tenants/bootstrap",
        json={"name": label, "slug": f"{label.lower()}-{uuid4()}"},
        headers={"X-Actor-ID": "integration-bootstrap"},
    )
    assert response.status_code == 201, response.text
    return {
        "X-Tenant-ID": response.json()["id"],
        "X-Actor-ID": "growth-requester",
    }


async def _ready_agent(client: AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    suffix = uuid4().hex[:8]
    response = await client.post(
        "/api/v1/agents",
        json={
            "name": f"growth-researcher-{suffix}",
            "display_name": "Growth Researcher",
            "description": "Produces reusable evidence-backed findings",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    agent = response.json()
    response = await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json={
            "role": "researcher",
            "mandate": "Research, verify, and summarize reusable operating knowledge",
            "boundaries": ["no external mutation"],
            "run_config": {
                "runtime_provider": "mock",
                "mock": {
                    "steps": [
                        "collect bounded evidence",
                        "verify the evidence and summarize the reusable procedure",
                    ],
                    "output": {
                        "finding": "Tenant-scoped evidence should be verified before reuse",
                        "procedure": "collect, verify, review, publish",
                    },
                },
            },
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    version = response.json()
    response = await client.post(
        f"/api/v1/agents/{agent['id']}/versions/{version['id']}/publish",
        json={"expected_revision": agent["revision"]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _terminal_run_and_candidates(
    client: AsyncClient, headers: dict[str, str]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    project_response = await client.post(
        "/api/v1/projects",
        json={"name": f"growth-project-{uuid4().hex[:8]}"},
        headers=headers,
    )
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()
    agent = await _ready_agent(client, headers)
    task_response = await client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Turn verified execution evidence into controlled knowledge",
            "input": {"topic": "growth lifecycle"},
            "acceptance": {"finding": "required"},
            "assignee_agent_id": agent["id"],
        },
        headers=headers,
    )
    assert task_response.status_code == 201, task_response.text
    run_response = await client.post(
        f"/api/v1/tasks/{task_response.json()['id']}/runs",
        json={"max_steps": 4},
        headers=headers,
    )
    assert run_response.status_code == 201, run_response.text
    run = run_response.json()

    worker = RuntimeWorker(
        client.database,  # type: ignore[attr-defined]
        RuntimeProviderRegistry([MockRuntimeProvider()]),
        worker_id=f"growth-api-{uuid4()}",
        lease_seconds=5,
        heartbeat_seconds=0.05,
    )
    completed = None
    for _ in range(20):
        assert await worker.execute_once() is True
        completed = await client.get(f"/api/v1/runs/{run['id']}", headers=headers)
        assert completed.status_code == 200, completed.text
        if completed.json()["status"] != "pending":
            break
    assert completed is not None
    assert completed.json()["status"] == "completed"

    generated = await client.post(
        f"/api/v1/runs/{run['id']}/growth-candidates",
        json={},
        headers=headers,
    )
    assert generated.status_code == 200, generated.text
    assert generated.json()["already_generated"] is False
    assert generated.json()["skill"] is not None
    return project, run, generated.json()


async def _approve(
    client: AsyncClient,
    headers: dict[str, str],
    subject_path: str,
    expected_revision: int,
) -> dict[str, Any]:
    evaluation = await client.post(f"{subject_path}/evaluations", headers=headers)
    assert evaluation.status_code == 200, evaluation.text
    assert evaluation.json()["verdict"] == "pass"
    request = await client.post(
        f"{subject_path}/approvals",
        json={"expected_revision": expected_revision},
        headers=headers,
    )
    assert request.status_code == 200, request.text

    self_review = await client.post(
        f"/api/v1/growth-approvals/{request.json()['id']}/decision",
        json={
            "decision": "approved",
            "reason": "The requester must not be allowed to self-review.",
            "expected_revision": request.json()["revision"],
        },
        headers=headers,
    )
    assert self_review.status_code == 403, self_review.text
    assert self_review.json()["code"] == "APPROVAL_SELF_REVIEW_FORBIDDEN"

    reviewer_headers = {**headers, "X-Actor-ID": "growth-independent-reviewer"}
    decision = await client.post(
        f"/api/v1/growth-approvals/{request.json()['id']}/decision",
        json={
            "decision": "approved",
            "reason": "Independent deterministic validation and provenance review passed.",
            "expected_revision": request.json()["revision"],
        },
        headers=reviewer_headers,
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["status"] == "approved"
    return decision.json()


@pytest.mark.asyncio
async def test_growth_http_lifecycle_is_tenant_safe_and_fully_releasable(
    client: AsyncClient,
) -> None:
    headers = await _bootstrap(client, "GrowthAPI")
    project, run, generated = await _terminal_run_and_candidates(client, headers)

    semantic_ref = next(item for item in generated["memories"] if item["memory_type"] == "semantic")
    memory_path = f"/api/v1/memories/{semantic_ref['id']}"
    memory_response = await client.get(memory_path, headers=headers)
    assert memory_response.status_code == 200, memory_response.text
    memory = memory_response.json()
    sources = await client.get(f"{memory_path}/sources", headers=headers)
    assert sources.status_code == 200, sources.text
    assert len(sources.json()) == 1
    assert "snapshot" not in sources.json()[0]

    premature = await client.post(
        f"{memory_path}/publish",
        json={"expected_revision": memory["revision"]},
        headers=headers,
    )
    assert premature.status_code == 400
    assert premature.json()["code"] == "GROWTH_EVALUATION_NOT_PASSED"

    await _approve(client, headers, memory_path, memory["revision"])
    published_response = await client.post(
        f"{memory_path}/publish",
        json={"expected_revision": memory["revision"]},
        headers=headers,
    )
    assert published_response.status_code == 200, published_response.text
    published = published_response.json()
    assert published["status"] == "active"
    assert published["indexed"] is True

    search = await client.post(
        "/api/v1/memories/search",
        json={
            "query": memory["content"],
            "project_id": project["id"],
            "minimum_similarity": 0,
        },
        headers=headers,
    )
    assert search.status_code == 200, search.text
    assert any(item["memory_id"] == memory["id"] for item in search.json())
    assert search.json()[0]["sources"][0]["run_id"] == run["id"]

    revised_response = await client.post(
        f"{memory_path}/revisions",
        json={
            "content": f"{memory['content']}\nReviewed operating note: preserve tenant scope.",
            "reason": "Clarify the tenant isolation requirement.",
            "expected_revision": published["revision"],
        },
        headers=headers,
    )
    assert revised_response.status_code == 201, revised_response.text
    revised = revised_response.json()
    assert revised["version"] == 2

    conflict = await client.post(
        f"{memory_path}/revisions",
        json={
            "content": "This stale edit must not win.",
            "reason": "Exercise optimistic concurrency.",
            "expected_revision": 1,
        },
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "REVISION_CONFLICT"

    revised_path = f"/api/v1/memories/{revised['id']}"
    await _approve(client, headers, revised_path, revised["revision"])
    revised_published_response = await client.post(
        f"{revised_path}/publish",
        json={"expected_revision": revised["revision"]},
        headers=headers,
    )
    assert revised_published_response.status_code == 200, revised_published_response.text
    assert revised_published_response.json()["status"] == "active"
    assert (await client.get(memory_path, headers=headers)).json()["status"] == "invalidated"

    procedural_ref = next(
        item for item in generated["memories"] if item["memory_type"] == "procedural"
    )
    procedural_path = f"/api/v1/memories/{procedural_ref['id']}"
    procedural = (await client.get(procedural_path, headers=headers)).json()
    await _approve(client, headers, procedural_path, procedural["revision"])
    procedural_publish = await client.post(
        f"{procedural_path}/publish",
        json={"expected_revision": procedural["revision"]},
        headers=headers,
    )
    assert procedural_publish.status_code == 200, procedural_publish.text
    invalidated_response = await client.post(
        f"{procedural_path}/invalidate",
        json={
            "expected_revision": procedural_publish.json()["revision"],
            "reason": "Exercise explicit removal from retrieval.",
        },
        headers=headers,
    )
    assert invalidated_response.status_code == 200, invalidated_response.text
    assert invalidated_response.json()["status"] == "invalidated"
    deleted_response = await client.delete(
        procedural_path,
        params={
            "expected_revision": invalidated_response.json()["revision"],
            "reason": "Exercise the auditable tombstone endpoint.",
        },
        headers=headers,
    )
    assert deleted_response.status_code == 200, deleted_response.text
    assert deleted_response.json()["status"] == "deleted"
    filtered_memories = await client.get(
        "/api/v1/memories",
        params={"status": "active", "memory_type": "semantic"},
        headers=headers,
    )
    assert filtered_memories.status_code == 200
    assert [item["id"] for item in filtered_memories.json()] == [revised["id"]]

    skill_ref = generated["skill"]
    skill_id = skill_ref["skill_id"]
    version_id = skill_ref["skill_version_id"]
    version_path = f"/api/v1/skills/{skill_id}/versions/{version_id}"
    skill = (await client.get(f"/api/v1/skills/{skill_id}", headers=headers)).json()
    version = (await client.get(version_path, headers=headers)).json()
    await _approve(client, headers, version_path, version["revision"])
    first_publish_response = await client.post(
        f"{version_path}/publish",
        json={
            "expected_skill_revision": skill["revision"],
            "expected_version_revision": version["revision"],
        },
        headers=headers,
    )
    assert first_publish_response.status_code == 200, first_publish_response.text
    first_publish = first_publish_response.json()
    assert first_publish["skill_status"] == "published"

    draft = {
        key: version[key]
        for key in (
            "conditions",
            "preconditions",
            "input_schema",
            "steps",
            "tools",
            "output_schema",
            "validation",
            "failure_modes",
        )
    }
    draft["steps"] = [
        *draft["steps"],
        {"id": "record-review", "action": "record_independent_review"},
    ]
    draft["validation"] = {**draft["validation"], "independent_review": True}
    revised_version_response = await client.post(
        f"{version_path}/revisions",
        json={
            "draft": draft,
            "reason": "Require an explicit independent review record.",
            "expected_skill_revision": first_publish["skill_revision"],
            "expected_version_revision": first_publish["version_revision"],
        },
        headers=headers,
    )
    assert revised_version_response.status_code == 201, revised_version_response.text
    second = revised_version_response.json()
    second_path = f"/api/v1/skills/{skill_id}/versions/{second['skill_version_id']}"
    comparison = await client.get(
        f"/api/v1/skills/{skill_id}/versions/compare",
        params={"from_version_id": version_id, "to_version_id": second["skill_version_id"]},
        headers=headers,
    )
    assert comparison.status_code == 200, comparison.text
    assert comparison.json()["direction"] == "upgrade"
    assert {"steps", "validation"} <= set(comparison.json()["changed_fields"])

    await _approve(client, headers, second_path, second["version_revision"])
    second_publish_response = await client.post(
        f"{second_path}/publish",
        json={
            "expected_skill_revision": second["skill_revision"],
            "expected_version_revision": second["version_revision"],
        },
        headers=headers,
    )
    assert second_publish_response.status_code == 200, second_publish_response.text
    second_publish = second_publish_response.json()

    canary_response = await client.post(
        f"/api/v1/skills/{skill_id}/deployments",
        json={
            "skill_version_id": second["skill_version_id"],
            "scope_type": "project",
            "scope_id": project["id"],
            "rollout_percentage": 50,
            "expected_skill_revision": second_publish["skill_revision"],
        },
        headers=headers,
    )
    assert canary_response.status_code == 200, canary_response.text
    canary = canary_response.json()
    resolved = await client.get(
        f"/api/v1/skills/{skill_id}/resolve",
        params={"run_id": run["id"]},
        headers=headers,
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["selection"] in {"stable", "canary"}

    promoted_response = await client.post(
        f"/api/v1/skills/{skill_id}/promote",
        json={
            "skill_version_id": second["skill_version_id"],
            "reason": "The canary evidence is accepted.",
            "expected_skill_revision": canary["skill_revision"],
        },
        headers=headers,
    )
    assert promoted_response.status_code == 200, promoted_response.text
    promoted = promoted_response.json()
    assert promoted["current_version_id"] == second["skill_version_id"]

    rollback_response = await client.post(
        f"/api/v1/skills/{skill_id}/rollback",
        json={
            "skill_version_id": version_id,
            "reason": "Exercise the verified rollback path.",
            "expected_skill_revision": promoted["skill_revision"],
        },
        headers=headers,
    )
    assert rollback_response.status_code == 200, rollback_response.text
    rolled_back = rollback_response.json()
    assert rolled_back["current_version_id"] == version_id

    disabled_response = await client.post(
        f"/api/v1/skills/{skill_id}/disable",
        json={
            "reason": "Exercise the emergency stop path.",
            "expected_skill_revision": rolled_back["skill_revision"],
        },
        headers=headers,
    )
    assert disabled_response.status_code == 200, disabled_response.text
    assert disabled_response.json()["skill_status"] == "disabled"
    deployments = await client.get(f"/api/v1/skills/{skill_id}/deployments", headers=headers)
    assert deployments.status_code == 200
    assert deployments.json()[0]["status"] == "retired"

    invalid_shape = await client.post(
        "/api/v1/memories/search",
        json={"query": "   ", "project_id": project["id"]},
        headers=headers,
    )
    assert invalid_shape.status_code == 422

    foreign_headers = await _bootstrap(client, "GrowthForeign")
    assert (await client.get(memory_path, headers=foreign_headers)).status_code == 404
    assert (await client.get(f"{version_path}/sources", headers=foreign_headers)).status_code == 404
    assert (await client.get("/api/v1/memories", headers=foreign_headers)).json() == []
    assert (await client.get("/api/v1/skills", headers=foreign_headers)).json() == []
