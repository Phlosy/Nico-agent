"""Application and Runtime contracts for durable Agent questions."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_USER_INPUT_SCHEMA_BYTES = 32_768
MAX_USER_INPUT_ANSWER_BYTES = 65_536


def _json_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode()
    )


def validate_user_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if _json_size(schema) > MAX_USER_INPUT_SCHEMA_BYTES:
        raise ValueError(f"user input schema must not exceed {MAX_USER_INPUT_SCHEMA_BYTES} bytes")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError("user input schema is invalid") from exc
    return schema


def validate_user_input_answer(schema: dict[str, Any], answer: Any) -> Any:
    validate_user_input_schema(schema)
    if _json_size(answer) > MAX_USER_INPUT_ANSWER_BYTES:
        raise ValueError(f"user input answer must not exceed {MAX_USER_INPUT_ANSWER_BYTES} bytes")
    try:
        Draft202012Validator(schema).validate(answer)
    except JsonSchemaValidationError as exc:
        raise ValueError("user input answer does not match the request schema") from exc
    return answer


class UserInputAnswer(BaseModel):
    model_config = ConfigDict(frozen=True)

    expected_revision: int = Field(ge=1)
    answer: Any

    @field_validator("answer")
    @classmethod
    def bound_answer(cls, value: Any) -> Any:
        if _json_size(value) > MAX_USER_INPUT_ANSWER_BYTES:
            raise ValueError(
                f"user input answer must not exceed {MAX_USER_INPUT_ANSWER_BYTES} bytes"
            )
        return value


class RuntimeUserInputIntent(BaseModel):
    """Lease-bound request created for one already-dispatched AskUserAction."""

    model_config = ConfigDict(frozen=True)

    batch_key: str = Field(min_length=64, max_length=64)
    action_id: str = Field(min_length=64, max_length=64)
    ordinal: int = Field(ge=0)
    question: str = Field(min_length=1, max_length=4_000)
    reason: str = Field(min_length=1, max_length=4_000)
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "string"})
    checkpoint: dict[str, Any]
    ttl_seconds: int = Field(default=86_400, ge=1, le=604_800)
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("input_schema")
    @classmethod
    def valid_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_user_input_schema(value)

    @field_validator("checkpoint")
    @classmethod
    def bound_checkpoint(cls, value: dict[str, Any]) -> dict[str, Any]:
        if _json_size(value) > 262_144:
            raise ValueError("user input checkpoint must not exceed 262144 bytes")
        return value


class RuntimeUserInputRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    status: Literal["requested", "answered", "expired", "cancelled"]
    revision: int = Field(ge=1)
    wake_key: str = Field(min_length=64, max_length=64)
    expires_at: datetime


class UserInputRequestRead(BaseModel):
    """Redacted projection. The protected answer payload is deliberately absent."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    run_id: UUID
    agent_action_id: UUID
    question: str
    reason: str
    input_schema: dict[str, Any]
    status: Literal["requested", "answered", "expired", "cancelled"]
    answer_hash: str | None = Field(default=None, min_length=64, max_length=64)
    answer_ref: str | None = Field(default=None, min_length=1, max_length=500)
    expires_at: datetime
    answered_at: datetime | None = None
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class UserInputRequired(Exception):
    """Signals a durable request without exposing protected answer content."""

    def __init__(self, request: RuntimeUserInputRequest) -> None:
        super().__init__("durable user input is required")
        self.request = request
        self.details = {
            "request_id": str(request.id),
            "revision": request.revision,
            "wake_key": request.wake_key,
            "expires_at": request.expires_at.isoformat(),
        }
