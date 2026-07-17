"""Deterministic in-process runtime used for contracts and worker acceptance."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from nico_agent.runtime.contracts import (
    TERMINAL_RUNTIME_STATUSES,
    RuntimeCapability,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeProviderDescriptor,
    RuntimeResult,
    RuntimeSessionHandle,
    RuntimeSessionRequest,
    RuntimeSessionStatus,
    RuntimeTrajectory,
)
from nico_agent.runtime.errors import RuntimeSessionNotFound


@dataclass(slots=True)
class _MockSession:
    request: RuntimeSessionRequest
    status: RuntimeSessionStatus = RuntimeSessionStatus.CREATED
    events: list[RuntimeEvent] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    checkpoint: dict[str, Any] | None = None
    result: RuntimeResult | None = None
    cancel_requested: asyncio.Event = field(default_factory=asyncio.Event)
    resume_gate: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    sequence_offset: int = 0

    def __post_init__(self) -> None:
        self.resume_gate.set()


class MockRuntimeProvider:
    """A fully capable runtime whose behavior comes only from run_config.mock."""

    descriptor = RuntimeProviderDescriptor(
        name="mock",
        version="1.0",
        capabilities=frozenset(RuntimeCapability),
    )

    def __init__(self) -> None:
        self._sessions: dict[str, _MockSession] = {}

    async def create_session(self, request: RuntimeSessionRequest) -> RuntimeSessionHandle:
        external_id = f"mock:{request.run_id}"
        session = self._sessions.get(external_id)
        if session is None:
            session = _MockSession(
                request=request,
                checkpoint=request.checkpoint,
                sequence_offset=request.event_sequence,
            )
            self._sessions[external_id] = session
            await self._emit(
                session,
                RuntimeEventType.SESSION_CREATED,
                payload={"run_id": str(request.run_id)},
            )
        return self._handle(external_id, session)

    async def run(self, external_session_id: str, request: RuntimeSessionRequest) -> RuntimeResult:
        session = self._session(external_session_id)
        if session.request.run_id != request.run_id:
            raise ValueError("runtime request does not match the created session")
        if session.result is not None:
            return session.result
        if session.cancel_requested.is_set():
            return await self._finish_cancelled(session)

        behavior = request.run_config.get("mock", {})
        steps = behavior.get("steps", ["plan", "execute"])
        if not isinstance(steps, list) or not all(isinstance(item, str) for item in steps):
            raise ValueError("run_config.mock.steps must be a list of strings")
        delay = float(behavior.get("delay_seconds", 0))

        session.status = RuntimeSessionStatus.RUNNING
        session.messages.append({"role": "user", "content": request.task_input})
        await self._emit(
            session,
            RuntimeEventType.RUN_RESUMED if request.checkpoint else RuntimeEventType.RUN_STARTED,
        )

        completed_steps = 0
        if request.checkpoint is not None:
            completed_steps = int(request.checkpoint.get("completed_steps", 0))
        for index, step_name in enumerate(steps[completed_steps:], start=completed_steps + 1):
            if not await self._await_control(session, delay):
                return await self._finish_cancelled(session)
            await self._emit(
                session,
                RuntimeEventType.STEP_STARTED,
                payload={"index": index, "name": step_name},
            )
            text = f"{step_name} complete"
            await self._emit(
                session,
                RuntimeEventType.OUTPUT_DELTA,
                message=text,
                payload={"index": index},
            )
            await self._emit(
                session,
                RuntimeEventType.STEP_COMPLETED,
                payload={"index": index, "name": step_name},
            )
            session.checkpoint = {"completed_steps": index}
            await self._emit(
                session,
                RuntimeEventType.CHECKPOINT_SAVED,
                payload=session.checkpoint,
            )

        if behavior.get("fail"):
            error = {
                "code": str(behavior.get("error_code", "MOCK_FAILURE")),
                "message": str(behavior.get("error_message", "mock runtime failed")),
            }
            session.status = RuntimeSessionStatus.FAILED
            session.result = RuntimeResult(
                status=session.status,
                error=error,
                usage={"steps": len(steps)},
                checkpoint=session.checkpoint,
            )
            session.usage = session.result.usage
            await self._emit(session, RuntimeEventType.RUN_FAILED, payload={"error": error})
            return session.result

        output = behavior.get(
            "output",
            {"message": f"Mock completed: {request.task_title}", "input": request.task_input},
        )
        if not isinstance(output, dict):
            raise ValueError("run_config.mock.output must be an object")
        session.status = RuntimeSessionStatus.COMPLETED
        session.messages.append({"role": "assistant", "content": output})
        session.result = RuntimeResult(
            status=session.status,
            output=output,
            usage={"steps": len(steps), "input_units": 1, "output_units": 1},
            checkpoint=session.checkpoint,
        )
        session.usage = session.result.usage
        await self._emit(session, RuntimeEventType.RUN_COMPLETED, payload={"output": output})
        return session.result

    async def pause(self, external_session_id: str) -> RuntimeSessionHandle:
        session = self._session(external_session_id)
        if session.status is RuntimeSessionStatus.RUNNING:
            session.status = RuntimeSessionStatus.PAUSED
            session.resume_gate.clear()
            await self._emit(session, RuntimeEventType.RUN_PAUSED)
        return self._handle(external_session_id, session)

    async def resume(self, external_session_id: str) -> RuntimeSessionHandle:
        session = self._session(external_session_id)
        if session.status is RuntimeSessionStatus.PAUSED:
            session.status = RuntimeSessionStatus.RUNNING
            session.resume_gate.set()
            await self._emit(session, RuntimeEventType.RUN_RESUMED)
        return self._handle(external_session_id, session)

    async def cancel(self, external_session_id: str) -> RuntimeSessionHandle:
        session = self._session(external_session_id)
        if session.status not in TERMINAL_RUNTIME_STATUSES:
            session.cancel_requested.set()
            session.resume_gate.set()
        return self._handle(external_session_id, session)

    async def get_status(self, external_session_id: str) -> RuntimeSessionHandle:
        session = self._session(external_session_id)
        return self._handle(external_session_id, session)

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
        return RuntimeTrajectory(
            provider=self.descriptor.name,
            provider_version=self.descriptor.version,
            external_session_id=external_session_id,
            status=session.status,
            events=list(session.events),
            messages=list(session.messages),
            usage=dict(session.usage),
            metadata={"checkpoint": session.checkpoint},
        )

    def _session(self, external_session_id: str) -> _MockSession:
        try:
            return self._sessions[external_session_id]
        except KeyError as exc:
            raise RuntimeSessionNotFound(external_session_id) from exc

    def _handle(self, external_session_id: str, session: _MockSession) -> RuntimeSessionHandle:
        return RuntimeSessionHandle(
            external_session_id=external_session_id,
            status=session.status,
            capabilities=self.descriptor.capabilities,
            metadata={"checkpoint": session.checkpoint},
        )

    async def _emit(
        self,
        session: _MockSession,
        event_type: RuntimeEventType,
        *,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        event = RuntimeEvent(
            sequence=session.sequence_offset + len(session.events) + 1,
            type=event_type,
            message=message,
            payload=payload or {},
        )
        async with session.changed:
            session.events.append(event)
            session.changed.notify_all()

    async def _await_control(self, session: _MockSession, delay: float) -> bool:
        while not session.resume_gate.is_set():
            if session.cancel_requested.is_set():
                return False
            try:
                await asyncio.wait_for(session.resume_gate.wait(), timeout=0.01)
            except TimeoutError:
                pass
        if delay <= 0:
            return not session.cancel_requested.is_set()
        try:
            await asyncio.wait_for(session.cancel_requested.wait(), timeout=delay)
        except TimeoutError:
            return not session.cancel_requested.is_set()
        return False

    async def _finish_cancelled(self, session: _MockSession) -> RuntimeResult:
        if session.result is None:
            session.status = RuntimeSessionStatus.CANCELLED
            session.result = RuntimeResult(
                status=session.status,
                error={"code": "CANCELLED", "message": "runtime execution was cancelled"},
                checkpoint=session.checkpoint,
            )
            await self._emit(session, RuntimeEventType.RUN_CANCELLED)
        return session.result
