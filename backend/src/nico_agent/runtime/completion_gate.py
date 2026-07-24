"""Shared semantic floor for persisted FinalAction candidates."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from nico_agent.runtime.actions import FinalAction


class CompletionGatePolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: Literal["semantic-completion-v1"] = "semantic-completion-v1"
    correction_limit: Literal[1] = 1
    allow_legacy_plain_text: Literal[False] = False


class CompletionGateFacts(BaseModel):
    model_config = ConfigDict(frozen=True)

    pending_user_input_count: int = Field(default=0, ge=0, le=1_000)


class CompletionGateVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    accepted: bool
    reason_code: str = Field(min_length=1, max_length=120)
    policy_version: str = Field(min_length=1, max_length=40)
    action_id: str = Field(min_length=64, max_length=64)
    interpreted_intent: str = Field(min_length=1, max_length=4_000)
    compatibility_mode: bool
    answered_user_intent: bool
    requires_user_response: bool
    pending_user_input_count: int = Field(ge=0, le=1_000)

    def corrective_observation(self) -> dict[str, object]:
        """Return metadata correction guidance, never a domain answer."""

        return {
            "type": "semantic_completion_observation",
            "trust": "untrusted",
            "accepted": self.accepted,
            "reason_code": self.reason_code,
            "policy_version": self.policy_version,
            "source_action_id": self.action_id,
            "interpreted_intent": self.interpreted_intent,
            "instruction": (
                "Return one structured final Action that truthfully states whether the "
                "interpreted intent was answered and whether user input is still required. "
                "If user input is required, return ask_user instead of final. Do not reuse "
                "legacy plain text."
            ),
        }


def evaluate_final_action(
    action: FinalAction,
    facts: CompletionGateFacts,
    *,
    policy: CompletionGatePolicy | None = None,
) -> CompletionGateVerdict:
    active = policy or CompletionGatePolicy()
    if action.compatibility_mode and not active.allow_legacy_plain_text:
        accepted = False
        reason = "FINAL_METADATA_REQUIRED"
    elif facts.pending_user_input_count:
        accepted = False
        reason = "PENDING_USER_INPUT"
    elif action.completion.answered_user_intent and action.completion.requires_user_response:
        accepted = False
        reason = "CONTRADICTORY_FINAL_METADATA"
    elif action.completion.requires_user_response:
        accepted = False
        reason = "USER_RESPONSE_REQUIRED"
    elif not action.completion.answered_user_intent:
        accepted = False
        reason = "USER_INTENT_UNANSWERED"
    else:
        accepted = True
        reason = "SEMANTIC_FINAL_ACCEPTED"
    return CompletionGateVerdict(
        accepted=accepted,
        reason_code=reason,
        policy_version=active.version,
        action_id=action.action_id,
        interpreted_intent=action.intent.interpreted_intent,
        compatibility_mode=action.compatibility_mode,
        answered_user_intent=action.completion.answered_user_intent,
        requires_user_response=action.completion.requires_user_response,
        pending_user_input_count=facts.pending_user_input_count,
    )
