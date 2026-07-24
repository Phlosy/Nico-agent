"""Final-action completion policy over already parsed AgentAction facts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from nico_agent.runtime.actions import FinalAction


class CompletionGatePolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: Literal["agent-action-final-v1"] = "agent-action-final-v1"


class CompletionGateFacts(BaseModel):
    model_config = ConfigDict(frozen=True)

    pending_user_input_count: int = Field(default=0, ge=0, le=1_000)


class CompletionGateVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    accepted: bool
    reason_code: str = Field(min_length=1, max_length=120)
    policy_version: str = Field(min_length=1, max_length=40)
    action_id: str = Field(min_length=64, max_length=64)
    pending_user_input_count: int = Field(ge=0, le=1_000)

    def corrective_observation(self) -> dict[str, object]:
        return {
            "type": "completion_gate_observation",
            "trust": "untrusted",
            "accepted": self.accepted,
            "reason_code": self.reason_code,
            "policy_version": self.policy_version,
            "source_action_id": self.action_id,
            "instruction": (
                "A final Action cannot complete while user input is pending. "
                "Return one valid ask_user Action or continue the active workflow."
            ),
        }


def evaluate_final_action(
    action: FinalAction,
    facts: CompletionGateFacts,
    *,
    policy: CompletionGatePolicy | None = None,
) -> CompletionGateVerdict:
    """Decide only whether a valid parsed final may end the Run."""

    active = policy or CompletionGatePolicy()
    if not action.content.strip():
        accepted = False
        reason = "EMPTY_FINAL_CONTENT"
    elif facts.pending_user_input_count:
        accepted = False
        reason = "PENDING_USER_INPUT"
    else:
        accepted = True
        reason = "FINAL_ACCEPTED"
    return CompletionGateVerdict(
        accepted=accepted,
        reason_code=reason,
        policy_version=active.version,
        action_id=action.action_id,
        pending_user_input_count=facts.pending_user_input_count,
    )
