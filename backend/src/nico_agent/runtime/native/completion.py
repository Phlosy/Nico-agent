"""Task/Plan acceptance checks that run after the shared semantic Completion Gate."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from jsonschema import SchemaError, validate
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class CompletionCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    passed: bool
    details: dict[str, Any] = Field(default_factory=dict)


class CompletionEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    passed: bool
    output_hash: str
    evidence_refs: tuple[str, ...] = ()
    checks: tuple[CompletionCheck, ...]


class CompletionJudgeDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: Literal["passed", "failed"]
    reason: str = Field(min_length=1, max_length=4_000)


def completion_judge_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "nico_completion_judge",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["verdict", "reason"],
                "properties": {
                    "verdict": {"type": "string", "enum": ["passed", "failed"]},
                    "reason": {"type": "string", "minLength": 1},
                },
            },
        },
    }


def parse_completion_judge(value: str | dict[str, Any]) -> CompletionJudgeDecision:
    try:
        payload = json.loads(value) if isinstance(value, str) else value
        return CompletionJudgeDecision.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValidationError) as exc:
        raise ValueError("completion judge returned an invalid decision") from exc


def evaluate_completion(
    output: dict[str, Any],
    *,
    acceptance: dict[str, Any],
    evidence_refs: tuple[str, ...] = (),
) -> CompletionEvaluation:
    checks: list[CompletionCheck] = []
    if acceptance.get("non_empty") is True:
        checks.append(
            CompletionCheck(
                code="NON_EMPTY_REQUIRED",
                passed=_is_non_empty(output),
            )
        )

    required = acceptance.get("required")
    if isinstance(required, list) and all(isinstance(item, str) for item in required):
        missing = sorted(set(required) - set(output))
        checks.append(
            CompletionCheck(
                code="REQUIRED_FIELDS",
                passed=not missing,
                details={"missing": missing},
            )
        )

    schema = acceptance.get("output_schema")
    if isinstance(schema, dict):
        details: dict[str, Any] = {}
        try:
            validate(instance=output, schema=schema)
            valid = True
        except JsonSchemaValidationError as exc:
            valid = False
            details = {"path": list(exc.absolute_path), "message": exc.message[:500]}
        except SchemaError as exc:
            valid = False
            details = {"schema_error": exc.message[:500]}
        checks.append(
            CompletionCheck(
                code="OUTPUT_SCHEMA_INVALID",
                passed=valid,
                details=details,
            )
        )

    if not checks:
        checks.append(CompletionCheck(code="OUTPUT_PRESENT", passed=_is_non_empty(output)))
    return CompletionEvaluation(
        passed=all(check.passed for check in checks),
        output_hash=_hash_json(output),
        evidence_refs=evidence_refs,
        checks=tuple(checks),
    )


def _is_non_empty(value: Any) -> bool:
    if isinstance(value, dict):
        if not value:
            return False
        return all(_is_non_empty(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return bool(value) and all(_is_non_empty(item) for item in value)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def _hash_json(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
