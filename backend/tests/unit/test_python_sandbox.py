from __future__ import annotations

import base64
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from nico_agent.config import Settings
from nico_agent.sandbox import (
    SandboxExecutionRequest,
    SandboxExecutionResponse,
    container_create_payload,
)
from nico_agent.sandbox.api import create_sandbox_runner_app
from nico_agent.tools import ToolExecutionContext
from nico_agent.tools.builtin import PythonSandboxExecutor, SandboxRunnerClient
from nico_agent.tools.errors import ToolExecutorFailure


class FakeSandboxClient:
    def __init__(self, response: SandboxExecutionResponse) -> None:
        self.response = response
        self.execution_ids = []
        self.requests = []

    async def execute(self, execution_id, request):
        self.execution_ids.append(execution_id)
        self.requests.append(request)
        return self.response


class FakeRunner:
    def __init__(self) -> None:
        self.runs = []
        self.cancelled = []

    async def health(self):
        return True

    async def run(self, execution_id, request):
        self.runs.append((execution_id, request))
        return SandboxExecutionResponse(status="succeeded", result=7, duration_ms=1, exit_code=0)

    async def cancel(self, execution_id):
        self.cancelled.append(execution_id)
        return True

    async def close(self):
        return None


def test_python_tool_definition_hash_is_process_stable() -> None:
    executor = PythonSandboxExecutor(
        FakeSandboxClient(SandboxExecutionResponse(status="succeeded", duration_ms=0))
    )

    assert (
        executor.spec.content_hash
        == "749ff98d115e11be6d65b3a9c48f86682383cd12418d4908d84762d04c7e6f4a"
    )


def _context(config=None) -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id=uuid4(),
        run_id=uuid4(),
        run_step_id=uuid4(),
        actor_id="sandbox-test",
        correlation_id=uuid4(),
        tool_config=config or {},
    )


def test_sandbox_request_accepts_finite_json_and_rejects_sensitive_or_large_input() -> None:
    request = SandboxExecutionRequest(code="result = input['value'] * 2", input={"value": 3})
    assert request.input == {"value": 3}

    for value in (
        {"api_key": "do-not-send"},
        {"nested": [{"access_token": "do-not-send"}]},
        {"value": float("nan")},
        {"value": "x" * 100_001},
    ):
        with pytest.raises(ValidationError):
            SandboxExecutionRequest(code="result = input", input=value)


def test_container_payload_is_digest_pinned_and_has_mandatory_isolation() -> None:
    settings = Settings(_env_file=None)
    execution_id = uuid4()
    request = SandboxExecutionRequest(
        code="result = {'ok': True}",
        input={"numbers": [1, 2]},
        wall_time_seconds=3,
        memory_bytes=67_108_864,
        nano_cpus=200_000_000,
        pids_limit=8,
        output_bytes=4096,
    )

    payload = container_create_payload(execution_id, request, settings)

    assert "@sha256:" in payload["Image"]
    assert payload["User"] == "65534:65534"
    assert payload["NetworkDisabled"] is True
    assert payload["OpenStdin"] is False
    assert payload["Labels"]["nico.sandbox.execution_id"] == str(execution_id)
    host = payload["HostConfig"]
    assert host["ReadonlyRootfs"] is True
    assert host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["NetworkMode"] == "none"
    assert host["Memory"] == host["MemorySwap"] == 67_108_864
    assert host["NanoCpus"] == 200_000_000
    assert host["PidsLimit"] == 8
    assert host["Tmpfs"]["/tmp"].startswith("rw,noexec,nosuid,nodev")
    assert "Binds" not in host and "Mounts" not in payload
    request_environment = next(
        item.split("=", 1)[1]
        for item in payload["Env"]
        if item.startswith("NICO_SANDBOX_REQUEST_B64=")
    )
    decoded = json.loads(base64.b64decode(request_environment))
    assert decoded == {
        "code": "result = {'ok': True}",
        "input": {"numbers": [1, 2]},
        "output_bytes": 4096,
    }
    assert all("SECRET" not in item and "PASSWORD" not in item for item in payload["Env"])


