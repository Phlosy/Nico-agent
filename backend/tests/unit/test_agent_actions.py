from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from nico_agent.models.contracts import ModelResponse, ModelToolCall
from nico_agent.runtime.actions import (
    AgentActionKind,
    AskUserAction,
    FinalAction,
    ToolCallAction,
)
from nico_agent.runtime.native.action_parser import (
    AgentActionParseError,
    agent_action_json_schema,
    agent_action_response_format,
    parse_agent_actions,
    parse_compatibility_agent_actions,
)


def test_live_action_schema_includes_ask_user_only_when_gate_and_handler_are_enabled() -> None:
    from nico_agent.runtime.native.action_parser import active_agent_action_kinds

    assert AgentActionKind.ASK_USER not in active_agent_action_kinds(
        clarification_gate_enabled=False,
        user_input_handler_enabled=True,
    )
    assert AgentActionKind.ASK_USER not in active_agent_action_kinds(
        clarification_gate_enabled=True,
        user_input_handler_enabled=False,
    )
    assert AgentActionKind.ASK_USER in active_agent_action_kinds(
        clarification_gate_enabled=True,
        user_input_handler_enabled=True,
    )


def _intent() -> dict:
    return {
        "interpreted_intent": "Configure the second option discussed earlier",
        "confidence": 0.82,
        "candidates": [
            {
                "candidate_id": "second-option",
                "intent": "Configure the second option",
                "confidence": 0.82,
            },
            {
                "candidate_id": "first-option",
                "intent": "Configure the first option",
                "confidence": 0.18,
            },
        ],
        "ambiguity": 0.18,
        "risk": "low",
        "risk_reasons": [],
        "missing_information": [],
        "safe_partial_answer_possible": True,
    }


def _final_payload() -> dict:
    return {
        "type": "final",
        "content": "Configure the second option with the documented defaults.",
        "intent": _intent(),
        "completion": {
            "answered_user_intent": True,
            "requires_user_response": False,
        },
    }


def _ask_payload() -> dict:
    intent = _intent()
    intent.update(
        {
            "interpreted_intent": "Delete one of several recently discussed objects",
            "confidence": 0.45,
            "ambiguity": 0.8,
            "risk": "high",
            "risk_reasons": ["Deletion is irreversible without a backup."],
            "missing_information": ["Exact object type and identifier"],
            "safe_partial_answer_possible": False,
        }
    )
    return {
        "type": "ask_user",
        "question": "Which exact object should be deleted?",
        "reason": "Several targets are similarly plausible and deletion is high risk.",
        "intent": intent,
    }


def _parse_payload(payload: dict, *, structured_output: bool = False):
    return parse_agent_actions(
        ModelResponse(
            text=json.dumps(payload, ensure_ascii=False),
            structured_output=structured_output,
        )
    )


def test_structured_final_and_ask_user_actions_are_immutable_and_bounded() -> None:
    final_batch = _parse_payload(_final_payload())
    ask_batch = _parse_payload(_ask_payload())

    final = final_batch.actions[0]
    ask = ask_batch.actions[0]
    assert isinstance(final, FinalAction)
    assert final.completion.answered_user_intent is True
    assert final.compatibility_mode is False
    assert isinstance(ask, AskUserAction)
    assert ask.intent.risk == "high"
    assert ask.intent.missing_information == ("Exact object type and identifier",)
    assert len(final.action_id) == len(ask.action_id) == 64
    with pytest.raises(ValidationError):
        final.content = "mutated"


@pytest.mark.parametrize(
    ("path", "code"),
    [
        (("question",), "ACTION_METADATA_INVALID"),
        (("reason",), "ACTION_METADATA_INVALID"),
        (("intent", "candidates"), "ACTION_METADATA_INVALID"),
        (("intent", "ambiguity"), "ACTION_METADATA_INVALID"),
        (("intent", "risk"), "ACTION_METADATA_INVALID"),
        (("intent", "missing_information"), "ACTION_METADATA_INVALID"),
        (("intent", "confidence"), "ACTION_METADATA_INVALID"),
    ],
)
def test_ask_user_requires_complete_policy_metadata(
    path: tuple[str, ...],
    code: str,
) -> None:
    payload = _ask_payload()
    target = payload
    for key in path[:-1]:
        target = target[key]
    target.pop(path[-1])

    with pytest.raises(AgentActionParseError) as captured:
        _parse_payload(payload)

    assert captured.value.code == code


def test_final_preserves_contradictory_metadata_for_completion_gate() -> None:
    payload = _final_payload()
    payload["completion"]["requires_user_response"] = True

    batch = _parse_payload(payload)

    assert batch.actions[0].completion.answered_user_intent is True
    assert batch.actions[0].completion.requires_user_response is True


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (
            {**_final_payload(), "tool_call": {"name": "file.write"}},
            "ACTION_FINAL_EFFECT_MIXTURE",
        ),
        ({"type": "unknown", "content": "no"}, "ACTION_KIND_UNSUPPORTED"),
        (
            {
                **_final_payload(),
                "intent": {**_intent(), "confidence": 1.5},
            },
            "ACTION_METADATA_INVALID",
        ),
        (
            {**_final_payload(), "content": "x" * 256_001},
            "ACTION_METADATA_INVALID",
        ),
    ],
)
def test_structural_errors_are_stable_and_effect_free(payload: dict, code: str) -> None:
    with pytest.raises(AgentActionParseError) as captured:
        _parse_payload(payload)

    assert captured.value.code == code


