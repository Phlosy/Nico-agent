"""Docker Engine adapter for one-shot, resource-constrained Python execution."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import struct
import time
from typing import Any
from uuid import UUID

import httpx

from nico_agent.config import Settings
from nico_agent.sandbox.contracts import SandboxExecutionRequest, SandboxExecutionResponse
from nico_agent.tools.contracts import canonical_json

_DOCKER_API = "http://docker/v1.43"
_RESULT_MARKER = "NICO_SANDBOX_RESULT:"
_WRAPPER = r"""
import base64
import json
import os
import resource
import sys
import traceback

ORIGINAL_STDOUT = sys.__stdout__

class LimitedWriter:
    def __init__(self, limit):
        self.limit = limit
        self.parts = []
        self.size = 0
        self.truncated = False

    def write(self, value):
        value = str(value)
        encoded = value.encode("utf-8", errors="replace")
        remaining = self.limit - self.size
        if remaining <= 0:
            self.truncated = self.truncated or bool(encoded)
            return len(value)
        accepted = encoded[:remaining]
        self.parts.append(accepted.decode("utf-8", errors="ignore"))
        self.size += len(accepted)
        self.truncated = self.truncated or len(encoded) > remaining
        return len(value)

    def flush(self):
        return None

    def value(self):
        return "".join(self.parts)

payload = json.loads(base64.b64decode(os.environ.pop("NICO_SANDBOX_REQUEST_B64")))
os.environ.clear()
limit = int(payload["output_bytes"])
stdout = LimitedWriter(limit)
stderr = LimitedWriter(limit)
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
namespace = {"__name__": "__main__", "input": payload.get("input"), "result": None}
status = "succeeded"
error = None
sys.stdout = stdout
sys.stderr = stderr
try:
    exec(compile(payload["code"], "<nico-sandbox>", "exec"), namespace, namespace)
    result = namespace.get("result")
    encoded_result = json.dumps(
        result, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded_result) > limit:
        status = "failed"
        result = None
        error = {"type": "ResultTooLarge", "message": "result exceeded its byte limit"}
except BaseException as exc:
    status = "failed"
    result = None
    error = {"type": type(exc).__name__[:100], "message": str(exc)[:1000]}
finally:
    sys.stdout = ORIGINAL_STDOUT
    sys.stderr = sys.__stderr__

