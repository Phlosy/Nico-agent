"""Content-addressed, explicitly shared runtime artifacts."""

from nico_agent.artifacts.contracts import RuntimeArtifactIntent, RuntimeArtifactOutcome
from nico_agent.artifacts.service import ArtifactService

__all__ = ["ArtifactService", "RuntimeArtifactIntent", "RuntimeArtifactOutcome"]
