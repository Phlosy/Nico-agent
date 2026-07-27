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


def test_external_tool_provider_client_uses_public_registry_routes() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "provider-1", "status": "active"})

    with NicoApiClient(profile(), transport=httpx.MockTransport(handler)) as client:
        client.register_external_tool_provider(
            name="local.stub",
            endpoint_ref="https://provider.example.test",
            credential_ref="env:NICO_TOOL_SECRET_STUB",
            project_id="project-1",
        )
        client.get_external_tool_provider("provider-1")
        client.transition_external_tool_provider("provider-1", action="verify")
        client.transition_external_tool_provider("provider-1", action="disable")
        client.transition_external_tool_provider("provider-1", action="revoke")

    assert [request.url.path for request in seen] == [
        "/api/v1/external-tool-providers",
        "/api/v1/external-tool-providers/provider-1",
        "/api/v1/external-tool-providers/provider-1/verify",
        "/api/v1/external-tool-providers/provider-1/disable",
        "/api/v1/external-tool-providers/provider-1/revoke",
    ]
    assert seen[0].read() == (
        b'{"name":"local.stub","endpoint_ref":"https://provider.example.test",'
        b'"credential_ref":"env:NICO_TOOL_SECRET_STUB","project_id":"project-1"}'
    )


def test_external_tool_provider_client_rejects_unknown_transition() -> None:
    with (
        NicoApiClient(
            profile(),
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
        ) as client,
        pytest.raises(ValueError, match="unsupported"),
    ):
        client.transition_external_tool_provider("provider-1", action="delete")


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


def test_project_workflow_requests_preserve_revision_and_idempotency() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"member": {"status": "paused"}})

    with NicoApiClient(profile(), transport=httpx.MockTransport(handler)) as client:
        result = client.set_project_member_state(
            "project-1",
            "agent-1",
            target="paused",
            expected_project_revision=4,
            expected_member_revision=2,
            reason="waiting",
            idempotency_key="member-state-1",
        )

    assert result["member"]["status"] == "paused"
    assert seen[0].url.path == "/api/v1/projects/project-1/members/agent-1/state"
    assert seen[0].headers["idempotency-key"] == "member-state-1"
    assert seen[0].read() == (
        b'{"target":"paused","expected_project_revision":4,'
        b'"expected_member_revision":2,"reason":"waiting"}'
    )


def test_project_intervention_request_binds_session_run_and_revision() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"id": "intervention-1", "status": "pending"})

    with NicoApiClient(profile(), transport=httpx.MockTransport(handler)) as client:
        result = client.create_run_intervention(
            "project-1",
            "session-1",
            "run-1",
            content="re-check the tests",
            expected_run_revision=7,
            idempotency_key="guide-1",
        )

    assert result["status"] == "pending"
    assert seen[0].url.path == (
        "/api/v1/projects/project-1/sessions/session-1/runs/run-1/interventions"
    )
    assert seen[0].headers["idempotency-key"] == "guide-1"
    assert seen[0].read() == (b'{"content":"re-check the tests","expected_run_revision":7}')


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


def test_user_input_answer_is_versioned_idempotent_and_not_a_conversation_turn() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"id": "question-1", "status": "answered", "revision": 2},
        )

    with NicoApiClient(profile(), transport=httpx.MockTransport(handler)) as client:
        value = client.answer_user_input(
            "question-1",
            expected_revision=1,
            answer={"target": "database", "confirmed": True},
            idempotency_key="user-input-answer-1",
        )

    assert value["status"] == "answered"
    assert seen[0].url.path == "/api/v1/user-input-requests/question-1/answer"
    assert seen[0].headers["idempotency-key"] == "user-input-answer-1"
    assert seen[0].read() == (
        b'{"expected_revision":1,"answer":{"target":"database","confirmed":true}}'
    )
    assert "/conversations/" not in seen[0].url.path


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


def test_sse_stream_reports_reconnect_and_recovery_without_synthetic_events() -> None:
    requests: list[httpx.Request] = []
    updates: list[tuple[str, int | None, int | None]] = []
    body = (
        'id: 3\nevent: RunStarted\ndata: {"sequence":3,"type":"RunStarted","payload":{}}\n\n'
        'id: 4\nevent: RunCompleted\ndata: {"sequence":4,"type":"RunCompleted","payload":{}}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("temporary timeout", request=request)
        return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})

    with NicoApiClient(profile(), transport=httpx.MockTransport(handler)) as client:
        events = list(
            client.stream_run_events(
                "run-1",
                after_sequence=3,
                on_connection=lambda state, attempt, maximum: updates.append(
                    (state, attempt, maximum)
                ),
            )
        )

    assert [event["sequence"] for event in events] == [4]
    assert [request.headers["last-event-id"] for request in requests] == ["3", "3"]
    assert updates == [("reconnecting", 1, 3), ("recovered", 1, 3)]


def test_sse_stream_reports_only_bounded_reconnect_attempts_before_disconnect() -> None:
    requests: list[httpx.Request] = []
    updates: list[tuple[str, int | None, int | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ConnectError("disconnected", request=request)

    with (
        NicoApiClient(profile(), transport=httpx.MockTransport(handler)) as client,
        pytest.raises(CliError) as captured,
    ):
        list(
            client.stream_run_events(
                "run-1",
                reconnect_attempts=2,
                on_connection=lambda state, attempt, maximum: updates.append(
                    (state, attempt, maximum)
                ),
            )
        )

    assert captured.value.code == "SSE_DISCONNECTED"
    assert len(requests) == 3
    assert updates == [("reconnecting", 1, 2), ("reconnecting", 2, 2)]


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


def test_conversation_queue_and_permission_requests_are_revisioned_and_idempotent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"conversation_id": "conversation-1", "revision": 5})

    with NicoApiClient(profile(), transport=httpx.MockTransport(handler)) as client:
        client.get_conversation_queue("conversation-1")
        client.resume_conversation_queue(
            "conversation-1",
            expected_revision=4,
            idempotency_key="resume-1",
        )
        client.update_conversation(
            "conversation-1",
            expected_revision=5,
            approval_mode="auto-medium",
        )

    assert [request.url.path for request in seen] == [
        "/api/v1/conversations/conversation-1/queue",
        "/api/v1/conversations/conversation-1/queue/resume",
        "/api/v1/conversations/conversation-1",
    ]
    assert seen[1].headers["idempotency-key"] == "resume-1"
    assert seen[1].read() == b'{"expected_revision":4,"idempotency_key":"resume-1"}'
    assert seen[2].read() == b'{"expected_revision":5,"approval_mode":"auto-medium"}'


def test_agent_default_permission_request_is_revisioned() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "agent-1",
                "revision": 6,
                "default_approval_mode": "auto-medium",
            },
        )

    with NicoApiClient(profile(), transport=httpx.MockTransport(handler)) as client:
        client.update_agent(
            "agent-1",
            expected_revision=5,
            default_approval_mode="auto-medium",
        )

    assert seen[0].url.path == "/api/v1/agents/agent-1"
    assert seen[0].read() == (b'{"expected_revision":5,"default_approval_mode":"auto-medium"}')


def test_agent_response_metrics_request_is_revisioned() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "agent-1",
                "revision": 6,
                "show_response_metrics": True,
            },
        )

    with NicoApiClient(profile(), transport=httpx.MockTransport(handler)) as client:
        client.update_agent(
            "agent-1",
            expected_revision=5,
            show_response_metrics=True,
        )

    assert seen[0].url.path == "/api/v1/agents/agent-1"
    assert seen[0].read() == (b'{"expected_revision":5,"show_response_metrics":true}')


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
