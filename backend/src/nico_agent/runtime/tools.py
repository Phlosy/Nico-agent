"""Runtime-neutral adapter from normalized tool intents to the Tool Gateway."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nico_agent.database import RunClaim
from nico_agent.domain.states import ToolCallStatus
from nico_agent.runtime.contracts import (
    RuntimeActionBatchState,
    RuntimeActionOutcome,
    RuntimeToolIntent,
    RuntimeToolOutcome,
    RuntimeToolSpec,
)
from nico_agent.tools import ToolGateway, ToolGatewayRequest

if TYPE_CHECKING:
    from nico_agent.runtime.service import RuntimeExecutionService


class RunActionHandler:
    """Lease-bound persistence barrier and cursor authority for Native Actions."""

    def __init__(
        self,
        service: RuntimeExecutionService,
        claim: RunClaim,
        *,
        worker_id: str,
        commit_timeout_seconds: float = 5,
    ) -> None:
        self.service = service
        self.claim = claim
        self.worker_id = worker_id
        self.commit_timeout_seconds = commit_timeout_seconds

    async def wait_for_batch(self, batch_key: str) -> RuntimeActionBatchState:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.commit_timeout_seconds
        while True:
            state = await self.service.get_agent_action_batch_state(
                self.claim,
                worker_id=self.worker_id,
                batch_key=batch_key,
            )
            if state is not None:
                return state
            if loop.time() >= deadline:
                raise RuntimeError("AgentAction batch commit barrier timed out")
            # Yield to the provider Event forwarder that owns batch projection.
            await asyncio.sleep(0.01)

    async def begin_action(
        self,
        batch_key: str,
        *,
        ordinal: int,
        action_id: str,
    ) -> RuntimeActionBatchState:
        return await self.service.begin_agent_action(
            self.claim,
            worker_id=self.worker_id,
            batch_key=batch_key,
            ordinal=ordinal,
            action_id=action_id,
        )

    async def complete_action(
        self,
        batch_key: str,
        *,
        ordinal: int,
        action_id: str,
        outcome: RuntimeActionOutcome,
    ) -> RuntimeActionBatchState:
        return await self.service.complete_agent_action(
            self.claim,
            worker_id=self.worker_id,
            batch_key=batch_key,
            ordinal=ordinal,
            action_id=action_id,
            outcome=outcome,
        )

    async def release_action(
        self,
        batch_key: str,
        *,
        ordinal: int,
        action_id: str,
    ) -> RuntimeActionBatchState:
        return await self.service.release_agent_action(
            self.claim,
            worker_id=self.worker_id,
            batch_key=batch_key,
            ordinal=ordinal,
            action_id=action_id,
        )


class GatewayRuntimeToolHandler:
    def __init__(
        self,
        gateway: ToolGateway,
        claim: RunClaim,
        *,
        worker_id: str,
        caller: str,
    ) -> None:
        self.gateway = gateway
        self.claim = claim
        self.worker_id = worker_id
        self.caller = caller

    async def list_tools(self) -> tuple[RuntimeToolSpec, ...]:
        definitions = await self.gateway.list_authorized(
            self.claim,
            worker_id=self.worker_id,
        )
        return tuple(
            RuntimeToolSpec(
                name=spec.name,
                version=spec.version,
                description=spec.description,
                input_schema=spec.input_schema,
            )
            for spec in definitions
        )

    async def execute_tool(self, intent: RuntimeToolIntent) -> RuntimeToolOutcome:
        result = await self.gateway.execute(
            self.claim,
            worker_id=self.worker_id,
            request=ToolGatewayRequest(
                tool_name=intent.name,
                tool_version=intent.version,
                arguments=intent.arguments,
                idempotency_key=intent.idempotency_key,
                caller=self.caller,
                checkpoint=intent.checkpoint,
            ),
        )
        status = {
            ToolCallStatus.SUCCEEDED: "succeeded",
            ToolCallStatus.TIMED_OUT: "timed_out",
            ToolCallStatus.CANCELLED: "cancelled",
        }.get(result.status, "failed")
        return RuntimeToolOutcome(
            call_id=intent.call_id,
            tool_call_id=str(result.tool_call_id),
            run_step_id=str(result.run_step_id),
            status=status,
            output=result.output,
            error=result.error,
            usage=result.usage,
            cached=result.cached,
        )
