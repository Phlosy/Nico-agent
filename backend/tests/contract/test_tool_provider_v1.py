from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate

from nico_agent.testing.fake_tool_provider import (
    FAKE_ECHO_OUTPUT_SCHEMA,
    FAKE_ECHO_TOOL,
    FakeToolProvider,
    FakeToolProviderMode,
    FakeToolProviderScope,
)
from nico_agent.tool_providers.contracts import (
    ProviderCancelRequest,
    ProviderCancelResponse,
    ProviderCapabilitiesResponse,
    ProviderHealthResponse,
    ProviderToolCallRequest,
    ProviderToolCallResponse,
    ProviderToolCallStatus,
    ProviderTrace,
    canonical_json_bytes,
)
from nico_agent.tool_providers.security import sign_hmac_request

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
SECRET = "contract-test-tool-provider-secret-at-least-32-bytes"
PROVIDER_ID = "provider_contract_stub"
BINDING_DIGEST = f"sha256:{'c' * 64}"
TENANT_ID = UUID("11111111-1111-4111-8111-111111111111")
PROJECT_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
TASK_ID = UUID("44444444-4444-4444-8444-444444444444")
AGENT_ID = UUID("55555555-5555-4555-8555-555555555555")
AGENT_VERSION_ID = UUID("66666666-6666-4666-8666-666666666666")


@pytest.fixture
def provider() -> FakeToolProvider:
    stub = FakeToolProvider(
        provider_id=PROVIDER_ID,
        secret=SECRET,
        clock=lambda: NOW,
    )
    stub.provision_scope(
        FakeToolProviderScope(
            binding_digest=BINDING_DIGEST,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            run_id=RUN_ID,
            task_id=TASK_ID,
            agent_id=AGENT_ID,
            agent_version_id=AGENT_VERSION_ID,
            tool=FAKE_ECHO_TOOL,
        )
    )
    return stub


@pytest.fixture
async def client(provider: FakeToolProvider):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=provider.app),
        base_url="https://provider.test",
    ) as value:
        yield value


def tool_call(
    *,
    request_id: str = "request_01",
    idempotency_key: str = "runtime-action-7",
    attempt: int = 1,
    arguments: dict | None = None,
    tenant_id: UUID = TENANT_ID,
) -> ProviderToolCallRequest:
    return ProviderToolCallRequest(
        provider_id=PROVIDER_ID,
        binding_digest=BINDING_DIGEST,
        request_id=request_id,
        tool_call_id="tool-call-01",
        idempotency_key=idempotency_key,
        tenant_id=tenant_id,
        project_id=PROJECT_ID,
        run_id=RUN_ID,
        task_id=TASK_ID,
        agent_id=AGENT_ID,
        agent_version_id=AGENT_VERSION_ID,
        tool=FAKE_ECHO_TOOL,
        arguments=arguments or {"message": "hello"},
        deadline=NOW + timedelta(minutes=1),
        attempt=attempt,
        trace=ProviderTrace(trace_id="trace_01", parent_event_id="event_01"),
    )


def cancellation(request_id: str = "request_01") -> ProviderCancelRequest:
    return ProviderCancelRequest(
        provider_id=PROVIDER_ID,
        binding_digest=BINDING_DIGEST,
        request_id=request_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        run_id=RUN_ID,
        reason="run_cancelled",
        deadline=NOW + timedelta(seconds=5),
    )


def signed_headers(
    *,
    method: str,
    path: str,
    body: bytes,
    request_id: str,
    nonce: int,
    secret: str = SECRET,
    timestamp: int | None = None,
    provider_id: str = PROVIDER_ID,
    binding_digest: str = BINDING_DIGEST,
) -> dict[str, str]:
    return sign_hmac_request(
        secret=secret,
        method=method,
        path_with_query=path,
        body=body,
        provider_id=provider_id,
        binding_digest=binding_digest,
        request_id=request_id,
        timestamp=int(NOW.timestamp()) if timestamp is None else timestamp,
        nonce=f"{nonce:032x}",
    ).as_http_headers()


async def post_tool_call(
    client: httpx.AsyncClient,
    request: ProviderToolCallRequest,
    *,
    nonce: int,
) -> httpx.Response:
    body = canonical_json_bytes(request)
    return await client.post(
        "/v1/tool-calls",
        content=body,
        headers={
            **signed_headers(
                method="POST",
                path="/v1/tool-calls",
                body=body,
                request_id=request.request_id,
                nonce=nonce,
            ),
            "Content-Type": "application/json",
        },
    )


