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
    active_agent_action_kinds,
    agent_action_json_schema,
    agent_action_protocol_instruction,
    agent_action_response_format,
    parse_agent_actions,
)


def _parse(payload: dict, *, response_format_type: str = "json_object"):
    return parse_agent_actions(
        ModelResponse(
            text=json.dumps(payload, ensure_ascii=False),
            response_format_type=response_format_type,
        )
    )


def _intent() -> dict:
    return {
        "interpreted_intent": "Delete one of several objects",
        "confidence": 0.45,
        "candidates": [
            {
                "candidate_id": "candidate-1",
                "intent": "Delete the selected object",
                "confidence": 0.45,
            }
        ],
        "ambiguity": 0.8,
        "risk": "high",
        "risk_reasons": ["Deletion is irreversible."],
        "missing_information": ["Exact object identifier"],
        "safe_partial_answer_possible": False,
    }


def test_valid_final_is_strict_immutable_agent_action() -> None:
    batch = _parse({"type": "final", "content": "hello"})

    action = batch.actions[0]
    assert isinstance(action, FinalAction)
    assert action.content == "hello"
    assert batch.source_format == "json_object"
    with pytest.raises(ValidationError):
        action.content = "changed"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("hello", "NON_JSON_RESPONSE"),
        ('{"final":{"content":"hello"}}', "ACTION_SCHEMA_MISMATCH"),
        ('{"answer":"hello"}', "ACTION_SCHEMA_MISMATCH"),
        ('{"content":"hello"}', "ACTION_SCHEMA_MISMATCH"),
        ('{"type":"something_else"}', "UNKNOWN_ACTION_TYPE"),
        ('{"type":"final","content":""}', "EMPTY_FINAL_CONTENT"),
        ('```json\n{"type":"final","content":"hello"}\n```', "NON_JSON_RESPONSE"),
        (
            'Here is the result:\n{"type":"final","content":"hello"}',
            "NON_JSON_RESPONSE",
        ),
        ('{"type":"final","content":"hello"} trailing', "MALFORMED_JSON"),
        ('{"type":"final","content":"hello"', "MALFORMED_JSON"),
    ],
)
def test_invalid_responses_have_stable_protocol_error_categories(
    raw: str,
    code: str,
) -> None:
    with pytest.raises(AgentActionParseError) as captured:
        parse_agent_actions(ModelResponse(text=raw, response_format_type="json_object"))

    assert captured.value.code == code


def test_content_json_tool_call_uses_the_same_internal_action_contract() -> None:
    batch = _parse(
        {
            "type": "tool_call",
            "tool_name": "search",
            "arguments": {"query": "example"},
        }
    )

    action = batch.actions[0]
    assert isinstance(action, ToolCallAction)
    assert action.name == "search"
    assert action.arguments == {"query": "example"}
    assert action.provider_call_id.startswith("json:")


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "tool_call", "arguments": {}},
        {"type": "tool_call", "tool_name": "", "arguments": {}},
        {"type": "tool_call", "tool_name": "search", "arguments": []},
        {
            "type": "tool_call",
            "tool_name": "search",
            "arguments": {},
            "unknown": True,
        },
    ],
)
def test_invalid_content_tool_calls_are_classified(payload: dict) -> None:
    with pytest.raises(AgentActionParseError) as captured:
        _parse(payload)

    assert captured.value.code == "INVALID_TOOL_CALL"


def test_native_tool_calls_preserve_order_and_reject_mixed_text_or_duplicates() -> None:
    response = ModelResponse(
        tool_calls=(
            ModelToolCall(id="provider-a", name="web.search", arguments={"query": "Nico"}),
            ModelToolCall(id="provider-b", name="file.write", arguments={"path": "result"}),
        )
    )
    batch = parse_agent_actions(response)

    assert batch.source_format == "native_tool_calls"
    assert [action.provider_call_id for action in batch.actions] == [
        "provider-a",
        "provider-b",
    ]
    assert [action.position for action in batch.actions] == [0, 1]

    with pytest.raises(AgentActionParseError) as mixed:
        parse_agent_actions(response.model_copy(update={"text": '{"type":"final","content":"x"}'}))
    assert mixed.value.code == "INVALID_TOOL_CALL"

    with pytest.raises(AgentActionParseError) as duplicate:
        parse_agent_actions(
            response.model_copy(update={"tool_calls": (response.tool_calls[0],) * 2})
        )
    assert duplicate.value.code == "INVALID_TOOL_CALL"


def test_ask_user_is_available_only_when_runtime_capabilities_are_active() -> None:
    inactive = active_agent_action_kinds(
        clarification_gate_enabled=True,
        user_input_handler_enabled=False,
    )
    active = active_agent_action_kinds(
        clarification_gate_enabled=True,
        user_input_handler_enabled=True,
    )
    payload = {
        "type": "ask_user",
        "question": "Which object?",
        "reason": "The target is ambiguous.",
        "intent": _intent(),
    }

    with pytest.raises(AgentActionParseError) as captured:
        parse_agent_actions(
            ModelResponse(text=json.dumps(payload)),
            allowed_actions=inactive,
        )
    assert captured.value.code == "ACTION_SCHEMA_MISMATCH"

    batch = parse_agent_actions(
        ModelResponse(text=json.dumps(payload)),
        allowed_actions=active,
    )
    assert isinstance(batch.actions[0], AskUserAction)


def test_response_format_source_is_explicit_and_actions_are_equivalent() -> None:
    plain = _parse({"type": "final", "content": "hello"})
    schema = _parse(
        {"type": "final", "content": "hello"},
        response_format_type="json_schema",
    )

    assert plain.actions == schema.actions
    assert plain.source_format == "json_object"
    assert schema.source_format == "json_schema"


def test_schema_and_prompt_are_centralized_on_the_active_contract() -> None:
    kinds = frozenset({AgentActionKind.FINAL, AgentActionKind.TOOL_CALL})
    schema = agent_action_json_schema(kinds)
    response_format = agent_action_response_format(kinds)
    prompt = agent_action_protocol_instruction(kinds)

    rendered = json.dumps(schema)
    assert "FinalActionInput" in rendered
    assert "ToolCallActionInput" in rendered
    assert "AskUserActionInput" not in rendered
    assert response_format["type"] == "json_schema"
    assert '{"type":"final","content":"最终答案"}' in prompt
    assert '{"type":"tool_call","tool_name":"tool_name","arguments":{}}' in prompt
    assert '{"final":{"content":"..."}}' in prompt
    assert '{"answer":"..."}' in prompt
