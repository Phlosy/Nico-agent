"""Small facade wiring growth application services for HTTP dependencies."""

from nico_agent.database import Database
from nico_agent.growth.approval import GrowthApprovalService
from nico_agent.growth.evaluation import GrowthEvaluationService
from nico_agent.growth.read_service import GrowthReadService
from nico_agent.growth.service import GrowthCandidateService
from nico_agent.memory.lifecycle import MemoryLifecycleService
from nico_agent.memory.service import MemoryRetriever
from nico_agent.skills.service import SkillLifecycleService


class GrowthApiService:
    def __init__(self, database: Database) -> None:
        self.read = GrowthReadService(database)
        self.candidates = GrowthCandidateService(database)
        self.evaluations = GrowthEvaluationService(database)
        self.approvals = GrowthApprovalService(database)
        self.memories = MemoryLifecycleService(database)
        self.retrieval = MemoryRetriever(database)
        self.skills = SkillLifecycleService(database)
