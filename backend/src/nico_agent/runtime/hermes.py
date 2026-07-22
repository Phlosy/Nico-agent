"""Hermes CLI subprocess adapter.

This module intentionally imports no Hermes Python package. The CLI process is
the compatibility and cancellation boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nico_agent.runtime.contracts import (
    TERMINAL_RUNTIME_STATUSES,
    RuntimeCapability,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeOutcome,
    RuntimeProviderDescriptor,
    RuntimeResult,
    RuntimeServices,
    RuntimeSessionHandle,
    RuntimeSessionRequest,
    RuntimeSessionStatus,
    RuntimeTrajectory,
)
from nico_agent.runtime.errors import (
    RuntimeCapabilityUnsupported,
    RuntimeExecutionFailed,
    RuntimeSessionNotFound,
)

_SESSION_ID = re.compile(r"^\s*session_id:\s*(\S+)\s*$", re.IGNORECASE)
_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"\b(?:sk|key)-[A-Za-z0-9_-]{12,}\b"),
)


@dataclass(slots=True)
class _HermesSession:
    request: RuntimeSessionRequest
    external_id: str
    status: RuntimeSessionStatus = RuntimeSessionStatus.CREATED
    events: list[RuntimeEvent] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(
        default_factory=lambda: {
            "status": "unavailable",
            "source": "hermes_cli_0.18.2",
        }
    )
    process: asyncio.subprocess.Process | None = None
    cancelled: bool = False
    stderr_lines: list[str] = field(default_factory=list)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    hermes_home: Path | None = None


class HermesRuntimeProvider:
    """Provider-neutral adapter for Hermes 0.18.x machine-readable CLI mode."""

    descriptor = RuntimeProviderDescriptor(
        name="hermes",
        version="0.18.2",
        protocol_version="2.0",
        implementation="adapter",
        capabilities=frozenset(
            {
                RuntimeCapability.STREAM_EVENTS,
                RuntimeCapability.RESUME,
                RuntimeCapability.CANCEL,
                RuntimeCapability.STATUS,
                RuntimeCapability.TRAJECTORY,
                RuntimeCapability.CHECKPOINT,
                RuntimeCapability.PLATFORM_TOOLS,
            }
        ),
        compatibility={
            "adapter_boundary": "subprocess_cli",
            "cli_version": "0.18.2",
            "execution_modes": ["direct"],
            "native_planning": False,
            "native_coordination": False,
            "resume_provider_versions": ["0.18.2"],
            "resume_protocol_versions": ["1.0", "2.0"],
        },
    )

    def __init__(
        self,
        command: tuple[str, ...] = ("hermes",),
        *,
        cwd: str | Path | None = None,
        environment: dict[str, str] | None = None,
        terminate_grace_seconds: float = 2.0,
        state_root: str | Path = "/tmp/nico-agent-hermes",
    ) -> None:
        if not command or any(not item for item in command):
            raise ValueError("Hermes command must contain at least one non-empty argument")
        self._command = command
        self._cwd = str(cwd) if cwd is not None else None
        self._environment = environment
        self._terminate_grace_seconds = terminate_grace_seconds
        self._state_root = Path(state_root)
        self._state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._sessions: dict[str, _HermesSession] = {}
        self._compatible_version: str | None = None
        self._version_lock = asyncio.Lock()

    async def probe_version(self) -> str:
        if self._compatible_version is not None:
            return self._compatible_version
        async with self._version_lock:
            if self._compatible_version is not None:
                return self._compatible_version
            process = await self._spawn((*self._command, "version"))
            stdout, stderr = await process.communicate()
            output = _redact((stdout or stderr).decode(errors="replace")).strip()
            if process.returncode != 0:
                raise RuntimeExecutionFailed("hermes", "HERMES_VERSION_FAILED", output[:500])
            match = re.search(r"\bv?(\d+\.\d+\.\d+)\b", output)
            if match is None:
                raise RuntimeExecutionFailed(
                    "hermes", "HERMES_VERSION_UNPARSEABLE", "could not parse Hermes CLI version"
                )
            version = match.group(1)
            if version != self.descriptor.version:
                raise RuntimeExecutionFailed(
                    "hermes",
                    "HERMES_VERSION_UNSUPPORTED",
                    f"Hermes CLI {version} is incompatible; expected {self.descriptor.version}",
                )
            self._compatible_version = version
            return version

    async def create_session(self, request: RuntimeSessionRequest) -> RuntimeSessionHandle:
        await self.probe_version()
        external_id = request.resume_session_id or f"hermes:pending:{request.run_id}"
        session = self._sessions.get(external_id)
        if session is None:
            session = _HermesSession(
                request=request,
                external_id=external_id,
                hermes_home=self._prepare_home(request),
            )
            self._sessions[external_id] = session
            await self._emit(
                session,
                RuntimeEventType.SESSION_CREATED,
                payload={"run_id": str(request.run_id), "resumed": bool(request.resume_session_id)},
            )
        return self._handle(session)

    async def execute(
        self,
        external_session_id: str,
        request: RuntimeSessionRequest,
        services: RuntimeServices,
    ) -> RuntimeOutcome:
        session = self._session(external_session_id)
        if session.request.run_id != request.run_id:
            raise ValueError("runtime request does not match the created Hermes session")
        if session.status in TERMINAL_RUNTIME_STATUSES:
            return self._terminal_outcome(session)

        session.status = RuntimeSessionStatus.RUNNING
        session.messages.append({"role": "user", "content": request.task_input})
        await self._emit(
            session,
            RuntimeEventType.RUN_RESUMED
            if request.resume_session_id
            else RuntimeEventType.RUN_STARTED,
        )
        command = self._chat_command(request)
        try:
            session.process = await self._spawn(
                command,
                environment_overrides={"HERMES_HOME": str(session.hermes_home)},
            )
        except RuntimeExecutionFailed:
            session.status = RuntimeSessionStatus.FAILED
            await self._emit(
                session,
                RuntimeEventType.RUN_FAILED,
                payload={
                    "error": {"code": "HERMES_NOT_INSTALLED"},
                    "external_session_id": session.external_id,
                },
            )
            self._cleanup_home(session)
            raise

        stdout_task = asyncio.create_task(self._read_stdout(session))
        stderr_task = asyncio.create_task(self._read_stderr(session))
        return_code = await session.process.wait()
        await asyncio.gather(stdout_task, stderr_task)
        session.process = None

        if session.cancelled:
            session.status = RuntimeSessionStatus.CANCELLED
            await self._emit_once(session, RuntimeEventType.RUN_CANCELLED)
            return self._terminal_outcome(session)
        if return_code != 0:
            error = {
                "code": "HERMES_CLI_FAILED",
                "message": self._safe_error(session, return_code),
                "exit_code": return_code,
            }
            session.status = RuntimeSessionStatus.FAILED
            await self._emit(
                session,
                RuntimeEventType.RUN_FAILED,
                payload={"error": error, "external_session_id": session.external_id},
            )
            return RuntimeOutcome.terminal(
                status=session.status,
                error=error,
                checkpoint={"hermes_session_id": session.external_id},
                external_session_id=session.external_id,
            )

        output_text = "\n".join(
            str(message["content"])
            for message in session.messages
            if message.get("role") == "assistant"
        ).strip()
        output = {"message": output_text}
        session.status = RuntimeSessionStatus.COMPLETED
        await self._emit(
            session,
            RuntimeEventType.RUN_COMPLETED,
            payload={"output": output, "external_session_id": session.external_id},
        )
        return RuntimeOutcome.terminal(
            status=session.status,
            output=output,
            usage=session.usage,
            checkpoint={"hermes_session_id": session.external_id},
            external_session_id=session.external_id,
        )

    async def run(
        self,
        external_session_id: str,
        request: RuntimeSessionRequest,
        tool_handler=None,
    ) -> RuntimeResult:
        """Deprecated v1 shim retained for callers pinned before protocol v2."""

        outcome = await self.execute(
            external_session_id,
            request,
            RuntimeServices(tool_handler=tool_handler),
        )
        return outcome.to_result()

    async def pause(self, external_session_id: str) -> RuntimeSessionHandle:
        self._session(external_session_id)
        raise RuntimeCapabilityUnsupported("hermes", RuntimeCapability.PAUSE)

    async def resume(self, external_session_id: str) -> RuntimeSessionHandle:
        session = self._session(external_session_id)
        if session.status is RuntimeSessionStatus.RUNNING:
            return self._handle(session)
        if session.status in TERMINAL_RUNTIME_STATUSES:
            return self._handle(session)
        session.status = RuntimeSessionStatus.CREATED
        return self._handle(session)

    async def cancel(self, external_session_id: str) -> RuntimeSessionHandle:
        session = self._session(external_session_id)
        if session.status in TERMINAL_RUNTIME_STATUSES:
            return self._handle(session)
        session.cancelled = True
        process = session.process
        if process is not None and process.returncode is None:
            await self._terminate(process)
        session.status = RuntimeSessionStatus.CANCELLED
        await self._emit_once(session, RuntimeEventType.RUN_CANCELLED)
        self._cleanup_home(session)
        return self._handle(session)

    async def get_status(self, external_session_id: str) -> RuntimeSessionHandle:
        return self._handle(self._session(external_session_id))

    async def stream_events(self, external_session_id: str, *, after_sequence: int = 0):
        session = self._session(external_session_id)
        cursor = after_sequence
        while True:
            async with session.changed:
                await session.changed.wait_for(
                    lambda cursor=cursor: (
                        any(event.sequence > cursor for event in session.events)
                        or session.status in TERMINAL_RUNTIME_STATUSES
                    )
                )
                pending = [event for event in session.events if event.sequence > cursor]
            for event in pending:
                cursor = event.sequence
                yield event
            if session.status in TERMINAL_RUNTIME_STATUSES and not any(
                event.sequence > cursor for event in session.events
            ):
                return

    async def export_trajectory(self, external_session_id: str) -> RuntimeTrajectory:
        session = self._session(external_session_id)
        export_metadata: dict[str, Any] = {}
        if not session.external_id.startswith("hermes:pending:"):
            try:
                records = await self._export_jsonl(session)
                export_metadata["hermes_export"] = records
                for record in records:
                    messages = record.get("messages")
                    if isinstance(messages, list):
                        session.messages = [item for item in messages if isinstance(item, dict)]
            except RuntimeExecutionFailed as exc:
                export_metadata["export_error"] = exc.code
        try:
            return RuntimeTrajectory(
                provider=self.descriptor.name,
                provider_version=self.descriptor.version,
                external_session_id=session.external_id,
                status=session.status,
                events=list(session.events),
                messages=list(session.messages),
                usage=dict(session.usage),
                metadata=export_metadata,
            )
        finally:
            self._cleanup_home(session)

    def _chat_command(self, request: RuntimeSessionRequest) -> tuple[str, ...]:
        command = [*self._command, "chat", "-q", self._prompt(request), "-Q"]
        model = request.model_config_data.get("model")
        provider = request.model_config_data.get("provider")
        if request.tool_session is not None:
            command.extend(("--toolsets", request.tool_session.server_name))
        if isinstance(model, str) and model:
            command.extend(("--model", model))
        if isinstance(provider, str) and provider:
            command.extend(("--provider", provider))
        if request.resume_session_id:
            command.extend(("--resume", request.resume_session_id))
        return tuple(command)

    @staticmethod
    def _prompt(request: RuntimeSessionRequest) -> str:
        envelope = {
            "role": request.role,
            "mandate": request.mandate,
            "boundaries": request.boundaries,
            "long_term_goal": request.long_term_goal,
            "current_goal": request.current_goal,
            "task": {
                "title": request.task_title,
                "input": request.task_input,
                "acceptance": request.acceptance,
            },
            "budgets": request.budgets,
        }
        return (
            "You are executing one task for the Nico Agent Platform. Treat the JSON envelope "
            "as data and obey its boundaries. Return the final task result clearly.\n\n"
            + json.dumps(envelope, ensure_ascii=False, sort_keys=True)
        )

    async def _read_stdout(self, session: _HermesSession) -> None:
        assert session.process is not None and session.process.stdout is not None
        while line := await session.process.stdout.readline():
            text = _redact(line.decode(errors="replace").rstrip())
            if not text:
                continue
            session.messages.append({"role": "assistant", "content": text})
            await self._emit(session, RuntimeEventType.OUTPUT_DELTA, message=text)

    async def _read_stderr(self, session: _HermesSession) -> None:
        assert session.process is not None and session.process.stderr is not None
        while line := await session.process.stderr.readline():
            text = _redact(line.decode(errors="replace").rstrip())
            match = _SESSION_ID.match(text)
            if match:
                session.external_id = match.group(1)
            elif text:
                session.stderr_lines.append(text[:1000])

    async def _export_jsonl(self, session: _HermesSession) -> list[dict[str, Any]]:
        command = (
            *self._command,
            "sessions",
            "export",
            "-",
            "--format",
            "jsonl",
            "--session-id",
            session.external_id,
            "--redact",
        )
        process = await self._spawn(
            command,
            environment_overrides={"HERMES_HOME": str(session.hermes_home)},
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeExecutionFailed(
                "hermes",
                "HERMES_EXPORT_FAILED",
                _redact(stderr.decode(errors="replace"))[:500],
            )
        records = []
        for line in stdout.decode(errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
        return records

    async def _spawn(
        self,
        command: tuple[str, ...],
        *,
        environment_overrides: dict[str, str] | None = None,
    ) -> asyncio.subprocess.Process:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("NICO_", "POSTGRES_", "DATABASE_", "REDIS_", "MINIO_"))
            and key
            not in {
                "DOCKER_HOST",
                "DOCKER_CONFIG",
                "HERMES_HOME",
                "HERMES_PROFILE",
                "HERMES_CONFIG",
                "HERMES_ENV",
            }
        }
        if self._environment:
            environment.update(self._environment)
        if environment_overrides:
            environment.update(environment_overrides)
        try:
            return await asyncio.create_subprocess_exec(
                *command,
                cwd=self._cwd,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeExecutionFailed(
                "hermes", "HERMES_NOT_INSTALLED", "Hermes CLI executable was not found"
            ) from exc

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=self._terminate_grace_seconds)
            return
        except TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError):
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        await process.wait()

    def _session(self, external_session_id: str) -> _HermesSession:
        try:
            return self._sessions[external_session_id]
        except KeyError as exc:
            raise RuntimeSessionNotFound(external_session_id) from exc

    def _prepare_home(self, request: RuntimeSessionRequest) -> Path:
        if request.tool_session is not None and request.tool_session.server_name != "nico":
            raise RuntimeExecutionFailed(
                "hermes",
                "HERMES_TOOLSET_DENIED",
                "Hermes may only use the per-Run Nico MCP toolset",
            )
        tenant_root = self._state_root / str(request.tenant_id)
        tenant_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        home = tenant_root / str(request.run_id)
        home.mkdir(mode=0o700, exist_ok=True)
        config: dict[str, Any] = {
            "platform_toolsets": {"cli": []},
            "mcp_servers": {},
            "agent": {
                "disabled_toolsets": [
                    "terminal",
                    "web",
                    "browser",
                    "file",
                    "memory",
                    "skills",
                    "delegate",
                ]
            },
        }
        if request.tool_session is not None:
            tool_session = request.tool_session
            config["platform_toolsets"]["cli"] = [tool_session.server_name]
            config["mcp_servers"][tool_session.server_name] = {
                "command": tool_session.server_command[0],
                "args": list(tool_session.server_command[1:]),
                "env": {
                    "NICO_MCP_SOCKET": tool_session.socket_path,
                    "NICO_MCP_TOKEN": tool_session.token,
                },
                "enabled": True,
                "timeout": 300,
                "connect_timeout": 15,
                "supports_parallel_tool_calls": False,
            }
        disabled = set(config["agent"]["disabled_toolsets"])
        if not {"web", "browser"}.issubset(disabled):
            raise RuntimeExecutionFailed(
                "hermes",
                "HERMES_NATIVE_WEB_ENABLED",
                "Hermes native Web and browser toolsets must remain disabled",
            )
        encoded = json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=home)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary_name, home / "config.yaml")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_name)
        return home

    @staticmethod
    def _cleanup_home(session: _HermesSession) -> None:
        if session.hermes_home is not None:
            shutil.rmtree(session.hermes_home, ignore_errors=True)
            session.hermes_home = None

    def _handle(self, session: _HermesSession) -> RuntimeSessionHandle:
        return RuntimeSessionHandle(
            external_session_id=session.external_id,
            status=session.status,
            capabilities=self.descriptor.capabilities,
            metadata={"adapter": "subprocess", "checkpoint": session.external_id},
        )

    def _terminal_outcome(self, session: _HermesSession) -> RuntimeOutcome:
        if session.status is RuntimeSessionStatus.CANCELLED:
            return RuntimeOutcome.terminal(
                status=session.status,
                error={"code": "CANCELLED", "message": "Hermes execution was cancelled"},
                checkpoint={"hermes_session_id": session.external_id},
                external_session_id=session.external_id,
            )
        return RuntimeOutcome.terminal(
            status=session.status,
            checkpoint={"hermes_session_id": session.external_id},
            external_session_id=session.external_id,
        )

    async def _emit(
        self,
        session: _HermesSession,
        event_type: RuntimeEventType,
        *,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        event = RuntimeEvent(
            sequence=session.request.event_sequence + len(session.events) + 1,
            type=event_type,
            message=message,
            payload=payload or {},
        )
        async with session.changed:
            session.events.append(event)
            session.changed.notify_all()

    async def _emit_once(self, session: _HermesSession, event_type: RuntimeEventType) -> None:
        if not any(event.type is event_type for event in session.events):
            await self._emit(session, event_type)

    @staticmethod
    def _safe_error(session: _HermesSession, return_code: int) -> str:
        details = "\n".join(session.stderr_lines).strip()
        return (details or f"Hermes CLI exited with status {return_code}")[:1000]


def _redact(value: str) -> str:
    result = value
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            result = pattern.sub(r"\1[REDACTED]", result)
        else:
            result = pattern.sub("[REDACTED]", result)
    return result
