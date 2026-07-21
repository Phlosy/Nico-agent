"""One-shot worker for durable Project supervision cycles."""

from __future__ import annotations

import logging

from nico_agent.database import Database
from nico_agent.projects.orchestration import ProjectOrchestrationService

logger = logging.getLogger(__name__)


class ProjectSupervisionWorker:
    def __init__(
        self,
        database: Database,
        *,
        worker_id: str,
        lease_seconds: int = 90,
    ) -> None:
        if not worker_id or len(worker_id) > 200:
            raise ValueError("worker_id must contain between 1 and 200 characters")
        if lease_seconds < 30 or lease_seconds > 3600:
            raise ValueError("lease_seconds must be between 30 and 3600")
        self.database = database
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.service = ProjectOrchestrationService(database)

    async def execute_once(self) -> bool:
        claim = await self.database.claim_next_project_supervision(
            self.worker_id, self.lease_seconds
        )
        if claim is None:
            return False
        try:
            await self.service.materialize_claim(claim, worker_id=self.worker_id)
        except Exception:
            logger.exception(
                "project supervision materialization failed",
                extra={"cycle_id": str(claim.cycle_id), "worker_id": self.worker_id},
            )
            raise
        return True
