"""Immutable provider-neutral AgentAction contracts."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ActionId = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]
ShortText = Annotated[str, Field(min_length=1, max_length=2_000)]
LongText = Annotated[str, Field(min_length=1, max_length=256_000)]


class AgentActionKind(StrEnum):
    FINAL = "final"
    TOOL_CALL = "tool_call"
    ASK_USER = "ask_user"


class ActionRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"


class ActionSourceFormat(StrEnum):
    JSON_OBJECT = "json_object"
    JSON_SCHEMA = "json_schema"
    NATIVE_TOOL_CALLS = "native_tool_calls"


class ActionModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class IntentCandidate(ActionModel):
    candidate_id: Annotated[str, Field(min_length=1, max_length=120)]
    intent: ShortText
    confidence: float = Field(ge=0, le=1)


class IntentResolution(ActionModel):
    interpreted_intent: Annotated[str, Field(min_length=1, max_length=4_000)]
    confidence: float = Field(ge=0, le=1)
    candidates: tuple[IntentCandidate, ...] = Field(min_length=1, max_length=8)
    ambiguity: float = Field(ge=0, le=1)
    risk: ActionRisk
    risk_reasons: tuple[ShortText, ...] = Field(max_length=8)
    missing_information: tuple[ShortText, ...] = Field(max_length=8)
    safe_partial_answer_possible: bool


class FinalCompletion(ActionModel):
    answered_user_intent: bool
    requires_user_response: bool


class FinalActionInput(ActionModel):
    type: Literal["final"]
    content: LongText
    intent: IntentResolution | None = None
    completion: FinalCompletion | None = None


class ToolCallActionInput(ActionModel):
    type: Literal["tool_call"]
    tool_name: Annotated[str, Field(min_length=1, max_length=120)]
    arguments: dict[str, Any]

    @field_validator("arguments")
    @classmethod
    def bound_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        rendered = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        if len(rendered) > 256_000:
            raise ValueError("tool arguments exceed the AgentAction bound")
        return value


class AskUserActionInput(ActionModel):
    type: Literal["ask_user"]
    question: Annotated[str, Field(min_length=1, max_length=4_000)]
    reason: Annotated[str, Field(min_length=1, max_length=4_000)]
    intent: IntentResolution


class FinalAction(ActionModel):
    schema_version: Literal[1] = 1
    kind: Literal["final"] = "final"
    action_id: ActionId
    content: LongText
    intent: IntentResolution | None = None
    completion: FinalCompletion | None = None


class ToolCallAction(ActionModel):
    schema_version: Literal[1] = 1
    kind: Literal["tool_call"] = "tool_call"
    action_id: ActionId
    position: int = Field(ge=0, le=1_000)
    provider_call_id: Annotated[str, Field(min_length=1, max_length=300)]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    arguments: dict[str, Any]

    @field_validator("arguments")
    @classmethod
    def bound_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        rendered = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        if len(rendered) > 256_000:
            raise ValueError("tool arguments exceed the AgentAction bound")
        return value


class AskUserAction(ActionModel):
    schema_version: Literal[1] = 1
    kind: Literal["ask_user"] = "ask_user"
    action_id: ActionId
    question: Annotated[str, Field(min_length=1, max_length=4_000)]
    reason: Annotated[str, Field(min_length=1, max_length=4_000)]
    intent: IntentResolution


AgentAction = Annotated[
    FinalAction | ToolCallAction | AskUserAction,
    Field(discriminator="kind"),
]


class AgentActionBatch(ActionModel):
    schema_version: Literal[1] = 1
    actions: tuple[AgentAction, ...] = Field(min_length=1, max_length=32)
    source_format: ActionSourceFormat
    content_hash: ActionId

    @model_validator(mode="after")
    def require_one_action_family(self) -> AgentActionBatch:
        kinds = {action.kind for action in self.actions}
        if len(kinds) != 1:
            raise ValueError("an AgentAction batch cannot mix action families")
        if kinds != {AgentActionKind.TOOL_CALL} and len(self.actions) != 1:
            raise ValueError("final and ask_user batches contain exactly one action")
        return self
