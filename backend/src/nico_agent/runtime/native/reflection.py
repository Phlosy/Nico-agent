"""Constrained Reflection decisions for recovery and replanning."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ReflectionDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: Literal["retry", "replan", "fail"]
    reason: str = Field(min_length=1, max_length=4_000)
    recovery_instruction: str | None = Field(default=None, max_length=8_000)


def reflection_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "nico_reflection",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["decision", "reason", "recovery_instruction"],
                "properties": {
                    "decision": {"type": "string", "enum": ["retry", "replan", "fail"]},
                    "reason": {"type": "string", "minLength": 1},
                    "recovery_instruction": {"type": ["string", "null"]},
                },
            },
        },
    }


def parse_reflection(value: str | dict[str, Any]) -> ReflectionDecision:
    try:
        payload = json.loads(value) if isinstance(value, str) else value
        return ReflectionDecision.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValidationError) as exc:
        raise ValueError("reflection returned an invalid constrained decision") from exc
