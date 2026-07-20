"""Provider-safe Artifact handler bound to one claimed Run."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from nico_agent.artifacts.contracts import RuntimeArtifactIntent, RuntimeArtifactOutcome
from nico_agent.artifacts.service import ArtifactService
from nico_agent.database import RunClaim, TenantContext


class RunArtifactHandler:
    def __init__(
        self,
        service: ArtifactService,
        claim: RunClaim,
        *,
        worker_id: str,
    ) -> None:
        self.service = service
        self.claim = claim
        self.context = TenantContext(
            tenant_id=claim.tenant_id,
            actor_id=f"runtime:{worker_id}",
            correlation_id=uuid5(NAMESPACE_URL, f"nico:artifact:{claim.lease_token}"),
        )

    async def store_artifact(self, intent: RuntimeArtifactIntent) -> RuntimeArtifactOutcome:
        return await self.service.store_runtime_artifact(
            self.context,
            self.claim.run_id,
            intent,
        )
