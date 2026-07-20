"""Provider-safe adapter from one owned Run lease to CoordinationService."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from nico_agent.coordination.contracts import (
    RuntimeCoordinationIntent,
    RuntimeCoordinationOutcome,
)
from nico_agent.coordination.service import CoordinationService
from nico_agent.database import RunClaim, TenantContext
from nico_agent.domain.errors import DomainConflict


class RunCoordinationHandler:
    def __init__(
        self,
        service: CoordinationService,
        claim: RunClaim,
        *,
        worker_id: str,
    ) -> None:
        self.service = service
        self.claim = claim
        self.worker_id = worker_id
        self.context = TenantContext(
            tenant_id=claim.tenant_id,
            actor_id=f"runtime:{worker_id}",
            correlation_id=uuid5(NAMESPACE_URL, f"nico:coordination:{claim.lease_token}"),
        )

    async def coordinate(self, intent: RuntimeCoordinationIntent) -> RuntimeCoordinationOutcome:
        if intent.action != "delegate" or intent.delegation is None:
            raise DomainConflict(
                "COORDINATION_ACTION_UNSUPPORTED",
                "this runtime handler only accepts delegation intents",
            )
        result = await self.service.delegate(
            self.context,
            self.claim.run_id,
            intent.delegation,
            worker_id=self.worker_id,
            lease_token=self.claim.lease_token,
        )
        return RuntimeCoordinationOutcome(
            action="delegate",
            status="accepted",
            delegation_id=result.delegation_id,
            child_task_id=result.child_task_id,
            child_run_id=result.child_run_id,
            detail={"idempotent_replay": result.idempotent_replay},
        )