@pytest.mark.asyncio
async def test_health_and_capabilities_are_authenticated_v1_contracts(
    provider: FakeToolProvider,
    client: httpx.AsyncClient,
) -> None:
    health_headers = signed_headers(
        method="GET",
        path="/v1/health",
        body=b"",
        request_id="health_01",
        nonce=1,
    )
    health_response = await client.get("/v1/health", headers=health_headers)
    capabilities_headers = signed_headers(
        method="GET",
        path="/v1/capabilities",
        body=b"",
        request_id="capabilities_01",
        nonce=2,
    )
    capabilities_response = await client.get(
        "/v1/capabilities",
        headers=capabilities_headers,
    )

    health = ProviderHealthResponse.model_validate(health_response.json())
    capabilities = ProviderCapabilitiesResponse.model_validate(capabilities_response.json())
    assert health_response.status_code == 200
    assert health.provider_id == PROVIDER_ID
    assert health.status == "healthy"
    assert capabilities_response.status_code == 200
    assert capabilities.supported_tools == (FAKE_ECHO_TOOL,)
    assert capabilities.features.cancellation is True
    assert provider.call_counts == {"health": 1, "capabilities": 1}


@pytest.mark.asyncio
async def test_execute_replays_same_idempotent_request_without_second_execution(
    provider: FakeToolProvider,
    client: httpx.AsyncClient,
) -> None:
    first_request = tool_call()
    first_http = await post_tool_call(client, first_request, nonce=10)
    retry_http = await post_tool_call(
        client,
        first_request.model_copy(update={"attempt": 2}),
        nonce=11,
    )

    first = ProviderToolCallResponse.model_validate(first_http.json())
    replay = ProviderToolCallResponse.model_validate(retry_http.json())
    assert first.status is ProviderToolCallStatus.SUCCEEDED
    assert first.result == {"echo": {"message": "hello"}}
    assert first.idempotency_replayed is False
    assert replay.provider_execution_id == first.provider_execution_id
    assert replay.result == first.result
    assert replay.idempotency_replayed is True
    assert provider.count("execute") == 2
    assert provider.count("executions") == 1
    assert provider.count("idempotency_replay") == 1


@pytest.mark.asyncio
async def test_different_idempotency_keys_execute_independently(
    provider: FakeToolProvider,
    client: httpx.AsyncClient,
) -> None:
    first = tool_call(request_id="independent-1", idempotency_key="independent-1")
    second = tool_call(request_id="independent-2", idempotency_key="independent-2")

    first_response = ProviderToolCallResponse.model_validate(
        (await post_tool_call(client, first, nonce=12)).json()
    )
    second_response = ProviderToolCallResponse.model_validate(
        (await post_tool_call(client, second, nonce=13)).json()
    )

    assert first_response.provider_execution_id != second_response.provider_execution_id
    assert provider.count("executions") == 2


@pytest.mark.asyncio
async def test_idempotency_key_conflict_is_409(
    provider: FakeToolProvider,
    client: httpx.AsyncClient,
) -> None:
    first = tool_call()
    conflict = tool_call(arguments={"message": "different"})

    assert (await post_tool_call(client, first, nonce=20)).status_code == 200
    response = await post_tool_call(client, conflict, nonce=21)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"
    assert provider.count("executions") == 1


@pytest.mark.asyncio
async def test_auth_stale_invalid_missing_and_nonce_replay_are_rejected(
    provider: FakeToolProvider,
    client: httpx.AsyncClient,
) -> None:
    request = tool_call()
    body = canonical_json_bytes(request)
    valid = signed_headers(
        method="POST",
        path="/v1/tool-calls",
        body=body,
        request_id=request.request_id,
        nonce=30,
    )

    missing = await client.post("/v1/tool-calls", content=body)
    invalid = await client.post(
        "/v1/tool-calls",
        content=body,
        headers=signed_headers(
            method="POST",
            path="/v1/tool-calls",
            body=body,
            request_id=request.request_id,
            nonce=31,
            secret="another-contract-secret-with-at-least-32-bytes",
        ),
    )
    stale = await client.post(
        "/v1/tool-calls",
        content=body,
        headers=signed_headers(
            method="POST",
            path="/v1/tool-calls",
            body=body,
            request_id=request.request_id,
            nonce=32,
            timestamp=int((NOW - timedelta(minutes=2)).timestamp()),
        ),
    )
    accepted = await client.post("/v1/tool-calls", content=body, headers=valid)
    replay = await client.post("/v1/tool-calls", content=body, headers=valid)

    assert [missing.status_code, invalid.status_code, stale.status_code, replay.status_code] == [
        401,
        401,
        401,
        401,
    ]
    assert accepted.status_code == 200
    assert provider.count("auth_failure") == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "nonce"),
    [
        ("tenant_id", UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), 40),
        ("project_id", UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"), 41),
        ("run_id", UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"), 42),
        (
            "tool",
            FAKE_ECHO_TOOL.model_copy(update={"name": "nico.stub.wrong-tool"}),
            43,
        ),
    ],
)
async def test_authenticated_scope_mismatch_is_403(
    provider: FakeToolProvider,
    client: httpx.AsyncClient,
    field: str,
    value: object,
    nonce: int,
) -> None:
    request = tool_call().model_copy(update={field: value})
    response = await post_tool_call(client, request, nonce=nonce)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "scope_mismatch"
    assert provider.count("executions") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_status", "error_code"),
    [
        (FakeToolProviderMode.RATE_LIMIT, 429, "rate_limited"),
        (FakeToolProviderMode.SERVER_ERROR, 500, "provider_unavailable"),
        (FakeToolProviderMode.AUTH_FAILURE, 401, "auth_failure"),
    ],
)
async def test_http_failure_injection(
    provider: FakeToolProvider,
    client: httpx.AsyncClient,
    mode: FakeToolProviderMode,
    expected_status: int,
    error_code: str,
) -> None:
    provider.set_mode(mode)

    response = await post_tool_call(client, tool_call(), nonce=50)

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == error_code


