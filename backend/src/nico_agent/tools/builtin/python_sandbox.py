"""Gateway executor and authenticated client for the independent Python Sandbox Runner."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import httpx
from pydantic import ValidationError

from nico_agent.sandbox.contracts import SandboxExecutionRequest, SandboxExecutionResponse
from nico_agent.tools.contracts import (
    ToolDefinitionSpec,
    ToolExecutionResult,
    ToolIsolation,
    ToolRetryPolicy,
    ToolRisk,
    canonical_hash,
)
from nico_agent.tools.errors import ToolExecutorFailure


class SandboxRunnerClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._transport = transport

    async def execute(
        self,
        execution_id: UUID,
        request: SandboxExecutionRequest,
    ) -> SandboxExecutionResponse:
        timeout = request.wall_time_seconds + 10
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(timeout),
                trust_env=False,
            ) as client:
                try:
                    response = await client.post(
                        f"{self.base_url}/v1/executions/{execution_id}",
                        headers=self._headers,
                        json=request.model_dump(mode="json"),
                    )
                except asyncio.CancelledError:
                    try:
                        async with asyncio.timeout(3):
                            await asyncio.shield(
                                client.delete(
                                    f"{self.base_url}/v1/executions/{execution_id}",
                                    headers=self._headers,
                                )
                            )
                    except Exception:
                        pass
                    raise
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException as exc:
            raise ToolExecutorFailure(
                "SANDBOX_RUNNER_TIMEOUT", "Sandbox Runner did not respond in time"
            ) from exc
        except httpx.HTTPError as exc:
            raise ToolExecutorFailure(
                "SANDBOX_RUNNER_UNAVAILABLE", "Sandbox Runner is unavailable"
            ) from exc
        if response.status_code != 200:
            raise ToolExecutorFailure(
                "SANDBOX_RUNNER_REJECTED", "Sandbox Runner rejected the execution"
            )
        try:
            return SandboxExecutionResponse.model_validate(response.json())
        except (ValidationError, ValueError) as exc:
            raise ToolExecutorFailure(
                "SANDBOX_RESPONSE_INVALID", "Sandbox Runner returned an invalid response"
            ) from exc

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}


class PythonSandboxExecutor:
    spec = ToolDefinitionSpec(
        name="python.execute",
        version="1.0.0",
        description="Execute Python in a one-shot, networkless, resource-constrained container",
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "minLength": 1, "maxLength": 50000},
                "input": {},
            },
            "required": ["code"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "result": {},
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
                "stdout_truncated": {"type": "boolean"},
                "stderr_truncated": {"type": "boolean"},
                "error": {"type": ["object", "null"]},
            },
            "required": [
                "success",
                "result",
                "stdout",
                "stderr",
                "stdout_truncated",
                "stderr_truncated",
                "error",
            ],
            "additionalProperties": False,
        },
        permission="code.python.execute",
        timeout_seconds=40,
        retry_policy=ToolRetryPolicy(
            max_attempts=2,
            backoff_seconds=0.2,
            retryable_codes=frozenset({"SANDBOX_RUNNER_TIMEOUT", "SANDBOX_RUNNER_UNAVAILABLE"}),
        ),
        isolation=ToolIsolation.CONTAINER,
        risk=ToolRisk.HIGH,
        max_output_bytes=1_048_576,
    )
    implementation_hash = canonical_hash({"executor": "python.execute", "revision": 1})

    def __init__(
        self,
        client: SandboxRunnerClient,
        *,
        wall_time_seconds: int = 10,
        memory_bytes: int = 134_217_728,
        nano_cpus: int = 500_000_000,
        pids_limit: int = 32,
        output_bytes: int = 65_536,
    ) -> None:
        self.client = client
        self.wall_time_seconds = wall_time_seconds
        self.memory_bytes = memory_bytes
        self.nano_cpus = nano_cpus
        self.pids_limit = pids_limit
        self.output_bytes = output_bytes

    async def execute(self, context, arguments, secrets):
        try:
            request = SandboxExecutionRequest(
                code=arguments["code"],
                input=arguments.get("input"),
                wall_time_seconds=_restrict_int(
                    context.tool_config.get("wall_time_seconds"), self.wall_time_seconds
                ),
                memory_bytes=_restrict_int(
                    context.tool_config.get("memory_bytes"), self.memory_bytes
                ),
                nano_cpus=_restrict_int(context.tool_config.get("nano_cpus"), self.nano_cpus),
                pids_limit=_restrict_int(context.tool_config.get("pids_limit"), self.pids_limit),
                output_bytes=_restrict_int(
                    context.tool_config.get("output_bytes"), self.output_bytes
                ),
            )
        except ValidationError as exc:
            raise ToolExecutorFailure(
                "PYTHON_INPUT_INVALID", "Python sandbox input is invalid"
            ) from exc
        response = await self.client.execute(uuid4(), request)
        if response.status == "timed_out":
            raise ToolExecutorFailure("PYTHON_TIMEOUT", "Python execution exceeded its wall time")
        if response.status == "oom_killed":
            raise ToolExecutorFailure(
                "PYTHON_MEMORY_LIMIT", "Python execution exceeded its memory limit"
            )
        return ToolExecutionResult(
            output={
                "success": response.status == "succeeded",
                "result": response.result,
                "stdout": response.stdout,
                "stderr": response.stderr,
                "stdout_truncated": response.stdout_truncated,
                "stderr_truncated": response.stderr_truncated,
                "error": response.error,
            },
            usage={
                "duration_ms": response.duration_ms,
                "exit_code": response.exit_code,
            },
        )


def _restrict_int(value: Any, platform_limit: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return min(value, platform_limit)
    return platform_limit
