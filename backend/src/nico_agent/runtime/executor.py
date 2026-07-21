"""One-shot persistent Run executor used by the worker and tests."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace

from nico_agent.artifacts.runtime import RunArtifactHandler
from nico_agent.artifacts.service import ArtifactService
from nico_agent.coordination.runtime import RunCoordinationHandler
from nico_agent.coordination.service import CoordinationService
from nico_agent.database import Database
from nico_agent.mcp import McpGatewayHost
from nico_agent.projects.interventions import (
    ProjectInterventionService,
    RunInterventionHandler,
)
from nico_agent.runtime.contracts import (
    AgentRuntimeProvider,
    AgentRuntimeProviderV2,
    RuntimeCapability,
    RuntimeDisposition,
    RuntimeServices,
    execute_provider,
)
from nico_agent.runtime.errors import RuntimeLeaseLost
from nico_agent.runtime.registry import RuntimeProviderRegistry
from nico_agent.runtime.service import PreparedRuntime, RuntimeExecutionService
from nico_agent.runtime.tools import GatewayRuntimeToolHandler
from nico_agent.tools import ToolGateway

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
        tool_gateway: ToolGateway | None = None,
        artifact_service: ArtifactService | None = None,
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
        self.coordination_service = CoordinationService(database)
        self.intervention_service = ProjectInterventionService(database)
        self.tool_gateway = tool_gateway
        self.artifact_service = artifact_service

    async def execute_once(self) -> bool:
        await self.database.reconcile_expired_tool_approvals()
        await self.database.reconcile_coordination_waiters()
        claim = await self.database.claim_next_run(self.worker_id, self.lease_seconds)
        if claim is None:
            return False
        prepared: PreparedRuntime | None = None
        provider: AgentRuntimeProvider | AgentRuntimeProviderV2 | None = None
        mcp_host: McpGatewayHost | None = None
        tool_handler: GatewayRuntimeToolHandler | None = None
        coordination_handler: RunCoordinationHandler | None = None
        artifact_handler: RunArtifactHandler | None = None
        try:
            prepared = await self.service.prepare_claim(
                claim, worker_id=self.worker_id, registry=self.registry
            )
            provider = self.registry.get(prepared.provider_name)
            if RuntimeCapability.COORDINATION in prepared.descriptor.capabilities:
                coordination_handler = RunCoordinationHandler(
                    self.coordination_service,
                    claim,
                    worker_id=self.worker_id,
                )
            if (
                self.artifact_service is not None
                and RuntimeCapability.ARTIFACTS in prepared.descriptor.capabilities
            ):
                artifact_handler = RunArtifactHandler(
                    self.artifact_service,
                    claim,
                    worker_id=self.worker_id,
                )
            if (
                self.tool_gateway is not None
                and RuntimeCapability.PLATFORM_TOOLS in prepared.descriptor.capabilities
            ):
                tool_handler = GatewayRuntimeToolHandler(
                    self.tool_gateway,
                    claim,
                    worker_id=self.worker_id,
                    caller=f"runtime:{prepared.provider_name}",
                )
                mcp_host = McpGatewayHost(tool_handler)
                tool_session = await mcp_host.start()
                prepared = replace(
                    prepared,
                    request=prepared.request.model_copy(update={"tool_session": tool_session}),
                )
            handle = await provider.create_session(prepared.request)
            prepared = replace(prepared, external_session_id=handle.external_session_id)
            await self.service.bind_session(prepared, worker_id=self.worker_id, handle=handle)
            await self._run_provider(
                provider,
                prepared,
                handle.external_session_id,
                tool_handler=tool_handler,
                coordination_handler=coordination_handler,
                artifact_handler=artifact_handler,
            )
        except RuntimeLeaseLost:
            logger.info(
                "runtime lease lost; provider cancellation requested",
                extra={"run_id": str(claim.run_id), "worker_id": self.worker_id},
            )
            if provider is not None and prepared is not None and prepared.external_session_id:
                with contextlib.suppress(Exception):
                    await provider.cancel(prepared.external_session_id)
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
                        message=self._safe_exception_message(exc),
                    )
            else:
                with contextlib.suppress(Exception):
                    await self.service.fail_unprepared_claim(
                        claim,
                        worker_id=self.worker_id,
                        code=getattr(exc, "code", type(exc).__name__.upper()),
                        message=self._safe_exception_message(exc),
                    )
        finally:
            if mcp_host is not None:
                await mcp_host.close()
            if provider is not None and prepared is not None and prepared.external_session_id:
                release_session = getattr(provider, "release_session", None)
                if callable(release_session):
                    with contextlib.suppress(Exception):
                        await release_session(prepared.external_session_id)
        return True

    @staticmethod
    def _safe_exception_message(exc: Exception) -> str:
        domain_message = getattr(exc, "message", None)
        if isinstance(domain_message, str) and domain_message:
            return domain_message[:1000]
        if isinstance(exc, (ValueError, TypeError)):
            return str(exc)[:1000]
        return f"runtime provider raised {type(exc).__name__}"

    async def _run_provider(
        self,
        provider: AgentRuntimeProvider | AgentRuntimeProviderV2,
        prepared: PreparedRuntime,
        external_session_id: str,
        *,
        tool_handler: GatewayRuntimeToolHandler | None,
        coordination_handler: RunCoordinationHandler | None,
        artifact_handler: RunArtifactHandler | None,
    ) -> None:
        intervention_handler = (
            RunInterventionHandler(
                self.intervention_service,
                prepared.claim,
                worker_id=self.worker_id,
            )
            if RuntimeCapability.INTERVENTIONS in prepared.descriptor.capabilities
            else None
        )
        stream_task = asyncio.create_task(
            self._forward_events(provider, prepared, external_session_id)
        )
        heartbeat_task = asyncio.create_task(
            self._heartbeat(provider, prepared, external_session_id)
        )
        try:
            if prepared.timeout_seconds is None:
                outcome = await execute_provider(
                    provider,
                    external_session_id,
                    prepared.request,
                    RuntimeServices(
                        tool_handler=tool_handler,
                        coordination_handler=coordination_handler,
                        artifact_handler=artifact_handler,
                        intervention_handler=intervention_handler,
                    ),
                )
            else:
                async with asyncio.timeout(prepared.timeout_seconds):
                    outcome = await execute_provider(
                        provider,
                        external_session_id,
                        prepared.request,
                        RuntimeServices(
                            tool_handler=tool_handler,
                            coordination_handler=coordination_handler,
                            artifact_handler=artifact_handler,
                            intervention_handler=intervention_handler,
                        ),
                    )
            await stream_task
            trajectory = await provider.export_trajectory(external_session_id)
            if outcome.disposition is RuntimeDisposition.SUSPENDED:
                await self.service.suspend_claim(
                    prepared,
                    worker_id=self.worker_id,
                    checkpoint=outcome.checkpoint or {},
                    wake_condition=outcome.wake_condition or {},
                    usage=outcome.usage,
                    trajectory=trajectory,
                )
            else:
                await self.service.complete_claim(
                    prepared,
                    worker_id=self.worker_id,
                    result=outcome.to_result(),
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
        provider: AgentRuntimeProvider | AgentRuntimeProviderV2,
        prepared: PreparedRuntime,
        external_session_id: str,
    ) -> None:
        async for event in provider.stream_events(
            external_session_id, after_sequence=prepared.request.event_sequence
        ):
            await self.service.record_event(prepared, worker_id=self.worker_id, event=event)

    async def _heartbeat(
        self,
        provider: AgentRuntimeProvider | AgentRuntimeProviderV2,
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
