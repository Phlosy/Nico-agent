from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from nico_agent.config import Settings
from nico_agent.database import TenantContext
from nico_agent.net.safe_http import PinnedRequest, RawHttpResponse, SafeHttpClient
from nico_agent.tool_providers.client import ProviderEndpoint, ToolProviderClient
from nico_agent.tool_providers.contracts import (
    ExternalToolProviderCreate,
    ProviderToolCallRequest,
    ProviderToolContract,
    ProviderTrace,
)
from nico_agent.tool_providers.errors import ToolProviderError
from nico_agent.tool_providers.service import ToolProviderService
from nico_agent.tools.secrets import EnvironmentSecretResolver

PROVIDER_ID = UUID("77777777-7777-4777-8777-777777777777")
SECRET = "unit-test-provider-secret-with-at-least-32-bytes"


@dataclass
class _StaticTransport:
    status: int
    body: bytes
    calls: int = 0
    last_request: PinnedRequest | None = None

    async def request(self, request: PinnedRequest) -> RawHttpResponse:
        self.calls += 1
        self.last_request = request
        return RawHttpResponse(status=self.status, headers=(), body=self.body)


def _endpoint() -> ProviderEndpoint:
    return ProviderEndpoint(
        provider_id=PROVIDER_ID,
        endpoint_ref="https://provider.example",
        credential_ref="env:NICO_TOOL_SECRET_PROVIDER_CLIENT",
    )


def _client(transport: _StaticTransport, resolver) -> ToolProviderClient:
    return ToolProviderClient(
        http=SafeHttpClient(transport=transport, resolver=resolver),
        secret_resolver=EnvironmentSecretResolver({"NICO_TOOL_SECRET_PROVIDER_CLIENT": SECRET}),
    )


def _tool_call(*, deadline: datetime) -> ProviderToolCallRequest:
    return ProviderToolCallRequest(
        provider_id=str(PROVIDER_ID),
        binding_digest=f"sha256:{'a' * 64}",
        request_id="deadline-request",
        tool_call_id=str(uuid4()),
        idempotency_key="deadline-key",
        tenant_id=uuid4(),
        project_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        tool=ProviderToolContract(
            name="test.echo",
            version="1.0.0",
            input_schema_digest=f"sha256:{'b' * 64}",
            output_schema_digest=f"sha256:{'c' * 64}",
        ),
        arguments={"message": "hello"},
        deadline=deadline,
        attempt=1,
        trace=ProviderTrace(trace_id="deadline-trace"),
    )


@pytest.mark.asyncio
async def test_client_re_resolves_and_blocks_dns_rebinding_before_transport() -> None:
    answers = iter([("93.184.216.34",), ("127.0.0.1",)])
    transport = _StaticTransport(200, b"{}")
    client = _client(transport, lambda _hostname, _port: next(answers))

    assert await client.validate_endpoint(_endpoint()) == "https://provider.example"
    with pytest.raises(ToolProviderError) as captured:
        await client.health(_endpoint())

    assert captured.value.code == "TOOL_PROVIDER_CONNECTION_ERROR"
    assert captured.value.cause == "ADDRESS_DENIED"
    assert transport.calls == 0


@pytest.mark.asyncio
async def test_registry_maps_unsafe_endpoint_resolution_to_public_provider_error() -> None:
    transport = _StaticTransport(200, b"{}")
    client = _client(transport, lambda _hostname, _port: ("169.254.169.254",))
    service = ToolProviderService(
        cast(Any, object()),
        Settings(environment="test", _env_file=None),
        client=client,
    )

    with pytest.raises(ToolProviderError) as captured:
        await service.register(
            TenantContext(uuid4(), "unit:provider-registry", uuid4()),
            ExternalToolProviderCreate(
                name="unsafe.endpoint",
                endpoint_ref="https://provider.example",
                credential_ref="env:NICO_TOOL_SECRET_PROVIDER_CLIENT",
            ),
        )

    assert captured.value.code == "TOOL_PROVIDER_CONNECTION_ERROR"
    assert captured.value.cause == "ENDPOINT_ADDRESS_DENIED"
    assert transport.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_code", "retryable"),
    [
        (401, "TOOL_PROVIDER_AUTH_ERROR", False),
        (403, "TOOL_PROVIDER_PERMISSION_DENIED", False),
        (429, "TOOL_PROVIDER_RATE_LIMIT", True),
        (500, "TOOL_PROVIDER_CONNECTION_ERROR", True),
    ],
)
async def test_client_maps_http_statuses_to_stable_errors(
    status: int,
    expected_code: str,
    retryable: bool,
) -> None:
    transport = _StaticTransport(status, b'{"error":{"code":"unsafe-provider-detail"}}')
    client = _client(transport, lambda _hostname, _port: ("93.184.216.34",))

    with pytest.raises(ToolProviderError) as captured:
        await client.health(_endpoint())

    assert captured.value.code == expected_code
    assert captured.value.retryable is retryable
    assert captured.value.cause == f"HTTP_{status}"
    assert "unsafe-provider-detail" not in str(captured.value.details)


@pytest.mark.asyncio
async def test_client_rejects_malformed_success_response() -> None:
    transport = _StaticTransport(200, b"{malformed")
    client = _client(transport, lambda _hostname, _port: ("93.184.216.34",))

    with pytest.raises(ToolProviderError) as captured:
        await client.health(_endpoint())

    assert captured.value.code == "TOOL_PROVIDER_INVALID_RESPONSE"
    assert captured.value.cause == "RESPONSE_VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_execute_caps_socket_timeouts_by_protocol_deadline() -> None:
    transport = _StaticTransport(200, b"{}")
    client = _client(transport, lambda _hostname, _port: ("93.184.216.34",))

    with pytest.raises(ToolProviderError):
        await client.execute(
            _endpoint(),
            _tool_call(deadline=datetime.now(UTC) + timedelta(seconds=2)),
        )

    assert transport.last_request is not None
    assert 0 < transport.last_request.connect_timeout <= 2
    assert 0 < transport.last_request.read_timeout <= 2


@pytest.mark.asyncio
async def test_execute_rejects_elapsed_deadline_before_transport() -> None:
    transport = _StaticTransport(200, b"{}")
    client = _client(transport, lambda _hostname, _port: ("93.184.216.34",))

    with pytest.raises(ToolProviderError) as captured:
        await client.execute(
            _endpoint(),
            _tool_call(deadline=datetime.now(UTC) - timedelta(seconds=1)),
        )

    assert captured.value.code == "TOOL_PROVIDER_TIMEOUT"
    assert captured.value.cause == "DEADLINE_ELAPSED"
    assert transport.calls == 0
