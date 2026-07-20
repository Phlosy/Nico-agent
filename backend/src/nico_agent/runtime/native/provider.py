"""In-process Nico native Runtime Provider v2."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from nico_agent.models.gateway import ModelGateway
from nico_agent.runtime.contracts import (
    TERMINAL_RUNTIME_STATUSES,
    RuntimeCapability,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeOutcome,
    RuntimeProviderDescriptor,
    RuntimeServices,
    RuntimeSessionHandle,
    RuntimeSessionRequest,
    RuntimeSessionStatus,
    RuntimeTrajectory,
)
from nico_agent.runtime.errors import RuntimeSessionNotFound
from nico_agent.runtime.native.loop import NativeAgentLoop


@dataclass(slots=True)
class _NativeSession:
    request: RuntimeSessionRequest
    status: RuntimeSessionStatus = RuntimeSessionStatus.CREATED
    events: list[RuntimeEvent] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    outcome: RuntimeOutcome | None = None
    cancel_requested: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    sequence_offset: int = 0


class NicoNativeRuntimeProvider:
    descriptor = RuntimeProviderDescriptor(
        name="nico_native",
        version="0.2.0",
        protocol_version="2.0",
        implementation="native",
        capabilities=frozenset(
            {
                RuntimeCapability.STREAM_EVENTS,
                RuntimeCapability.CANCEL,
                RuntimeCapability.STATUS,
                RuntimeCapability.TRAJECTORY,
                RuntimeCapability.CHECKPOINT,
                RuntimeCapability.RESUME,
                RuntimeCapability.PLATFORM_TOOLS,
                RuntimeCapability.PLANNING,
                RuntimeCapability.REFLECTION,
                RuntimeCapability.COORDINATION,
                RuntimeCapability.ARTIFACTS,
            }
        ),
        compatibility={
            "execution_modes": ["direct", "react", "plan_and_execute"],
            "resume_provider_versions": ["0.2.0"],
            "resume_protocol_versions": ["2.0"],
        },
    )

    def __init__(self, gateway: ModelGateway, *, post_tool_delay_seconds: float = 0) -> None:
        self.loop = NativeAgentLoop(
            gateway,
            post_tool_delay_seconds=post_tool_delay_seconds,
        )
        self._sessions: dict[str, _NativeSession] = {}

    async def create_session(self, request: RuntimeSessionRequest) -> RuntimeSessionHandle:
        # Native recovery is checkpoint-based. A fresh per-attempt identity keeps
        # a late cancel/release from the previous lease owner from reaching the
        # replacement execution in this process.
        external_id = f"nico:{request.run_id}:{uuid4().hex}"
        session = _NativeSession(
            request=request,
            sequence_offset=request.event_sequence,
        )
        self._sessions[external_id] = session
        await self._emit(
            session,
            RuntimeEventType.SESSION_CREATED,
            None,
            {"run_id": str(request.run_id), "protocol_version": "2.0"},
        )
        return self._handle(external_id, session)

    async def execute(
        self,
        external_session_id: str,
        request: RuntimeSessionRequest,
        services: RuntimeServices,
    ) -> RuntimeOutcome:
        session = self._session(external_session_id)
        if session.request.run_id != request.run_id:
            raise ValueError("runtime request does not match the created session")
        if session.outcome is not None:
            return session.outcome
        session.status = RuntimeSessionStatus.RUNNING
        await self._emit(
            session,
            RuntimeEventType.RUN_RESUMED if request.checkpoint else RuntimeEventType.RUN_STARTED,
            None,
            {},
        )

        async def emit(
            event_type: RuntimeEventType,
            message: str | None,
            payload: dict[str, Any],
        ) -> None:
            await self._emit(session, event_type, message, payload)

        outcome = await self.loop.execute(
            request,
            services=services,
            emit=emit,
            cancelled=session.cancel_requested.is_set,
        )
        session.outcome = outcome
        session.status = outcome.status
        session.usage = outcome.usage
        async with session.changed:
            session.changed.notify_all()
        if outcome.output is not None:
            session.messages.append({"role": "assistant", "content": outcome.output})
        return outcome

    async def pause(self, external_session_id: str) -> RuntimeSessionHandle:
        session = self._session(external_session_id)
        if session.status is RuntimeSessionStatus.RUNNING:
            session.status = RuntimeSessionStatus.PAUSED
            await self._emit(session, RuntimeEventType.RUN_PAUSED, None, {})
        return self._handle(external_session_id, session)

    async def resume(self, external_session_id: str) -> RuntimeSessionHandle:
        session = self._session(external_session_id)
        if session.status is RuntimeSessionStatus.PAUSED:
            session.status = RuntimeSessionStatus.RUNNING
            await self._emit(session, RuntimeEventType.RUN_RESUMED, None, {})
        return self._handle(external_session_id, session)

    async def cancel(self, external_session_id: str) -> RuntimeSessionHandle:
        session = self._session(external_session_id)
        if session.status not in TERMINAL_RUNTIME_STATUSES:
            session.cancel_requested.set()
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
                        or session.status is RuntimeSessionStatus.SUSPENDED
                    )
                )
                pending = [event for event in session.events if event.sequence > cursor]
            for event in pending:
                cursor = event.sequence
                yield event
            if session.status in TERMINAL_RUNTIME_STATUSES | {RuntimeSessionStatus.SUSPENDED}:
                if not any(event.sequence > cursor for event in session.events):
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
            metadata={"protocol_version": self.descriptor.protocol_version},
        )

    async def release_session(self, external_session_id: str) -> None:
        self._sessions.pop(external_session_id, None)

    def _session(self, external_session_id: str) -> _NativeSession:
        try:
            return self._sessions[external_session_id]
        except KeyError as exc:
            raise RuntimeSessionNotFound(external_session_id) from exc

    def _handle(self, external_session_id: str, session: _NativeSession) -> RuntimeSessionHandle:
        return RuntimeSessionHandle(
            external_session_id=external_session_id,
            status=session.status,
            capabilities=self.descriptor.capabilities,
            metadata={"protocol_version": self.descriptor.protocol_version},
        )

    async def _emit(
        self,
        session: _NativeSession,
        event_type: RuntimeEventType,
        message: str | None,
        payload: dict[str, Any],
    ) -> None:
        terminal_status = {
            RuntimeEventType.RUN_COMPLETED: RuntimeSessionStatus.COMPLETED,
            RuntimeEventType.RUN_FAILED: RuntimeSessionStatus.FAILED,
            RuntimeEventType.RUN_CANCELLED: RuntimeSessionStatus.CANCELLED,
        }.get(event_type)
        if terminal_status is not None:
            session.status = terminal_status
        event = RuntimeEvent(
            sequence=session.sequence_offset + len(session.events) + 1,
            type=event_type,
            message=message,
            payload=payload,
        )
        async with session.changed:
            session.events.append(event)
            session.changed.notify_all()