@pytest.mark.asyncio
async def test_malformed_and_invalid_output_schema_injection(
    provider: FakeToolProvider,
    client: httpx.AsyncClient,
) -> None:
    provider.set_mode(FakeToolProviderMode.MALFORMED_RESPONSE)
    malformed = await post_tool_call(client, tool_call(request_id="malformed"), nonce=60)
    with pytest.raises(ValueError):
        malformed.json()

    provider.set_mode(FakeToolProviderMode.INVALID_SCHEMA)
    invalid_http = await post_tool_call(
        client,
        tool_call(request_id="invalid-schema", idempotency_key="invalid-schema"),
        nonce=61,
    )
    invalid = ProviderToolCallResponse.model_validate(invalid_http.json())

    assert malformed.status_code == 200
    assert invalid.status is ProviderToolCallStatus.SUCCEEDED
    assert invalid.result == {"unexpected": ["invalid-output-schema"]}
    with pytest.raises(JsonSchemaValidationError):
        validate(invalid.result, FAKE_ECHO_OUTPUT_SCHEMA)


@pytest.mark.asyncio
async def test_delay_and_timeout_envelope_injection(
    provider: FakeToolProvider,
    client: httpx.AsyncClient,
) -> None:
    provider.set_mode(FakeToolProviderMode.DELAY, delay_seconds=0.01)
    delayed = ProviderToolCallResponse.model_validate(
        (await post_tool_call(client, tool_call(request_id="delayed"), nonce=70)).json()
    )
    provider.set_mode(FakeToolProviderMode.TIMEOUT, delay_seconds=0.01)
    timed_out = ProviderToolCallResponse.model_validate(
        (
            await post_tool_call(
                client,
                tool_call(request_id="timed-out", idempotency_key="timed-out"),
                nonce=71,
            )
        ).json()
    )

    assert delayed.status is ProviderToolCallStatus.SUCCEEDED
    assert timed_out.status is ProviderToolCallStatus.TIMED_OUT
    assert timed_out.error is not None
    assert timed_out.error.retryable is True


@pytest.mark.asyncio
async def test_structured_failure_envelope_injection(
    provider: FakeToolProvider,
    client: httpx.AsyncClient,
) -> None:
    provider.set_mode(FakeToolProviderMode.FAILURE)

    response = ProviderToolCallResponse.model_validate(
        (await post_tool_call(client, tool_call(), nonce=75)).json()
    )

    assert response.status is ProviderToolCallStatus.FAILED
    assert response.error is not None
    assert response.error.code == "PROVIDER_COMMAND_FAILED"
    assert response.result is None


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_stops_delayed_execution(
    provider: FakeToolProvider,
    client: httpx.AsyncClient,
) -> None:
    request = tool_call(request_id="cancel-me")
    provider.set_mode(FakeToolProviderMode.DELAY, delay_seconds=0.03)
    execution_task = asyncio.create_task(post_tool_call(client, request, nonce=80))
    await asyncio.sleep(0.005)
    cancel_request = cancellation(request.request_id)
    cancel_body = canonical_json_bytes(cancel_request)

    async def send_cancel(nonce: int) -> httpx.Response:
        path = f"/v1/tool-calls/{request.request_id}/cancel"
        return await client.post(
            path,
            content=cancel_body,
            headers=signed_headers(
                method="POST",
                path=path,
                body=cancel_body,
                request_id=request.request_id,
                nonce=nonce,
            ),
        )

    first_cancel = ProviderCancelResponse.model_validate((await send_cancel(81)).json())
    replay_cancel = ProviderCancelResponse.model_validate((await send_cancel(82)).json())
    execution = ProviderToolCallResponse.model_validate((await execution_task).json())

    assert first_cancel.status == "cancelled"
    assert replay_cancel == first_cancel
    assert execution.status is ProviderToolCallStatus.CANCELLED
    assert provider.count("cancel") == 2
    assert provider.count("cancel_replay") == 1
