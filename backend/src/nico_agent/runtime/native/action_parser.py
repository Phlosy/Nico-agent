"""Strict parsing from provider-neutral responses into AgentAction facts."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any

from pydantic import Field, TypeAdapter, ValidationError

from nico_agent.models.contracts import ModelResponse
from nico_agent.runtime.actions import (
    ActionSourceFormat,
    AgentAction,
    AgentActionBatch,
    AgentActionKind,
    AskUserAction,
    AskUserActionInput,
    FinalAction,
    FinalActionInput,
    ToolCallAction,
    ToolCallActionInput,
)

_ALL_ACTIONS: frozenset[AgentActionKind] = frozenset(AgentActionKind)

FINAL_ACTION_EXAMPLE = '{"type":"final","content":"最终答案"}'
TOOL_CALL_ACTION_EXAMPLE = '{"type":"tool_call","tool_name":"tool_name","arguments":{}}'


class AgentActionParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def active_agent_action_kinds(
    *,
    clarification_gate_enabled: bool,
    user_input_handler_enabled: bool,
) -> frozenset[AgentActionKind]:
    kinds = {AgentActionKind.FINAL, AgentActionKind.TOOL_CALL}
    if clarification_gate_enabled and user_input_handler_enabled:
        kinds.add(AgentActionKind.ASK_USER)
    return frozenset(kinds)


def agent_action_protocol_instruction(
    allowed_actions: frozenset[AgentActionKind],
) -> str:
    """Render protocol prose and examples from the same active contract."""

    examples = [f"Final: {FINAL_ACTION_EXAMPLE}"]
    if AgentActionKind.TOOL_CALL in allowed_actions:
        examples.append(f"Tool call: {TOOL_CALL_ACTION_EXAMPLE}")
    if AgentActionKind.ASK_USER in allowed_actions:
        examples.append(
            'Ask user: {"type":"ask_user","question":"...","reason":"...",'
            '"intent":{"interpreted_intent":"...","confidence":0.5,'
            '"candidates":[{"candidate_id":"candidate-1","intent":"...",'
            '"confidence":0.5}],"ambiguity":0.5,"risk":"unknown",'
            '"risk_reasons":[],"missing_information":["..."],'
            '"safe_partial_answer_possible":false}}'
        )
    return "\n".join(
        (
            "AgentAction output protocol:",
            "- Return exactly one JSON object for every response.",
            "- Output no text before or after the JSON object.",
            "- Do not use Markdown or JSON code fences.",
            "- The root object must contain a supported type.",
            *examples,
            "Forbidden: plain text such as 最终答案是……",
            'Forbidden: {"final":{"content":"..."}}',
            'Forbidden: {"answer":"..."}',
        )
    )


def protocol_correction_instruction(error: AgentActionParseError) -> str:
    if error.code == "NON_JSON_RESPONSE":
        problem = "Your response was not JSON."
    elif error.code == "MALFORMED_JSON":
        problem = "Your response started as JSON but was malformed."
    else:
        problem = f"Your JSON did not satisfy AgentAction ({error.code})."
    return "\n".join(
        (
            problem,
            "Return only one corrected AgentAction JSON object.",
            f"Valid final: {FINAL_ACTION_EXAMPLE}",
            f"Valid tool call: {TOOL_CALL_ACTION_EXAMPLE}",
            "Do not return a wrapper object, aliases, Markdown, or surrounding text.",
        )
    )


def parse_agent_actions(
    response: ModelResponse,
    *,
    allowed_actions: frozenset[AgentActionKind] = _ALL_ACTIONS,
) -> AgentActionBatch:
    """Strictly parse one response without aliases, wrappers, or inference."""

    text = response.text.strip()
    if response.tool_calls:
        if text:
            raise AgentActionParseError(
                "INVALID_TOOL_CALL",
                "a response cannot mix provider tool calls with JSON text",
            )
        _require_allowed(AgentActionKind.TOOL_CALL, allowed_actions)
        return _native_tool_action_batch(response)
    if not text or not text.startswith("{"):
        raise AgentActionParseError(
            "NON_JSON_RESPONSE",
            "AgentAction response must be exactly one JSON object",
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentActionParseError(
            "MALFORMED_JSON",
            "AgentAction response contains malformed JSON or surrounding text",
        ) from exc
    if not isinstance(payload, dict):
        raise AgentActionParseError(
            "ACTION_SCHEMA_MISMATCH",
            "AgentAction JSON root must be one object",
        )

    action_type = payload.get("type")
    if action_type is None:
        raise AgentActionParseError(
            "ACTION_SCHEMA_MISMATCH",
            "AgentAction root object must contain type",
        )
    if action_type not in {kind.value for kind in AgentActionKind}:
        raise AgentActionParseError(
            "UNKNOWN_ACTION_TYPE",
            f"unknown AgentAction type: {action_type!r}",
        )

    kind = AgentActionKind(action_type)
    _require_allowed(kind, allowed_actions)
    if kind is AgentActionKind.FINAL:
        content = payload.get("content")
        if isinstance(content, str) and not content.strip():
            raise AgentActionParseError(
                "EMPTY_FINAL_CONTENT",
                "final content must not be empty",
            )
        parsed = _validate(FinalActionInput, payload, "ACTION_SCHEMA_MISMATCH")
        action: AgentAction = FinalAction(
            action_id=_action_id(parsed.model_dump(mode="json")),
            content=parsed.content,
            intent=parsed.intent,
            completion=parsed.completion,
        )
    elif kind is AgentActionKind.TOOL_CALL:
        parsed_tool = _validate(ToolCallActionInput, payload, "INVALID_TOOL_CALL")
        action = ToolCallAction(
            action_id=_action_id(parsed_tool.model_dump(mode="json")),
            position=0,
            provider_call_id=f"json:{_action_id(parsed_tool.model_dump(mode='json'))[:48]}",
            name=parsed_tool.tool_name,
            arguments=parsed_tool.arguments,
        )
    else:
        parsed_ask = _validate(AskUserActionInput, payload, "ACTION_SCHEMA_MISMATCH")
        action = AskUserAction(
            action_id=_action_id(parsed_ask.model_dump(mode="json")),
            question=parsed_ask.question,
            reason=parsed_ask.reason,
            intent=parsed_ask.intent,
        )
    return _batch((action,), _json_source(response))


def agent_action_json_schema(
    allowed_actions: frozenset[AgentActionKind],
) -> dict[str, Any]:
    enabled = allowed_actions.intersection(_ALL_ACTIONS)
    if not enabled:
        raise ValueError("at least one AgentAction must be enabled")
    if enabled == {AgentActionKind.FINAL}:
        return FinalActionInput.model_json_schema()
    if enabled == {AgentActionKind.TOOL_CALL}:
        return ToolCallActionInput.model_json_schema()
    if enabled == {AgentActionKind.ASK_USER}:
        return AskUserActionInput.model_json_schema()
    if enabled == {AgentActionKind.FINAL, AgentActionKind.TOOL_CALL}:
        return TypeAdapter(
            Annotated[
                FinalActionInput | ToolCallActionInput,
                Field(discriminator="type"),
            ]
        ).json_schema()
    if enabled == {AgentActionKind.FINAL, AgentActionKind.ASK_USER}:
        return TypeAdapter(
            Annotated[
                FinalActionInput | AskUserActionInput,
                Field(discriminator="type"),
            ]
        ).json_schema()
    if enabled == {AgentActionKind.TOOL_CALL, AgentActionKind.ASK_USER}:
        return TypeAdapter(
            Annotated[
                ToolCallActionInput | AskUserActionInput,
                Field(discriminator="type"),
            ]
        ).json_schema()
    return TypeAdapter(
        Annotated[
            FinalActionInput | ToolCallActionInput | AskUserActionInput,
            Field(discriminator="type"),
        ]
    ).json_schema()


def agent_action_response_format(
    allowed_actions: frozenset[AgentActionKind],
) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "nico_agent_action_v1",
            "strict": True,
            "schema": agent_action_json_schema(allowed_actions),
        },
    }


def _native_tool_action_batch(response: ModelResponse) -> AgentActionBatch:
    seen: set[str] = set()
    actions: list[AgentAction] = []
    for position, call in enumerate(response.tool_calls):
        if call.id in seen:
            raise AgentActionParseError(
                "INVALID_TOOL_CALL",
                f"duplicate provider tool call id: {call.id}",
            )
        seen.add(call.id)
        identity = {
            "kind": AgentActionKind.TOOL_CALL,
            "position": position,
            "name": call.name,
            "arguments": call.arguments,
        }
        try:
            actions.append(
                ToolCallAction(
                    action_id=_action_id(identity),
                    position=position,
                    provider_call_id=call.id,
                    name=call.name,
                    arguments=call.arguments,
                )
            )
        except ValidationError as exc:
            raise AgentActionParseError(
                "INVALID_TOOL_CALL",
                f"provider tool call is invalid: {exc.errors()[0]['type']}",
            ) from exc
    return _batch(tuple(actions), ActionSourceFormat.NATIVE_TOOL_CALLS)


def _json_source(response: ModelResponse) -> ActionSourceFormat:
    if response.response_format_type == "json_schema":
        return ActionSourceFormat.JSON_SCHEMA
    return ActionSourceFormat.JSON_OBJECT


def _batch(
    actions: tuple[AgentAction, ...],
    source: ActionSourceFormat,
) -> AgentActionBatch:
    projection = [action.model_dump(mode="json") for action in actions]
    return AgentActionBatch(
        actions=actions,
        source_format=source,
        content_hash=_action_id(projection),
    )


def _action_id(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate(model: Any, payload: dict[str, Any], code: str) -> Any:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise AgentActionParseError(
            code,
            f"AgentAction schema validation failed: {exc.errors()[0]['type']}",
        ) from exc


def _require_allowed(
    kind: AgentActionKind,
    allowed: frozenset[AgentActionKind],
) -> None:
    if kind not in allowed:
        raise AgentActionParseError(
            "ACTION_SCHEMA_MISMATCH",
            f"{kind.value} is not enabled for this AgentAction contract",
        )
