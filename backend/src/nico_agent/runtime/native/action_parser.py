"""Pure parsing from provider-neutral ModelResponse into AgentAction facts."""

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
    FinalCompletion,
    IntentCandidate,
    IntentResolution,
    ToolCallAction,
)

_ALL_ACTIONS = frozenset(AgentActionKind)
_FINAL_EFFECT_FIELDS = frozenset({"tool_call", "tool_calls", "effect", "effects"})


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
    kinds = {
        AgentActionKind.FINAL,
        AgentActionKind.TOOL_CALL,
    }
    if clarification_gate_enabled and user_input_handler_enabled:
        kinds.add(AgentActionKind.ASK_USER)
    return frozenset(kinds)


def parse_agent_actions(
    response: ModelResponse,
    *,
    allowed_actions: frozenset[AgentActionKind] = _ALL_ACTIONS,
    allow_legacy_plain_text: bool = True,
) -> AgentActionBatch:
    """Parse one response without dispatching, persisting, or inferring from phrases."""

    text = response.text.strip()
    if response.tool_calls:
        if text:
            raise AgentActionParseError(
                "ACTION_FINAL_EFFECT_MIXTURE",
                "a response cannot contain both text and provider Tool calls",
            )
        if AgentActionKind.TOOL_CALL not in allowed_actions:
            raise AgentActionParseError(
                "ACTION_KIND_UNSUPPORTED",
                "tool_call is not enabled for this Action contract",
            )
        return _tool_action_batch(response)
    if not text:
        raise AgentActionParseError("ACTION_RESPONSE_EMPTY", "model response is empty")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        if response.structured_output or not allow_legacy_plain_text or text.startswith(("{", "[")):
            raise AgentActionParseError(
                "ACTION_JSON_INVALID",
                "AgentAction response is not valid JSON",
            ) from exc
        return _legacy_final_batch(text)
    if not isinstance(payload, dict):
        raise AgentActionParseError(
            "ACTION_ENVELOPE_INVALID",
            "AgentAction JSON must be one object",
        )

    action_type = payload.get("type")
    if action_type == AgentActionKind.FINAL:
        if _FINAL_EFFECT_FIELDS.intersection(payload):
            raise AgentActionParseError(
                "ACTION_FINAL_EFFECT_MIXTURE",
                "a final Action cannot contain effect fields",
            )
        _require_allowed(AgentActionKind.FINAL, allowed_actions)
        parsed = _validate(FinalActionInput, payload)
        action: AgentAction = FinalAction(
            action_id=_action_id(parsed.model_dump(mode="json")),
            content=parsed.content,
            intent=parsed.intent,
            completion=parsed.completion,
        )
    elif action_type == AgentActionKind.ASK_USER:
        _require_allowed(AgentActionKind.ASK_USER, allowed_actions)
        parsed_ask = _validate(AskUserActionInput, payload)
        action = AskUserAction(
            action_id=_action_id(parsed_ask.model_dump(mode="json")),
            question=parsed_ask.question,
            reason=parsed_ask.reason,
            intent=parsed_ask.intent,
        )
    else:
        raise AgentActionParseError(
            "ACTION_KIND_UNSUPPORTED",
            f"unsupported AgentAction type: {action_type!r}",
        )
    source = (
        ActionSourceFormat.STRUCTURED_JSON
        if response.structured_output
        else ActionSourceFormat.PLAIN_JSON
    )
    return _batch((action,), source)


def parse_compatibility_agent_actions(response: ModelResponse) -> AgentActionBatch:
    """Shadow-normalize the currently authoritative pre-Action loop behavior.

    During U5, ordinary non-Tool text remains a legacy final even when another
    phase requested structured JSON. Provider Tool calls remain authoritative
    when a provider also emits incidental text. U6 replaces this compatibility
    path with active Action dispatch.
    """

    if response.tool_calls:
        return parse_agent_actions(
            response.model_copy(update={"text": "", "structured_output": False})
        )
    text = response.text.strip()
    if not text:
        raise AgentActionParseError("ACTION_RESPONSE_EMPTY", "model response is empty")
    return _legacy_final_batch(text)


def agent_action_json_schema(
    allowed_actions: frozenset[AgentActionKind],
) -> dict[str, Any]:
    """Return the non-Tool Action schema for the enabled dispatcher surface."""

    enabled = allowed_actions.intersection({AgentActionKind.FINAL, AgentActionKind.ASK_USER})
    if not enabled:
        raise ValueError("at least one non-Tool AgentAction must be enabled")
    if enabled == {AgentActionKind.FINAL}:
        return FinalActionInput.model_json_schema()
    if enabled == {AgentActionKind.ASK_USER}:
        return AskUserActionInput.model_json_schema()
    envelope = Annotated[
        FinalActionInput | AskUserActionInput,
        Field(discriminator="type"),
    ]
    return TypeAdapter(envelope).json_schema()


def agent_action_response_format(
    allowed_actions: frozenset[AgentActionKind],
    *,
    structured_output_supported: bool,
) -> dict[str, Any] | None:
    if not structured_output_supported:
        return None
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "nico_agent_action_v1",
            "strict": True,
            "schema": agent_action_json_schema(allowed_actions),
        },
    }


def _tool_action_batch(response: ModelResponse) -> AgentActionBatch:
    seen: set[str] = set()
    actions: list[AgentAction] = []
    for position, call in enumerate(response.tool_calls):
        if call.id in seen:
            raise AgentActionParseError(
                "ACTION_DUPLICATE_CALL_ID",
                f"duplicate provider Tool call id: {call.id}",
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
            raise _metadata_error(exc) from exc
    return _batch(tuple(actions), ActionSourceFormat.PROVIDER_TOOL_CALLS)


def _legacy_final_batch(text: str) -> AgentActionBatch:
    intent = IntentResolution(
        interpreted_intent="Legacy plain-text final compatibility",
        confidence=0,
        candidates=(
            IntentCandidate(
                candidate_id="legacy-plain-text",
                intent="Preserve the pre-AgentAction final behavior",
                confidence=0,
            ),
        ),
        ambiguity=1,
        risk="unknown",
        risk_reasons=(),
        missing_information=(),
        safe_partial_answer_possible=False,
    )
    completion = FinalCompletion(
        answered_user_intent=True,
        requires_user_response=False,
    )
    try:
        action = FinalAction(
            action_id=_action_id({"kind": "legacy_final", "content": text}),
            content=text,
            intent=intent,
            completion=completion,
            compatibility_mode=True,
        )
    except ValidationError as exc:
        raise _metadata_error(exc) from exc
    return _batch((action,), ActionSourceFormat.LEGACY_PLAIN_TEXT)


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


def _validate(model: Any, payload: dict[str, Any]) -> Any:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise _metadata_error(exc) from exc


def _metadata_error(exc: ValidationError) -> AgentActionParseError:
    return AgentActionParseError(
        "ACTION_METADATA_INVALID",
        f"AgentAction metadata is invalid: {exc.errors()[0]['type']}",
    )


def _require_allowed(
    kind: AgentActionKind,
    allowed: frozenset[AgentActionKind],
) -> None:
    if kind not in allowed:
        raise AgentActionParseError(
            "ACTION_KIND_UNSUPPORTED",
            f"{kind.value} is not enabled for this Action contract",
        )
