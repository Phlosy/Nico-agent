from __future__ import annotations

import json

import pytest

from nico_agent.models.contracts import ModelResponse
from nico_agent.runtime.completion_gate import (
    CompletionGateFacts,
    CompletionGatePolicy,
    evaluate_final_action,
)
from nico_agent.runtime.native.action_parser import parse_agent_actions


def _final(
    *,
    answered: bool = True,
    requires_user: bool = False,
    legacy: bool = False,
):
    if legacy:
        return parse_agent_actions(
            ModelResponse(text="legacy answer"),
            allow_legacy_plain_text=True,
        ).actions[0]
    return parse_agent_actions(
        ModelResponse(
            text=json.dumps(
                {
                    "type": "final",
                    "content": "answer",
                    "intent": {
                        "interpreted_intent": "answer the user",
                        "confidence": 0.9,
                        "candidates": [
                            {
                                "candidate_id": "answer",
                                "intent": "answer the user",
                                "confidence": 0.9,
                            }
                        ],
                        "ambiguity": 0.1,
                        "risk": "low",
                        "risk_reasons": [],
                        "missing_information": [],
                        "safe_partial_answer_possible": True,
                    },
                    "completion": {
                        "answered_user_intent": answered,
                        "requires_user_response": requires_user,
                    },
                }
            )
        )
    ).actions[0]


def test_accepts_truthful_structured_final() -> None:
    verdict = evaluate_final_action(_final(), CompletionGateFacts())

    assert verdict.accepted is True
    assert verdict.reason_code == "SEMANTIC_FINAL_ACCEPTED"
    assert verdict.policy_version == "semantic-completion-v1"


@pytest.mark.parametrize(
    ("answered", "requires_user", "reason"),
    [
        (False, True, "USER_RESPONSE_REQUIRED"),
        (False, False, "USER_INTENT_UNANSWERED"),
        (True, True, "CONTRADICTORY_FINAL_METADATA"),
    ],
)
def test_rejects_pseudo_final(
    answered: bool,
    requires_user: bool,
    reason: str,
) -> None:
    verdict = evaluate_final_action(
        _final(answered=answered, requires_user=requires_user),
        CompletionGateFacts(),
    )

    assert verdict.accepted is False
    assert verdict.reason_code == reason
    assert verdict.corrective_observation()["type"] == "semantic_completion_observation"
    assert "answer" not in verdict.corrective_observation()


def test_pending_user_input_overrides_claimed_completion() -> None:
    verdict = evaluate_final_action(
        _final(),
        CompletionGateFacts(pending_user_input_count=1),
    )

    assert verdict.accepted is False
    assert verdict.reason_code == "PENDING_USER_INPUT"


def test_legacy_plain_text_cannot_bypass_metadata_floor() -> None:
    verdict = evaluate_final_action(_final(legacy=True), CompletionGateFacts())

    assert CompletionGatePolicy().allow_legacy_plain_text is False
    assert verdict.accepted is False
    assert verdict.reason_code == "FINAL_METADATA_REQUIRED"
