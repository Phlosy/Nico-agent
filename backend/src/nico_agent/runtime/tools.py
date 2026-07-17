"""Runtime-neutral adapter from normalized tool intents to the Tool Gateway."""

from __future__ import annotations

from nico_agent.database import RunClaim
from nico_agent.domain.states import ToolCallStatus
from nico_agent.runtime.contracts import (
    RuntimeToolIntent,
    RuntimeToolOutcome,
    RuntimeToolSpec,
)
from nico_agent.tools import ToolGateway, ToolGatewayRequest


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
            ),
        )
        status = {
            ToolCallStatus.SUCCEEDED: "succeeded",
            ToolCallStatus.TIMED_OUT: "timed_out",
            ToolCallStatus.CANCELLED: "cancelled",
        }.get(result.status, "failed")
        return RuntimeToolOutcome(
            call_id=intent.call_id,
            status=status,
            output=result.output,
            error=result.error,
            usage=result.usage,
        )
