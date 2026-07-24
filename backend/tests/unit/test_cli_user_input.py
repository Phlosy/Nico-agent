from __future__ import annotations

from typing import Any

import pytest

from nico_agent.cli.errors import CliError
from nico_agent.cli.output import Output
from nico_agent.cli.user_inputs import UserInputCoordinator, parse_user_input_answer


class FakeClient:
    def __init__(self) -> None:
        self.answers: list[dict[str, Any]] = []

    def answer_user_input(self, request_id: str, **kwargs: Any) -> dict[str, Any]:
        self.answers.append({"request_id": request_id, **kwargs})
        return {"id": request_id, "status": "answered", "revision": 2}


@pytest.mark.parametrize(
    ("schema", "raw", "expected"),
    [
        ({"type": "string"}, "SQLite", "SQLite"),
        ({"type": "string", "enum": ["file", "database"]}, "database", "database"),
        ({"type": "integer"}, "42", 42),
        (
            {"type": "object"},
            '{"target":"run-7","confirmed":true}',
            {"target": "run-7", "confirmed": True},
        ),
    ],
)
def test_parse_user_input_answer_preserves_string_and_decodes_structured_values(
    schema: dict[str, Any],
    raw: str,
    expected: Any,
) -> None:
    assert parse_user_input_answer(schema, raw) == expected


def test_parse_user_input_answer_rejects_malformed_structured_json() -> None:
    with pytest.raises(CliError) as captured:
        parse_user_input_answer({"type": "object"}, "not-json")

    assert captured.value.code == "USER_INPUT_ANSWER_INVALID"


def test_coordinator_uses_user_input_endpoint_and_revision() -> None:
    client = FakeClient()
    coordinator = UserInputCoordinator(
        client,  # type: ignore[arg-type]
        Output(json_mode=True),
    )
    request = {
        "id": "question-1",
        "revision": 4,
        "status": "requested",
        "input_schema": {"type": "string"},
    }

    result = coordinator.answer_text(request, "database", idempotency_key="answer-4")

    assert result["status"] == "answered"
    assert client.answers == [
        {
            "request_id": "question-1",
            "expected_revision": 4,
            "answer": "database",
            "idempotency_key": "answer-4",
        }
    ]
