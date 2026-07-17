"""One-shot persistent Run executor used by the worker and tests."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from nico_agent.database import Database
from nico_agent.runtime.contracts import AgentRuntimeProvider
from nico_agent.runtime.registry import RuntimeProviderRegistry
from nico_agent.runtime.service import PreparedRuntime, RuntimeExecutionService

logger = logging.getLogger(__name__)


class RuntimeWorker:
    def __init__(
        self,
        database: Database,
        registry: RuntimeProviderRegistry,
        *,
        worker_id: str,
        lease_seconds: int = 30,
        heartbeat_seconds: float = 10,
    ) -> None:
        if lease_seconds < 5:
            raise ValueError("lease_seconds must be at least 5")
        if heartbeat_seconds <= 0 or heartbeat_seconds >= lease_seconds:
            raise ValueError("heartbeat_seconds must be positive and shorter than the lease")
        self.database = database
        self.registry = registry
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.service = RuntimeExecutionService(database)

    async def execute_once(self) -> bool:
        claim = await self.database.claim_next_run(self.worker_id, self.lease_seconds)
        if claim is None:
            return False
        prepared: PreparedRuntime | None = None
        provider: AgentRuntimeProvider | None = None
        try:
            prepared = await self.service.prepare_claim(
                claim, worker_id=self.worker_id, registry=self.registry
            )
            provider = self.registry.get(prepared.provider_name)
            handle = await provider.create_session(prepared.request)
            prepared = PreparedRuntime(
                claim=prepared.claim,
                provider_name=prepared.provider_name,
                descriptor=prepared.descriptor,
                request=prepared.request,
                external_session_id=handle.external_session_id,
                recovering=prepared.recovering,
                timeout_seconds=prepared.timeout_seconds,
            )
            await self.service.bind_session(prepared, worker_id=self.worker_id, handle=handle)
            await self._run_provider(provider, prepared, handle.external_session_id)
        except Exception as exc:
            logger.exception(
                "runtime execution failed",
                extra={"run_id": str(claim.run_id), "worker_id": self.worker_id},
            )
            if provider is not None and prepared is not None and prepared.external_session_id:
                with contextlib.suppress(Exception):
                    await provider.cancel(prepared.external_session_id)
            if prepared is not None:
                with contextlib.suppress(Exception):
                    await self.service.fail_claim(
                        prepared,
                        worker_id=self.worker_id,
                        code=getattr(exc, "code", type(exc).__name__.upper()),
                        message=str(exc),
                    )
            else:
                with contextlib.suppress(Exception):
                    await self.service.fail_unprepared_claim(
                        claim,
                        worker_id=self.worker_id,
                        code=getattr(exc, "code", type(exc).__name__.upper()),
                        message=str(exc),
                    )
        return True

    async def _run_provider(
        self,
        provider: AgentRuntimeProvider,
        prepared: PreparedRuntime,
        external_session_id: str,
    ) -> None:
        stream_task = asyncio.create_task(
            self._forward_events(provider, prepared, external_session_id)
        )
        heartbeat_task = asyncio.create_task(
            self._heartbeat(provider, prepared, external_session_id)
        )
        try:
            if prepared.timeout_seconds is None:
                result = await provider.run(external_session_id, prepared.request)
            else:
                async with asyncio.timeout(prepared.timeout_seconds):
                    result = await provider.run(external_session_id, prepared.request)
            await stream_task
            trajectory = await provider.export_trajectory(external_session_id)
            await self.service.complete_claim(
                prepared,
                worker_id=self.worker_id,
                result=result,
                trajectory=trajectory,
            )
        except TimeoutError:
            await provider.cancel(external_session_id)
            await self.service.fail_claim(
                prepared,
                worker_id=self.worker_id,
                code="RUNTIME_TIMEOUT",
                message="runtime execution exceeded its timeout",
                timed_out=True,
            )
        finally:
            heartbeat_task.cancel()
            if not stream_task.done():
                stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            with contextlib.suppress(asyncio.CancelledError):
                await stream_task

    async def _forward_events(
        self,
        provider: AgentRuntimeProvider,
        prepared: PreparedRuntime,
        external_session_id: str,
    ) -> None:
        async for event in provider.stream_events(
            external_session_id, after_sequence=prepared.request.event_sequence
        ):
            await self.service.record_event(prepared, worker_id=self.worker_id, event=event)

    async def _heartbeat(
        self,
        provider: AgentRuntimeProvider,
        prepared: PreparedRuntime,
        external_session_id: str,
    ) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            owned = await self.service.heartbeat(
                prepared.claim,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
            )
            if not owned:
                await provider.cancel(external_session_id)
                return