envelope = {
    "status": status,
    "result": result,
    "stdout": stdout.value(),
    "stderr": stderr.value(),
    "stdout_truncated": stdout.truncated,
    "stderr_truncated": stderr.truncated,
    "error": error,
}
ORIGINAL_STDOUT.write(
    "NICO_SANDBOX_RESULT:"
    + json.dumps(envelope, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
    + "\n"
)
ORIGINAL_STDOUT.flush()
""".strip()


class SandboxRunnerFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class DockerEngineSandboxRunner:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=settings.sandbox_docker_socket),
            timeout=httpx.Timeout(10.0),
            trust_env=False,
        )
        self._active: dict[UUID, str] = {}
        self._active_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def health(self) -> bool:
        try:
            response = await self.client.get(f"{_DOCKER_API}/_ping")
            return response.status_code == 200 and response.text.strip() == "OK"
        except httpx.HTTPError:
            return False

    async def run(
        self,
        execution_id: UUID,
        request: SandboxExecutionRequest,
    ) -> SandboxExecutionResponse:
        bounded = _bounded_request(request, self.settings)
        payload = container_create_payload(execution_id, bounded, self.settings)
        container_id: str | None = None
        started = time.monotonic()
        timed_out = False
        try:
            response = await self.client.post(
                f"{_DOCKER_API}/containers/create",
                params={"name": f"nico-sandbox-{execution_id}"},
                json=payload,
            )
            if response.status_code == 404:
                raise SandboxRunnerFailure("SANDBOX_IMAGE_UNAVAILABLE")
            _require_docker_status(response, {201})
            container_id = str(response.json()["Id"])
            await self._register(execution_id, container_id)
            response = await self.client.post(f"{_DOCKER_API}/containers/{container_id}/start")
            _require_docker_status(response, {204, 304})
            try:
                async with asyncio.timeout(bounded.wall_time_seconds):
                    response = await self.client.post(
                        f"{_DOCKER_API}/containers/{container_id}/wait",
                        params={"condition": "not-running"},
                        timeout=bounded.wall_time_seconds + 2,
                    )
                    _require_docker_status(response, {200})
            except TimeoutError:
                timed_out = True
                await self._force_remove(container_id)

            duration_ms = max(0, int((time.monotonic() - started) * 1000))
            if timed_out:
                return SandboxExecutionResponse(
                    status="timed_out",
                    error={"type": "Timeout", "message": "sandbox wall time was exceeded"},
                    duration_ms=duration_ms,
                )

            inspect = await self.client.get(f"{_DOCKER_API}/containers/{container_id}/json")
            _require_docker_status(inspect, {200})
            state = inspect.json().get("State", {})
            exit_code = state.get("ExitCode")
            oom_killed = bool(state.get("OOMKilled"))
            logs = await self.client.get(
                f"{_DOCKER_API}/containers/{container_id}/logs",
                params={"stdout": "true", "stderr": "true"},
            )
            _require_docker_status(logs, {200})
            if oom_killed:
                return SandboxExecutionResponse(
                    status="oom_killed",
                    error={"type": "MemoryLimit", "message": "sandbox memory limit was exceeded"},
                    exit_code=exit_code,
                    duration_ms=duration_ms,
                )
            envelope = _parse_envelope(logs.content)
            envelope["exit_code"] = exit_code
            envelope["duration_ms"] = duration_ms
            if exit_code != 0 and envelope.get("status") == "succeeded":
                envelope["status"] = "failed"
                envelope["error"] = {
                    "type": "ProcessExit",
                    "message": "sandbox process exited unsuccessfully",
                }
            return SandboxExecutionResponse.model_validate(envelope)
        except asyncio.CancelledError:
            if container_id is not None:
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._force_remove(container_id))
            raise
        except SandboxRunnerFailure:
            raise
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SandboxRunnerFailure("SANDBOX_RUNNER_UNAVAILABLE") from exc
        finally:
            if container_id is not None:
                with contextlib.suppress(Exception):
                    await self._force_remove(container_id)
                await self._unregister(execution_id, container_id)

    async def cancel(self, execution_id: UUID) -> bool:
        async with self._active_lock:
            container_id = self._active.get(execution_id)
        if container_id is None:
            return False
        await self._force_remove(container_id)
        return True

    async def _register(self, execution_id: UUID, container_id: str) -> None:
        async with self._active_lock:
            if execution_id in self._active:
                raise SandboxRunnerFailure("SANDBOX_EXECUTION_CONFLICT")
            self._active[execution_id] = container_id

    async def _unregister(self, execution_id: UUID, container_id: str) -> None:
        async with self._active_lock:
            if self._active.get(execution_id) == container_id:
                self._active.pop(execution_id, None)

    async def _force_remove(self, container_id: str) -> None:
        try:
            response = await self.client.delete(
                f"{_DOCKER_API}/containers/{container_id}",
                params={"force": "true", "v": "true"},
                timeout=5,
            )
            if response.status_code not in {204, 404, 409}:
                raise SandboxRunnerFailure("SANDBOX_CLEANUP_FAILED")
        except httpx.HTTPError:
            return


def container_create_payload(
    execution_id: UUID,
    request: SandboxExecutionRequest,
    settings: Settings,
) -> dict[str, Any]:
    runtime_request = base64.b64encode(
        canonical_json(
            {
                "code": request.code,
                "input": request.input,
                "output_bytes": request.output_bytes,
            }
        ).encode()
    ).decode("ascii")
    return {
        "Image": settings.sandbox_image,
        "Cmd": ["python", "-I", "-S", "-B", "-c", _WRAPPER],
        "Env": [
            f"NICO_SANDBOX_REQUEST_B64={runtime_request}",
            "PYTHONHASHSEED=0",
            "PYTHONDONTWRITEBYTECODE=1",
        ],
        "User": "65534:65534",
        "WorkingDir": "/tmp",
        "NetworkDisabled": True,
        "OpenStdin": False,
        "StdinOnce": False,
        "Tty": False,
        "Labels": {
            "nico.sandbox": "true",
            "nico.sandbox.execution_id": str(execution_id),
        },
        "StopTimeout": 1,
        "HostConfig": {
            "AutoRemove": False,
            "ReadonlyRootfs": True,
            "Privileged": False,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "NetworkMode": "none",
            "Memory": request.memory_bytes,
            "MemorySwap": request.memory_bytes,
            "MemorySwappiness": 0,
            "NanoCpus": request.nano_cpus,
            "PidsLimit": request.pids_limit,
            "OomKillDisable": False,
            "ShmSize": 16_777_216,
            "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=16m,uid=65534,gid=65534,mode=700"},
            "Ulimits": [
                {"Name": "nofile", "Soft": 64, "Hard": 64},
                {"Name": "core", "Soft": 0, "Hard": 0},
                {"Name": "fsize", "Soft": 1_048_576, "Hard": 1_048_576},
            ],
            "LogConfig": {"Type": "local", "Config": {"max-size": "1m", "max-file": "2"}},
        },
    }


def _bounded_request(
    request: SandboxExecutionRequest, settings: Settings
) -> SandboxExecutionRequest:
    return request.model_copy(
        update={
            "wall_time_seconds": min(request.wall_time_seconds, settings.sandbox_wall_time_seconds),
            "memory_bytes": min(request.memory_bytes, settings.sandbox_memory_bytes),
            "nano_cpus": min(request.nano_cpus, settings.sandbox_nano_cpus),
            "pids_limit": min(request.pids_limit, settings.sandbox_pids_limit),
            "output_bytes": min(request.output_bytes, settings.sandbox_output_bytes),
        }
    )


def _require_docker_status(response: httpx.Response, allowed: set[int]) -> None:
    if response.status_code not in allowed:
        raise SandboxRunnerFailure("SANDBOX_ENGINE_ERROR")


def _parse_envelope(raw_logs: bytes) -> dict[str, Any]:
    decoded = _decode_docker_stream(raw_logs).decode("utf-8", errors="replace")
    marker_index = decoded.rfind(_RESULT_MARKER)
    if marker_index < 0:
        return {
            "status": "failed",
            "error": {
                "type": "MissingResult",
                "message": "sandbox did not return a result envelope",
            },
        }
    lines = decoded[marker_index + len(_RESULT_MARKER) :].splitlines()
    if not lines:
        raise SandboxRunnerFailure("SANDBOX_RESULT_INVALID")
    line = lines[0]
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise SandboxRunnerFailure("SANDBOX_RESULT_INVALID") from exc
    if not isinstance(value, dict):
        raise SandboxRunnerFailure("SANDBOX_RESULT_INVALID")
    return value


def _decode_docker_stream(value: bytes) -> bytes:
    output = bytearray()
    index = 0
    while index + 8 <= len(value):
        stream_type = value[index]
        if stream_type not in {0, 1, 2} or value[index + 1 : index + 4] != b"\x00\x00\x00":
            return value
        length = struct.unpack(">I", value[index + 4 : index + 8])[0]
        start = index + 8
        end = start + length
        if end > len(value):
            return value
        output.extend(value[start:end])
        index = end
    return bytes(output) if index == len(value) else value
