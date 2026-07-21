from __future__ import annotations

from uuid import UUID

import httpx
import pytest

from nico_agent.cli.client import NicoApiClient
from nico_agent.cli.config import ResolvedProfile
from nico_agent.cli.errors import CliError

TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")


def profile(*, tenant: bool = True, token: str | None = "private-token") -> ResolvedProfile:
    return ResolvedProfile(
        name="test",
        base_url="https://nico.example.test",
        tenant_id=TENANT_ID if tenant else None,
        actor_id="cli-test",
        api_token_env="TEST_TOKEN" if token else None,
        api_token=token,
        timeout_seconds=5,
        verify_tls=True,
    )


def test_resource_request_sends_context_auth_and_request_id() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json=[{"id": "agent-1"}], headers={"X-Request-ID": "r1"})

    with NicoApiClient(profile(), transport=httpx.MockTransport(handler)) as client:
        result = client.list_agents()

    assert result == [{"id": "agent-1"}]
    assert seen["x-tenant-id"] == str(TENANT_ID)
    assert seen["x-actor-id"] == "cli-test"
    assert seen["authorization"] == "Bearer private-token"
    assert seen["x-request-id"]


def test_health_does_not_require_tenant_context() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"status": "alive"}))
    with NicoApiClient(profile(tenant=False), transport=transport) as client:
        assert client.liveness() == {"status": "alive"}


def test_resource_request_without_tenant_fails_before_network() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=[])

    with (
        NicoApiClient(profile(tenant=False), transport=httpx.MockTransport(handler)) as client,
        pytest.raises(CliError) as captured,
    ):
        client.list_agents()

    assert captured.value.code == "TENANT_CONTEXT_REQUIRED"
    assert captured.value.exit_code == 2
    assert called is False


def test_api_error_preserves_safe_code_and_request_id() -> None:
    response = httpx.MockTransport(
        lambda _request: httpx.Response(
            404,
            json={"code": "RESOURCE_NOT_FOUND", "message": "agent not found"},
            headers={"X-Request-ID": "request-42"},
        )
    )
    with (
        NicoApiClient(profile(), transport=response) as client,
        pytest.raises(CliError) as captured,
    ):
        client.get_agent("missing")

    assert captured.value.as_dict() == {
        "error": {
            "code": "RESOURCE_NOT_FOUND",
            "message": "agent not found",
            "status_code": 404,
            "request_id": "request-42",
        }
    }


def test_non_json_success_is_rejected() -> None:
    response = httpx.MockTransport(lambda _request: httpx.Response(200, text="not json"))
    with (
        NicoApiClient(profile(), transport=response) as client,
        pytest.raises(CliError) as captured,
    ):
        client.list_agents()
    assert captured.value.code == "INVALID_API_RESPONSE"


def test_conversation_writes_send_idempotency_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": "conversation-1"})

    with NicoApiClient(profile(), transport=httpx.MockTransport(handler)) as client:
        client.create_conversation(
            project_id=None,
            agent_id="agent-1",
            mode="personal",
            idempotency_key="create-1",
        )

    assert seen[0].headers["idempotency-key"] == "create-1"
    assert seen[0].url.path == "/api/v1/conversations"
    assert seen[0].read() == (
        b'{"mode":"personal","agent_id":"agent-1","title":"New conversation"}'
    )


def test_tool_approval_decision_is_versioned_and_idempotent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"id": "approval-1", "status": "approved", "allowed_scope": "once"},
        )

    with NicoApiClient(profile(), transport=httpx.MockTransport(handler)) as client:
        value = client.decide_tool_approval(
            "approval-1",
            expected_revision=3,
            decision="approve",
            allowed_scope="once",
            idempotency_key="approval-decision-1",
        )

    assert value["status"] == "approved"
    assert seen[0].url.path == "/api/v1/tool-approval-requests/approval-1/decision"
    assert seen[0].headers["idempotency-key"] == "approval-decision-1"
    assert seen[0].read() == (
        b'{"expected_revision":3,"decision":"approve","allowed_scope":"once"}'
    )


def test_sse_stream_sends_cursor_and_deduplicates_sequences() -> None:
    seen: dict[str, str] = {}
    body = (
        'id: 3\nevent: RunStarted\ndata: {"sequence":3,"type":"RunStarted","payload":{}}\n\n'
        'id: 4\nevent: RunCompleted\ndata: {"sequence":4,"type":"RunCompleted","payload":{}}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(
            200,
            text=body,
            headers={"Content-Type": "text/event-stream"},
        )

    with NicoApiClient(profile(), transport=httpx.MockTransport(handler)) as client:
        events = list(client.stream_run_events("run-1", after_sequence=3))

    assert [event["sequence"] for event in events] == [4]
    assert seen["last-event-id"] == "3"
    assert seen["accept"] == "text/event-stream"


def test_conversation_retry_sends_current_run_identity_and_revision() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": "turn-1", "run_id": "run-2"})

    with NicoApiClient(profile(), transport=httpx.MockTransport(handler)) as client:
        result = client.retry_conversation_turn(
            "turn-1", expected_run_id="run-1", expected_run_revision=4
        )

    assert result["run_id"] == "run-2"
    assert seen[0].url.path == "/api/v1/conversation-turns/turn-1/retry"
    assert seen[0].read() == b'{"expected_run_id":"run-1","expected_run_revision":4}'


def test_conversation_attachment_and_artifact_bytes_never_become_server_paths() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"id": "attachment-1", "status": "staged"})
        return httpx.Response(
            200, content=b"artifact-bytes", headers={"Content-Type": "text/plain"}
        )

    with NicoApiClient(profile(), transport=httpx.MockTransport(handler)) as client:
        uploaded = client.upload_conversation_attachment(
            "conversation-1",
            name="notes.txt",
            content_type="text/plain",
            idempotency_key="attach-1",
            data=b"local-bytes",
        )
        content, content_type = client.download_artifact("run-1", "artifact-1")

    assert uploaded["status"] == "staged"
    assert seen[0].url.path == "/api/v1/conversations/conversation-1/attachments"
    assert seen[0].read() == b"local-bytes"
    assert "path" not in dict(seen[0].url.params)
    assert content == b"artifact-bytes"
    assert content_type == "text/plain"


def test_provider_client_binds_preview_hash_without_accepting_raw_key_fields() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/preview"):
            return httpx.Response(200, json={"preview_hash": "b" * 64})
        return httpx.Response(201, json={"status": "ready"})

    target = {
        "project_id": "project-1",
        "starter_agent_name": "assistant-one",
        "starter_agent_display_name": "Assistant One",
    }
    with NicoApiClient(profile(), transport=httpx.MockTransport(handler)) as client:
        preview = client.preview_provider_activation(
            probe_id="probe-1",
            candidate_hash="a" * 64,
            target=target,
        )
        client.activate_provider(
            probe_id="probe-1",
            candidate_hash="a" * 64,
            target=target,
            preview_hash=preview["preview_hash"],
            maintenance_attempt_id="attempt-1",
        )

    assert seen[0].url.path == "/api/v1/provider-activation/preview"
    assert seen[1].url.path == "/api/v1/provider-activation"
    assert b'"preview_hash":"' + b"b" * 64 + b'"' in seen[1].read()
    assert b"api_key" not in seen[0].read() + seen[1].read()
