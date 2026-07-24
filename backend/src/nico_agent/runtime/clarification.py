"""Deterministic policy for deciding whether an AskUserAction may block."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from nico_agent.runtime.actions import AskUserAction

RiskLevel = Literal["low", "medium", "high", "unknown"]


class ClarificationDecisionKind(StrEnum):
    ALLOW_ASK_USER = "allow_ask_user"
    ANSWER_WITH_ASSUMPTION = "answer_with_assumption"
    CONTINUE_WITH_PARTIAL_ANSWER = "continue_with_partial_answer"


class ClarificationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: Literal["clarification-v1"] = "clarification-v1"
    dominant_confidence: float = Field(default=0.75, ge=0, le=1)
    dominant_margin: float = Field(default=0.25, ge=0, le=1)
    maximum_low_risk_ambiguity: float = Field(default=0.5, ge=0, le=1)
    correction_limit: Literal[1] = 1


class ClarificationFacts(BaseModel):
    model_config = ConfigDict(frozen=True)

    authoritative_risk: RiskLevel
    pending_obligations: tuple[str, ...] = Field(default=(), max_length=16)


class ClarificationDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ClarificationDecisionKind
    reason_code: str = Field(min_length=1, max_length=120)
    policy_version: str = Field(min_length=1, max_length=40)
    interpreted_intent: str = Field(min_length=1, max_length=4_000)
    top_candidate_id: str = Field(min_length=1, max_length=120)
    top_confidence: float = Field(ge=0, le=1)
    candidate_margin: float = Field(ge=0, le=1)
    ambiguity: float = Field(ge=0, le=1)
    authoritative_risk: RiskLevel
    missing_information_count: int = Field(ge=0, le=8)
    pending_obligation_count: int = Field(ge=0, le=16)

    def corrective_observation(self) -> dict[str, object]:
        """Bounded, untrusted policy feedback; never composes a domain answer."""

        return {
            "type": "clarification_policy_observation",
            "trust": "untrusted",
            "decision": self.kind.value,
            "reason_code": self.reason_code,
            "policy_version": self.policy_version,
            "interpreted_intent": self.interpreted_intent,
            "top_candidate_id": self.top_candidate_id,
            "top_confidence": self.top_confidence,
            "candidate_margin": self.candidate_margin,
            "instruction": (
                "Answer the interpreted intent directly. State a concise assumption when useful. "
                "Do not ask another question unless new structured facts make blocking necessary."
            ),
        }


def evaluate_clarification(
    action: AskUserAction,
    facts: ClarificationFacts,
    *,
    policy: ClarificationPolicy | None = None,
) -> ClarificationDecision:
    active = policy or ClarificationPolicy()
    ranked = sorted(
        action.intent.candidates,
        key=lambda candidate: (-candidate.confidence, candidate.candidate_id),
    )
    top = ranked[0]
    runner_up = ranked[1].confidence if len(ranked) > 1 else 0.0
    margin = max(0.0, min(1.0, top.confidence - runner_up))

    if facts.pending_obligations:
        kind = ClarificationDecisionKind.ALLOW_ASK_USER
        reason = "PENDING_OBLIGATION"
    elif facts.authoritative_risk != "low":
        kind = ClarificationDecisionKind.ALLOW_ASK_USER
        reason = "MATERIAL_ACTION_RISK"
    elif action.intent.missing_information and not action.intent.safe_partial_answer_possible:
        kind = ClarificationDecisionKind.ALLOW_ASK_USER
        reason = "INDISPENSABLE_INFORMATION_MISSING"
    elif (
        top.confidence < active.dominant_confidence
        or margin < active.dominant_margin
        or action.intent.ambiguity > active.maximum_low_risk_ambiguity
    ):
        kind = ClarificationDecisionKind.ALLOW_ASK_USER
        reason = "NO_DOMINANT_CANDIDATE"
    elif action.intent.safe_partial_answer_possible:
        kind = ClarificationDecisionKind.CONTINUE_WITH_PARTIAL_ANSWER
        reason = "SAFE_PARTIAL_ANSWER_AVAILABLE"
    else:
        kind = ClarificationDecisionKind.ANSWER_WITH_ASSUMPTION
        reason = "DOMINANT_LOW_RISK_INTENT"

    return ClarificationDecision(
        kind=kind,
        reason_code=reason,
        policy_version=active.version,
        interpreted_intent=action.intent.interpreted_intent,
        top_candidate_id=top.candidate_id,
        top_confidence=top.confidence,
        candidate_margin=margin,
        ambiguity=action.intent.ambiguity,
        authoritative_risk=facts.authoritative_risk,
        missing_information_count=len(action.intent.missing_information),
        pending_obligation_count=len(facts.pending_obligations),
    )
