from __future__ import annotations

import pytest

from nico_agent.models.contracts import ModelResponse
from nico_agent.runtime.actions import AskUserAction
from nico_agent.runtime.clarification import (
    ClarificationDecisionKind,
    ClarificationFacts,
    ClarificationPolicy,
    evaluate_clarification,
)
from nico_agent.runtime.native.action_parser import parse_agent_actions


def _ask(
    *,
    candidates: list[tuple[str, float]],
    confidence: float = 0.9,
    ambiguity: float = 0.1,
    risk: str = "low",
    missing: list[str] | None = None,
    safe_partial: bool = False,
) -> AskUserAction:
    batch = parse_agent_actions(
        ModelResponse(
            text=__import__("json").dumps(
                {
                    "type": "ask_user",
                    "question": "Which target?",
                    "reason": "The request has more than one interpretation.",
                    "intent": {
                        "interpreted_intent": "Use the previously discussed target",
                        "confidence": confidence,
                        "candidates": [
                            {
                                "candidate_id": candidate_id,
                                "intent": candidate_id,
                                "confidence": score,
                            }
                            for candidate_id, score in candidates
                        ],
                        "ambiguity": ambiguity,
                        "risk": risk,
                        "risk_reasons": [],
                        "missing_information": missing or [],
                        "safe_partial_answer_possible": safe_partial,
                    },
                }
            ),
            response_format_type="json_schema",
        )
    )
    action = batch.actions[0]
    assert isinstance(action, AskUserAction)
    return action


def test_dominant_low_risk_candidate_rejects_blocking_with_assumption() -> None:
    decision = evaluate_clarification(
        _ask(candidates=[("platform timestamp", 0.9), ("API payload", 0.4)]),
        ClarificationFacts(authoritative_risk="low"),
    )

    assert decision.kind is ClarificationDecisionKind.ANSWER_WITH_ASSUMPTION
    assert decision.reason_code == "DOMINANT_LOW_RISK_INTENT"
    assert decision.interpreted_intent == "Use the previously discussed target"
    assert decision.policy_version == "clarification-v1"


def test_optional_detail_uses_safe_partial_answer_without_blocking() -> None:
    decision = evaluate_clarification(
        _ask(
            candidates=[("summarize", 0.92), ("translate", 0.2)],
            missing=["preferred output length"],
            safe_partial=True,
        ),
        ClarificationFacts(authoritative_risk="low"),
    )

    assert decision.kind is ClarificationDecisionKind.CONTINUE_WITH_PARTIAL_ANSWER
    assert decision.reason_code == "SAFE_PARTIAL_ANSWER_AVAILABLE"


@pytest.mark.parametrize(
    ("action", "facts", "reason"),
    [
        (
            _ask(candidates=[("file", 0.55), ("database", 0.54)], ambiguity=0.9),
            ClarificationFacts(authoritative_risk="low"),
            "NO_DOMINANT_CANDIDATE",
        ),
        (
            _ask(candidates=[("delete run", 0.95), ("delete file", 0.1)]),
            ClarificationFacts(authoritative_risk="high"),
            "MATERIAL_ACTION_RISK",
        ),
        (
            _ask(
                candidates=[("send", 0.9), ("draft", 0.2)],
                missing=["recipient"],
                safe_partial=False,
            ),
            ClarificationFacts(authoritative_risk="low"),
            "INDISPENSABLE_INFORMATION_MISSING",
        ),
        (
            _ask(candidates=[("file", 0.95), ("database", 0.1)]),
            ClarificationFacts(
                authoritative_risk="low",
                pending_obligations=("unresolved tool approval",),
            ),
            "PENDING_OBLIGATION",
        ),
    ],
)
def test_material_ambiguity_risk_missing_input_and_obligations_allow_question(
    action: AskUserAction,
    facts: ClarificationFacts,
    reason: str,
) -> None:
    decision = evaluate_clarification(action, facts)

    assert decision.kind is ClarificationDecisionKind.ALLOW_ASK_USER
    assert decision.reason_code == reason


def test_policy_thresholds_are_versioned_and_boundary_deterministic() -> None:
    policy = ClarificationPolicy()
    accepted = evaluate_clarification(
        _ask(candidates=[("a", 0.75), ("b", 0.5)], ambiguity=0.5),
        ClarificationFacts(authoritative_risk="low"),
        policy=policy,
    )
    below = evaluate_clarification(
        _ask(candidates=[("a", 0.749), ("b", 0.499)], ambiguity=0.5),
        ClarificationFacts(authoritative_risk="low"),
        policy=policy,
    )

    assert accepted.kind is ClarificationDecisionKind.ANSWER_WITH_ASSUMPTION
    assert below.kind is ClarificationDecisionKind.ALLOW_ASK_USER


def test_gate_does_not_classify_question_or_reason_phrases() -> None:
    action = _ask(candidates=[("answer", 0.9), ("other", 0.2)])
    hostile = action.model_copy(
        update={
            "question": "This says delete, confirm, clarify, and are you sure?",
            "reason": "Natural-language phrases are non-authoritative.",
        }
    )

    assert (
        evaluate_clarification(
            hostile,
            ClarificationFacts(authoritative_risk="low"),
        ).kind
        is ClarificationDecisionKind.ANSWER_WITH_ASSUMPTION
    )
