from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from nico_agent.api import create_app
from nico_agent.config import Settings
from nico_agent.database import TenantContext
from nico_agent.domain.models import (
    Artifact,
    AuditRecord,
    Conversation,
    ConversationAttachment,
    Event,
    Project,
    Run,
    Task,
)
from nico_agent.runtime import MockRuntimeProvider, RuntimeProviderRegistry
from nico_agent.runtime.executor import RuntimeWorker

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


async def _bootstrap(client: AsyncClient, label: str) -> tuple[UUID, dict[str, str]]:
    response = await client.post(
        "/api/v1/tenants/bootstrap",
        json={"name": label, "slug": f"{label.lower()}-{uuid4()}"},
        headers={"X-Actor-ID": "conversation-test"},
    )
    assert response.status_code == 201, response.text
    tenant_id = UUID(response.json()["id"])
    return tenant_id, {
        "X-Tenant-ID": str(tenant_id),
        "X-Actor-ID": "conversation-test",
    }


async def _project_and_agent(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    output: dict | None = None,
    mock: dict | None = None,
) -> tuple[dict, dict, dict]:
    project_response = await client.post(
        "/api/v1/projects",
        json={"name": f"conversation-{uuid4().hex[:8]}"},
        headers=headers,
    )
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()
    agent_response = await client.post(
        "/api/v1/agents",
        json={
            "name": f"chat-{uuid4().hex[:8]}",
            "display_name": "Conversation Agent",
        },
        headers=headers,
    )
    assert agent_response.status_code == 201, agent_response.text
    agent = agent_response.json()
    version_response = await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json={
            "role": "assistant",
            "mandate": "Answer the current conversation turn",
            "runtime_provider": "mock",
            "run_config": {
                "mock": mock
                or {
                    "steps": ["understand", "answer"],
                    "output": output or {"answer": "first response"},
                }
            },
        },
        headers=headers,
    )
    assert version_response.status_code == 201, version_response.text
    version = version_response.json()
    published = await client.post(
        f"/api/v1/agents/{agent['id']}/versions/{version['id']}/publish",
        json={"expected_revision": agent["revision"]},
        headers=headers,
    )
    assert published.status_code == 200, published.text
    return project, published.json(), version


