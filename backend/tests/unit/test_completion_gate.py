from __future__ import annotations

import json

from nico_agent.models.contracts import ModelResponse
from nico_agent.runtime.completion_gate import CompletionGateFacts, evaluate_final_action
from nico_agent.runtime.native.action_parser import parse_agent_actions


def _final():
    return parse_agent_actions(
        ModelResponse(
            text=json.dumps({"type": "final", "content": "answer"}),
            response_format_type="json_object",
        )
    ).actions[0]


def test_completion_gate_accepts_a_valid_nonempty_final() -> None:
    verdict = evaluate_final_action(_final(), CompletionGateFacts())

    assert verdict.accepted is True
    assert verdict.reason_code == "FINAL_ACCEPTED"
    assert verdict.policy_version == "agent-action-final-v1"


def test_completion_gate_rejects_completion_while_user_input_is_pending() -> None:
    verdict = evaluate_final_action(
        _final(),
        CompletionGateFacts(pending_user_input_count=1),
    )

    assert verdict.accepted is False
    assert verdict.reason_code == "PENDING_USER_INPUT"
    assert verdict.corrective_observation()["type"] == "completion_gate_observation"
