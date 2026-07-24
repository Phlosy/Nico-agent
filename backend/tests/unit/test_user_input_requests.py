from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from nico_agent.user_inputs.contracts import (
    UserInputAnswer,
    UserInputRequestRead,
    validate_user_input_answer,
)


@pytest.mark.parametrize(
    ("schema", "answer"),
    [
        ({"type": "string", "minLength": 1}, "SQLite"),
        ({"type": "string", "enum": ["file", "database", "run"]}, "database"),
        (
            {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "confirmed": {"const": True},
                },
                "required": ["target", "confirmed"],
                "additionalProperties": False,
            },
            {"target": "run-7", "confirmed": True},
        ),
    ],
)
def test_string_choice_and_structured_answers_validate(schema: dict, answer: object) -> None:
    assert validate_user_input_answer(schema, answer) == answer


@pytest.mark.parametrize(
    ("schema", "answer"),
    [
        ({"type": "string", "minLength": 1}, ""),
        ({"type": "string", "enum": ["file", "database"]}, "run"),
        (
            {
                "type": "object",
                "properties": {"confirmed": {"const": True}},
                "required": ["confirmed"],
            },
            {"confirmed": False},
        ),
    ],
)
def test_invalid_answers_fail_without_coercion(schema: dict, answer: object) -> None:
    with pytest.raises(ValueError, match="does not match"):
        validate_user_input_answer(schema, answer)


def test_answer_payload_is_bounded() -> None:
    with pytest.raises(ValueError, match="65536"):
        UserInputAnswer(expected_revision=1, answer="x" * 70_000)


def test_public_projection_has_no_protected_answer_field() -> None:
    request = UserInputRequestRead(
        id=uuid4(),
        run_id=uuid4(),
        agent_action_id=uuid4(),
        question="Which target?",
        reason="Several targets are equally plausible.",
        input_schema={"type": "string", "enum": ["file", "database", "run"]},
        status="answered",
        answer_hash="a" * 64,
        answer_ref="protected:user-input",
        expires_at=datetime.now(UTC),
        answered_at=datetime.now(UTC),
        revision=2,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    projection = request.model_dump(mode="json")

    assert "answer" not in projection
    assert "answer_payload" not in projection
    assert "database-password" not in str(projection)
