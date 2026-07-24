"""Shared parsing and persistence for durable Agent-question answers."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from nico_agent.cli.client import NicoApiClient
from nico_agent.cli.errors import CliError
from nico_agent.cli.output import Output


def parse_user_input_answer(schema: dict[str, Any], raw: str) -> Any:
    """Keep string answers literal and decode structured answers without coercion."""

    schema_type = schema.get("type")
    if schema_type == "string":
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(
            "USER_INPUT_ANSWER_INVALID",
            "this Agent question requires a valid JSON answer",
            exit_code=2,
        ) from exc


class UserInputCoordinator:
    def __init__(self, client: NicoApiClient, output: Output) -> None:
        self.client = client
        self.output = output

    def answer_text(
        self,
        request: dict[str, Any],
        raw: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        answer = parse_user_input_answer(
            dict(request.get("input_schema") or {"type": "string"}),
            raw,
        )
        return self.answer(
            request,
            answer,
            idempotency_key=idempotency_key,
        )

    def answer(
        self,
        request: dict[str, Any],
        answer: Any,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.client.answer_user_input(
            str(request["id"]),
            expected_revision=int(request["revision"]),
            answer=answer,
            idempotency_key=idempotency_key or str(uuid4()),
        )
