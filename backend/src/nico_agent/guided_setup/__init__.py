"""Authoritative guided-setup readiness and proof contracts."""

from nico_agent.guided_setup.contracts import (
    SetupAreaRead,
    SetupIntentPatch,
    SetupIntentRead,
    SetupReadinessRead,
)
from nico_agent.guided_setup.service import GuidedSetupService

__all__ = [
    "GuidedSetupService",
    "SetupAreaRead",
    "SetupIntentPatch",
    "SetupIntentRead",
    "SetupReadinessRead",
]