async def _create_conversation(
    client: AsyncClient,
    headers: dict[str, str],
    project: dict,
    agent: dict,
    *,
    key: str,
) -> dict:
    response = await client.post(
        "/api/v1/conversations",
        json={
            "project_id": project["id"],
            "agent_id": agent["id"],
            "title": "Durable chat",
        },
        headers={**headers, "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_personal_conversations_are_idempotent_hidden_and_actor_scoped(app_client) -> None:
    app, client = app_client
    tenant_id, first_headers = await _bootstrap(client, "PersonalConversation")
    _, agent, _ = await _project_and_agent(client, first_headers)

    created = await client.post(
        "/api/v1/conversations",
        json={"mode": "personal", "agent_id": agent["id"], "title": "Private chat"},
        headers={**first_headers, "Idempotency-Key": "personal-chat-1"},
    )
    assert created.status_code == 201, created.text
    first = created.json()
    replay = await client.post(
        "/api/v1/conversations",
        json={"mode": "personal", "agent_id": agent["id"], "title": "Private chat"},
        headers={**first_headers, "Idempotency-Key": "personal-chat-1"},
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == first["id"]
    assert replay.json()["project_id"] == first["project_id"]

    conflict = await client.post(
        "/api/v1/conversations",
        json={"mode": "personal", "agent_id": agent["id"], "title": "Changed"},
        headers={**first_headers, "Idempotency-Key": "personal-chat-1"},
    )
    assert conflict.status_code == 400
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

    assert (await client.get("/api/v1/projects", headers=first_headers)).json() != []
    visible = (await client.get("/api/v1/projects", headers=first_headers)).json()
    assert all(project["kind"] == "shared" for project in visible)
    with_system = (
        await client.get("/api/v1/projects?include_system=true", headers=first_headers)
    ).json()
    personal = [project for project in with_system if project["kind"] == "personal"]
    assert len(personal) == 1
    assert personal[0]["id"] == first["project_id"]
    assert personal[0]["owner_actor_id"] == first_headers["X-Actor-ID"]

    second_headers = {**first_headers, "X-Actor-ID": "another-operator"}
    second_created = await client.post(
        "/api/v1/conversations",
        json={"mode": "personal", "agent_id": agent["id"], "title": "Other private chat"},
        headers={**second_headers, "Idempotency-Key": "personal-chat-2"},
    )
    assert second_created.status_code == 201, second_created.text
    second = second_created.json()
    assert second["project_id"] != first["project_id"]

    first_list = (
        await client.get("/api/v1/conversations?mode=personal", headers=first_headers)
    ).json()
    second_list = (
        await client.get("/api/v1/conversations?mode=personal", headers=second_headers)
    ).json()
    assert [item["id"] for item in first_list] == [first["id"]]
    assert [item["id"] for item in second_list] == [second["id"]]
    assert (
        await client.get(f"/api/v1/conversations/{first['id']}", headers=second_headers)
    ).status_code == 404

    for actor_headers in (first_headers, second_headers):
        collaboration = await client.get(
            f"/api/v1/projects/{first['project_id']}/members",
            headers=actor_headers,
        )
        assert collaboration.status_code == 409
        assert collaboration.json()["code"] == "PROJECT_NOT_MANAGED"

    turn = await client.post(
        f"/api/v1/conversations/{first['id']}/turns",
        json={"user_input": "Keep this private."},
        headers={**first_headers, "Idempotency-Key": "personal-private-turn"},
    )
    assert turn.status_code == 202, turn.text
    for resource in ("tasks", "runs"):
        resource_id = turn.json()["task_id" if resource == "tasks" else "run_id"]
        assert (
            await client.get(f"/api/v1/{resource}/{resource_id}", headers=second_headers)
        ).status_code == 404
    cancelled = await client.post(
        f"/api/v1/conversation-turns/{turn.json()['id']}/cancel",
        json={"expected_revision": turn.json()["run_revision"]},
        headers=first_headers,
    )
    assert cancelled.status_code == 200, cancelled.text

    context = TenantContext(tenant_id, first_headers["X-Actor-ID"], uuid4())
    async with app.state.database.tenant_transaction(context) as session:
        personal_projects = list(
            await session.scalars(
                select(Project).where(
                    Project.tenant_id == tenant_id,
                    Project.kind == "personal",
                )
            )
        )
        project_events = list(
            await session.scalars(
                select(Event).where(
                    Event.tenant_id == tenant_id,
                    Event.event_type == "PersonalProjectCreated",
                )
            )
        )
        project_audits = list(
            await session.scalars(
                select(AuditRecord).where(
                    AuditRecord.tenant_id == tenant_id,
                    AuditRecord.action == "personal_project.create",
                )
            )
        )
    assert len(personal_projects) == 2
    assert len(project_events) == len(project_audits) == 2


@pytest.mark.asyncio
async def test_personal_mode_rejects_explicit_project_and_project_mode_stays_compatible(
    app_client,
) -> None:
    _, client = app_client
    _, headers = await _bootstrap(client, "PersonalContract")
    project, agent, _ = await _project_and_agent(client, headers)

    invalid = await client.post(
        "/api/v1/conversations",
        json={
            "mode": "personal",
            "project_id": project["id"],
            "agent_id": agent["id"],
        },
        headers=headers,
    )
    assert invalid.status_code == 422
    legacy = await _create_conversation(client, headers, project, agent, key="legacy-explicit")
    explicit = await client.post(
        "/api/v1/conversations",
        json={
            "mode": "project",
            "project_id": project["id"],
            "agent_id": agent["id"],
            "title": "Explicit project chat",
        },
        headers={**headers, "Idempotency-Key": "explicit-project"},
    )
    assert explicit.status_code == 201, explicit.text
    assert explicit.json()["project_id"] == legacy["project_id"] == project["id"]


@pytest.mark.asyncio
async def test_two_turn_conversation_is_atomic_streamable_resumable_and_version_frozen(
    app_client,
) -> None:
    app, client = app_client
    tenant_id, headers = await _bootstrap(client, "ConversationFlow")
    project, agent, first_version = await _project_and_agent(client, headers)
    conversation = await _create_conversation(
        client, headers, project, agent, key="conversation-flow"
    )

    replay = await _create_conversation(client, headers, project, agent, key="conversation-flow")
    assert replay["id"] == conversation["id"]
    assert conversation["agent_version_id"] == first_version["id"]

    first_response = await client.post(
        f"/api/v1/conversations/{conversation['id']}/turns",
        json={"user_input": "first question"},
        headers={**headers, "Idempotency-Key": "turn-1"},
    )
    assert first_response.status_code == 202, first_response.text
    first = first_response.json()
    assert first["status"] == "queued"
    assert first["run_status"] == "pending"
    assert (await client.get(f"/api/v1/tasks/{first['task_id']}", headers=headers)).json()[
        "input"
    ] == {
        "conversation": {
            "conversation_id": conversation["id"],
            "turn_id": first["id"],
            "sequence": 1,
        },
        "message": "first question",
    }
    assert (await client.get(f"/api/v1/runs/{first['run_id']}", headers=headers)).json()[
        "agent_version_id"
    ] == first_version["id"]

    active_conflict = await client.post(
        f"/api/v1/conversations/{conversation['id']}/turns",
        json={"user_input": "too early"},
        headers={**headers, "Idempotency-Key": "turn-early"},
    )
    assert active_conflict.status_code == 400
    assert active_conflict.json()["code"] == "CONVERSATION_RUN_ACTIVE"

    worker = RuntimeWorker(
        app.state.database,
        RuntimeProviderRegistry([MockRuntimeProvider()]),
        worker_id="conversation-worker-1",
        lease_seconds=5,
        heartbeat_seconds=0.05,
    )
    assert await worker.execute_once() is True
    first_done = (
        await client.get(f"/api/v1/conversation-turns/{first['id']}", headers=headers)
    ).json()
    assert first_done["status"] == first_done["run_status"] == "completed"
    assert first_done["assistant_output"] == {"answer": "first response"}
    assert first_done["usage"]["steps"] == 2

    async with client.stream(
        "GET",
        f"/api/v1/runs/{first['run_id']}/events/stream",
        headers={**headers, "Last-Event-ID": "0"},
    ) as stream:
        body = (await stream.aread()).decode()
    assert stream.status_code == 200
    assert "event: RunCompleted" in body
    assert "id: " in body

    second_version_response = await client.post(
        f"/api/v1/agents/{agent['id']}/versions",
        json={
            "role": "assistant-v2",
            "mandate": "A newer version",
            "runtime_provider": "mock",
            "run_config": {"mock": {"output": {"answer": "new version"}}},
        },
        headers=headers,
    )
    second_version = second_version_response.json()
    latest_agent = (await client.get(f"/api/v1/agents/{agent['id']}", headers=headers)).json()
    published = await client.post(
        f"/api/v1/agents/{agent['id']}/versions/{second_version['id']}/publish",
        json={"expected_revision": latest_agent["revision"]},
        headers=headers,
    )
    assert published.status_code == 200

    second_response = await client.post(
        f"/api/v1/conversations/{conversation['id']}/turns",
        json={"user_input": "second question"},
        headers={**headers, "Idempotency-Key": "turn-2"},
    )
    assert second_response.status_code == 202, second_response.text
    second = second_response.json()
    second_run = (await client.get(f"/api/v1/runs/{second['run_id']}", headers=headers)).json()
    assert second_run["agent_version_id"] == first_version["id"]
    assert second_run["agent_version_id"] != second_version["id"]
    assert await worker.execute_once() is True

    history = (
        await client.get(
            f"/api/v1/conversations/{conversation['id']}/turns?after_sequence=0&limit=10",
            headers=headers,
        )
    ).json()
    assert [item["sequence"] for item in history] == [1, 2]
    assert all(item["status"] == "completed" for item in history)
    continued = (
        await client.get(
            f"/api/v1/conversations?status=active&project_id={project['id']}&limit=1",
            headers=headers,
        )
    ).json()
    assert continued[0]["id"] == conversation["id"]

    _, foreign_headers = await _bootstrap(client, "ConversationForeign")
    assert (
        await client.get(f"/api/v1/conversations/{conversation['id']}", headers=foreign_headers)
    ).status_code == 404
    assert UUID(headers["X-Tenant-ID"]) == tenant_id


@pytest.mark.asyncio
async def test_turn_idempotency_cancel_archive_rls_and_identity_guards(app_client) -> None:
    app, client = app_client
    tenant_id, headers = await _bootstrap(client, "ConversationGuards")
    project, agent, version = await _project_and_agent(client, headers)
    conversation = await _create_conversation(
        client, headers, project, agent, key="conversation-guards"
    )
    turn_response = await client.post(
        f"/api/v1/conversations/{conversation['id']}/turns",
        json={"user_input": "cancel this"},
        headers={**headers, "Idempotency-Key": "cancel-turn"},
    )
    assert turn_response.status_code == 202
    turn = turn_response.json()

    duplicate = await client.post(
        f"/api/v1/conversations/{conversation['id']}/turns",
        json={"user_input": "cancel this"},
        headers={**headers, "Idempotency-Key": "cancel-turn"},
    )
    assert duplicate.status_code == 202
    assert duplicate.json()["id"] == turn["id"]
    mismatch = await client.post(
        f"/api/v1/conversations/{conversation['id']}/turns",
        json={"user_input": "different"},
        headers={**headers, "Idempotency-Key": "cancel-turn"},
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

    cancelled_response = await client.post(
        f"/api/v1/conversation-turns/{turn['id']}/cancel",
        json={"expected_revision": turn["run_revision"]},
        headers=headers,
    )
    assert cancelled_response.status_code == 200, cancelled_response.text
    cancelled = cancelled_response.json()
    assert cancelled["status"] == cancelled["run_status"] == "cancelled"
    assert (await client.get(f"/api/v1/tasks/{turn['task_id']}", headers=headers)).json()[
        "status"
    ] == "cancelled"

    context = TenantContext(tenant_id, "guard-test", uuid4())
    async with app.state.database.tenant_transaction(context) as session:
        task_count = await session.scalar(
            select(func.count(Task.id)).where(Task.tenant_id == tenant_id)
        )
        run_count = await session.scalar(
            select(func.count(Run.id)).where(Run.tenant_id == tenant_id)
        )
    assert (task_count, run_count) == (1, 1)

    archived_agent = await client.post(
        f"/api/v1/agents/{agent['id']}/archive",
        json={"expected_revision": agent["revision"]},
        headers=headers,
    )
    assert archived_agent.status_code == 200, archived_agent.text
    agent_denied = await client.post(
        f"/api/v1/conversations/{conversation['id']}/turns",
        json={"user_input": "after agent archive"},
        headers={**headers, "Idempotency-Key": "archived-agent-turn"},
    )
    assert agent_denied.status_code == 400
    assert agent_denied.json()["code"] == "AGENT_NOT_READY"

    with pytest.raises(DBAPIError, match="conversation identity is immutable"):
        async with app.state.database.tenant_transaction(context) as session:
            await session.execute(
                update(Conversation)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.id == UUID(conversation["id"]),
                )
                .values(agent_version_id=uuid4())
            )

    latest = (
        await client.get(f"/api/v1/conversations/{conversation['id']}", headers=headers)
    ).json()
    archived_response = await client.patch(
        f"/api/v1/conversations/{conversation['id']}",
        json={"expected_revision": latest["revision"], "status": "archived"},
        headers=headers,
    )
    assert archived_response.status_code == 200
    assert archived_response.json()["status"] == "archived"
    denied = await client.post(
        f"/api/v1/conversations/{conversation['id']}/turns",
        json={"user_input": "after archive"},
        headers={**headers, "Idempotency-Key": "archived-turn"},
    )
    assert denied.status_code == 400
    assert denied.json()["code"] == "CONVERSATION_ARCHIVED"
    assert conversation["agent_version_id"] == version["id"]


@pytest.mark.asyncio
async def test_failed_conversation_turn_retries_with_frozen_version(app_client) -> None:
    app, client = app_client
    _, headers = await _bootstrap(client, "ConversationRetry")
    project, agent, version = await _project_and_agent(
        client,
        headers,
        mock={"fail": True, "error_code": "EXPECTED_FAILURE"},
    )
    conversation = await _create_conversation(
        client, headers, project, agent, key="conversation-retry"
    )
    accepted = await client.post(
        f"/api/v1/conversations/{conversation['id']}/turns",
        json={"user_input": "retry this failure"},
        headers={**headers, "Idempotency-Key": "retry-turn"},
    )
    assert accepted.status_code == 202, accepted.text
    first = accepted.json()

    worker = RuntimeWorker(
        app.state.database,
        RuntimeProviderRegistry([MockRuntimeProvider()]),
        worker_id="conversation-retry-worker",
        lease_seconds=5,
        heartbeat_seconds=0.05,
    )
    assert await worker.execute_once() is True
    failed = (await client.get(f"/api/v1/conversation-turns/{first['id']}", headers=headers)).json()
    assert failed["status"] == failed["run_status"] == "failed"
    assert failed["error"]["code"] == "EXPECTED_FAILURE"

    generic_retry = await client.post(
        f"/api/v1/runs/{failed['run_id']}/retry",
        json={},
        headers=headers,
    )
    assert generic_retry.status_code == 400
    assert generic_retry.json()["code"] == "CONVERSATION_RETRY_REQUIRED"

    retried_response = await client.post(
        f"/api/v1/conversation-turns/{first['id']}/retry",
        json={
            "expected_run_id": failed["run_id"],
            "expected_run_revision": failed["run_revision"],
            "max_steps": 7,
        },
        headers=headers,
    )
    assert retried_response.status_code == 201, retried_response.text
    retried = retried_response.json()
    assert retried["id"] == first["id"]
    assert retried["run_id"] != failed["run_id"]
    assert retried["status"] == "queued"
    retry_run = (await client.get(f"/api/v1/runs/{retried['run_id']}", headers=headers)).json()
    assert retry_run["retry_of_run_id"] == failed["run_id"]
    assert retry_run["attempt"] == 2
    assert retry_run["max_steps"] == 7
    assert retry_run["agent_version_id"] == version["id"]
    retry_task = (await client.get(f"/api/v1/tasks/{retried['task_id']}", headers=headers)).json()
    assert retry_task["status"] == "running"

    replay = await client.post(
        f"/api/v1/conversation-turns/{first['id']}/retry",
        json={
            "expected_run_id": failed["run_id"],
            "expected_run_revision": failed["run_revision"],
        },
        headers=headers,
    )
    assert replay.status_code == 400
    assert replay.json()["code"] == "CONVERSATION_TURN_RUN_CHANGED"

    cancelled = await client.post(
        f"/api/v1/conversation-turns/{first['id']}/cancel",
        json={"expected_revision": retried["run_revision"]},
        headers=headers,
    )
    assert cancelled.status_code == 200, cancelled.text


@pytest.mark.asyncio
async def test_attachment_materialization_download_and_compaction_are_durable(app_client) -> None:
    app, client = app_client
    tenant_id, headers = await _bootstrap(client, "ConversationContext")
    project, agent, _ = await _project_and_agent(
        client,
        headers,
        mock={"steps": ["answer"], "output": {"content": "durable compact summary"}},
    )
    conversation = await _create_conversation(
        client, headers, project, agent, key="conversation-context"
    )
    attachment_url = f"/api/v1/conversations/{conversation['id']}/attachments"
    uploaded = await client.post(
        attachment_url,
        params={
            "name": "facts.txt",
            "content_type": "text/plain",
            "idempotency_key": "attachment-1",
        },
        content=b"blue-gold-white cat facts",
        headers=headers,
    )
    assert uploaded.status_code == 201, uploaded.text
    staged = uploaded.json()
    replay = await client.post(
        attachment_url,
        params={
            "name": "facts.txt",
            "content_type": "text/plain",
            "idempotency_key": "attachment-1",
        },
        content=b"blue-gold-white cat facts",
        headers=headers,
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == staged["id"]
    assert (await client.get(attachment_url, headers=headers)).json()[0]["status"] == "staged"

    accepted = await client.post(
        f"/api/v1/conversations/{conversation['id']}/turns",
        json={"user_input": "use the attachment"},
        headers={**headers, "Idempotency-Key": "attachment-turn"},
    )
    assert accepted.status_code == 202, accepted.text
    turn = accepted.json()
    assert len(turn["artifact_refs"]) == 1
    reference = turn["artifact_refs"][0]
    assert reference["name"] == "facts.txt"
    assert reference["summary"] == "blue-gold-white cat facts"
    assert reference["owner_run_id"] == turn["run_id"]

    context = TenantContext(tenant_id, "attachment-test", uuid4())
    async with app.state.database.tenant_transaction(context) as session:
        row = await session.scalar(
            select(ConversationAttachment).where(
                ConversationAttachment.tenant_id == tenant_id,
                ConversationAttachment.id == UUID(staged["id"]),
            )
        )
        artifact = await session.scalar(
            select(Artifact).where(
                Artifact.tenant_id == tenant_id,
                Artifact.id == UUID(reference["artifact_id"]),
            )
        )
    assert row is not None and row.status == "consumed"
    assert artifact is not None and artifact.owner_run_id == UUID(turn["run_id"])

    worker = RuntimeWorker(
        app.state.database,
        RuntimeProviderRegistry([MockRuntimeProvider()]),
        worker_id="conversation-context-worker",
        lease_seconds=5,
        heartbeat_seconds=0.05,
    )
    assert await worker.execute_once() is True
    downloaded = await client.get(
        f"/api/v1/runs/{turn['run_id']}/artifacts/{reference['artifact_id']}/content",
        headers=headers,
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"blue-gold-white cat facts"

    compacted = await client.post(
        f"/api/v1/conversations/{conversation['id']}/compact",
        json={},
        headers={**headers, "Idempotency-Key": "compact-1"},
    )
    assert compacted.status_code == 202, compacted.text
    compact_run = compacted.json()
    assert compact_run["through_sequence"] == 1
    assert await worker.execute_once() is True
    refreshed = (
        await client.get(f"/api/v1/conversations/{conversation['id']}", headers=headers)
    ).json()
    assert refreshed["summary"] == "durable compact summary"
    assert refreshed["summary_through_sequence"] == 1
    assert refreshed["summary_input_hash"] == compact_run["input_hash"]

    replay_compact = await client.post(
        f"/api/v1/conversations/{conversation['id']}/compact",
        json={},
        headers={**headers, "Idempotency-Key": "compact-1"},
    )
    assert replay_compact.status_code == 202
    assert replay_compact.json()["run_id"] == compact_run["run_id"]
    assert replay_compact.json()["replayed"] is True