def test_provider_tool_calls_preserve_order_and_identity_and_reject_duplicates() -> None:
    response = ModelResponse(
        tool_calls=(
            ModelToolCall(id="provider-a", name="web.search", arguments={"query": "Nico"}),
            ModelToolCall(
                id="provider-b",
                name="file.write",
                arguments={"path": "result.txt", "content": "ok"},
            ),
        )
    )

    batch = parse_agent_actions(response)

    assert all(isinstance(action, ToolCallAction) for action in batch.actions)
    assert [action.position for action in batch.actions] == [0, 1]
    assert [action.provider_call_id for action in batch.actions] == [
        "provider-a",
        "provider-b",
    ]
    assert [action.name for action in batch.actions] == ["web.search", "file.write"]

    duplicate = response.model_copy(
        update={"tool_calls": (response.tool_calls[0], response.tool_calls[0])}
    )
    with pytest.raises(AgentActionParseError) as captured:
        parse_agent_actions(duplicate)
    assert captured.value.code == "ACTION_DUPLICATE_CALL_ID"


def test_final_plus_provider_effect_is_rejected_before_dispatch() -> None:
    response = ModelResponse(
        text=json.dumps(_final_payload()),
        tool_calls=(ModelToolCall(id="call-1", name="file.write", arguments={"path": "result"}),),
    )

    with pytest.raises(AgentActionParseError) as captured:
        parse_agent_actions(response)

    assert captured.value.code == "ACTION_FINAL_EFFECT_MIXTURE"


def test_structured_output_and_plain_json_produce_equivalent_actions() -> None:
    plain = _parse_payload(_final_payload())
    structured = _parse_payload(_final_payload(), structured_output=True)

    assert plain.actions == structured.actions
    assert plain.source_format == "plain_json"
    assert structured.source_format == "structured_json"


def test_clarification_like_plain_text_is_only_a_legacy_final() -> None:
    text = "你是想问平台时间戳，还是系统时间来自哪里？"

    batch = parse_agent_actions(ModelResponse(text=text))

    assert isinstance(batch.actions[0], FinalAction)
    assert not isinstance(batch.actions[0], AskUserAction)
    assert batch.actions[0].content == text
    assert batch.actions[0].compatibility_mode is True
    assert batch.source_format == "legacy_plain_text"


def test_shadow_compatibility_preserves_structured_phase_json_as_legacy_final() -> None:
    response = ModelResponse(
        text='{"output":{"answer":"unchanged"}}',
        structured_output=True,
    )

    batch = parse_compatibility_agent_actions(response)

    assert batch.source_format == "legacy_plain_text"
    assert isinstance(batch.actions[0], FinalAction)
    assert batch.actions[0].content == response.text
    assert batch.actions[0].compatibility_mode is True


def test_shadow_compatibility_keeps_provider_tools_authoritative_over_incidental_text() -> None:
    response = ModelResponse(
        text="provider preamble",
        tool_calls=(ModelToolCall(id="call-1", name="web.search", arguments={"query": "Nico"}),),
    )

    batch = parse_compatibility_agent_actions(response)

    assert batch.source_format == "provider_tool_calls"
    assert isinstance(batch.actions[0], ToolCallAction)
    assert batch.actions[0].provider_call_id == "call-1"


def test_capability_filtered_schema_does_not_advertise_inactive_actions() -> None:
    final_only = agent_action_json_schema(frozenset({AgentActionKind.FINAL}))
    with_ask = agent_action_json_schema(
        frozenset({AgentActionKind.FINAL, AgentActionKind.ASK_USER})
    )
    unsupported = agent_action_response_format(
        frozenset({AgentActionKind.FINAL}),
        structured_output_supported=False,
    )
    supported = agent_action_response_format(
        frozenset({AgentActionKind.FINAL}),
        structured_output_supported=True,
    )

    assert "ask_user" not in json.dumps(final_only)
    assert "ask_user" in json.dumps(with_ask)
    assert "tool_call" not in json.dumps(with_ask)
    assert unsupported is None
    assert supported["type"] == "json_schema"


def test_equivalent_provider_tool_facts_have_same_platform_action_identity() -> None:
    first = parse_agent_actions(
        ModelResponse(
            tool_calls=(
                ModelToolCall(id="openai-call", name="web.search", arguments={"query": "Nico"}),
            )
        )
    ).actions[0]
    second = parse_agent_actions(
        ModelResponse(
            tool_calls=(
                ModelToolCall(id="gemini-0", name="web.search", arguments={"query": "Nico"}),
            )
        )
    ).actions[0]

    assert first.action_id == second.action_id
    assert first.provider_call_id != second.provider_call_id