@pytest.mark.asyncio
async def test_python_executor_restricts_agent_limits_and_returns_result() -> None:
    response = SandboxExecutionResponse(
        status="succeeded",
        result={"total": 6},
        stdout="calculated\n",
        stderr="",
        duration_ms=12,
        exit_code=0,
    )
    client = FakeSandboxClient(response)
    executor = PythonSandboxExecutor(
        client,
        wall_time_seconds=10,
        memory_bytes=100_000_000,
        pids_limit=20,
        output_bytes=10_000,
    )

    result = await executor.execute(
        _context(
            {
                "wall_time_seconds": 2,
                "memory_bytes": 200_000_000,
                "pids_limit": 5,
                "output_bytes": 2000,
            }
        ),
        {"code": "print('calculated'); result = {'total': sum(input)}", "input": [1, 2, 3]},
        {},
    )

    request = client.requests[0]
    assert request.wall_time_seconds == 2
    assert request.memory_bytes == 100_000_000
    assert request.pids_limit == 5
    assert request.output_bytes == 2000
    assert result.output["success"] is True
    assert result.output["result"] == {"total": 6}
    assert result.usage == {"duration_ms": 12, "exit_code": 0}


@pytest.mark.asyncio
async def test_python_executor_preserves_user_failure_but_raises_resource_failures() -> None:
    failed = PythonSandboxExecutor(
        FakeSandboxClient(
            SandboxExecutionResponse(
                status="failed",
                error={"type": "ValueError", "message": "bad input"},
                duration_ms=1,
                exit_code=0,
            )
        )
    )
    result = await failed.execute(_context(), {"code": "raise ValueError('bad input')"}, {})
    assert result.output["success"] is False
    assert result.output["error"]["type"] == "ValueError"

    for status, code in (("timed_out", "PYTHON_TIMEOUT"), ("oom_killed", "PYTHON_MEMORY_LIMIT")):
        executor = PythonSandboxExecutor(
            FakeSandboxClient(SandboxExecutionResponse(status=status, duration_ms=100))
        )
        with pytest.raises(ToolExecutorFailure) as captured:
            await executor.execute(_context(), {"code": "pass"}, {})
        assert captured.value.code == code


@pytest.mark.asyncio
async def test_runner_client_authenticates_and_never_exposes_error_body() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("Authorization")
        return httpx.Response(503, json={"detail": "docker socket /private/path secret"})

    client = SandboxRunnerClient(
        "http://sandbox-runner:8090",
        "private-runner-token",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ToolExecutorFailure) as captured:
        await client.execute(uuid4(), SandboxExecutionRequest(code="pass"))

    assert seen["authorization"] == "Bearer private-runner-token"
    assert captured.value.code == "SANDBOX_RUNNER_REJECTED"
    assert "/private/path" not in captured.value.message


@pytest.mark.asyncio
async def test_runner_api_requires_bearer_token_for_execute_and_cancel() -> None:
    settings = Settings(
        _env_file=None,
        sandbox_runner_token="runner-test-token-long-enough",
    )
    runner = FakeRunner()
    app = create_sandbox_runner_app(settings, runner)
    transport = httpx.ASGITransport(app=app)
    execution_id = uuid4()
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        denied = await client.post(f"/v1/executions/{execution_id}", json={"code": "result = 7"})
        accepted = await client.post(
            f"/v1/executions/{execution_id}",
            headers={"Authorization": "Bearer runner-test-token-long-enough"},
            json={"code": "result = 7"},
        )
        cancelled = await client.delete(
            f"/v1/executions/{execution_id}",
            headers={"Authorization": "Bearer runner-test-token-long-enough"},
        )

    assert denied.status_code == 401
    assert accepted.status_code == 200 and accepted.json()["result"] == 7
    assert cancelled.status_code == 204
    assert runner.runs[0][0] == execution_id
    assert runner.cancelled == [execution_id]
