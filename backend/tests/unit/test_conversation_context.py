import hashlib
import json
from uuid import uuid4

from nico_agent.conversations.context import (
    _project_conversation_messages,
    _select_recent_turns,
)
from nico_agent.domain.models import ConversationTurn


def _turn(sequence: int, chars: int) -> ConversationTurn:
    return ConversationTurn(
        id=uuid4(),
        tenant_id=uuid4(),
        conversation_id=uuid4(),
        sequence=sequence,
        user_input=str(sequence) * chars,
        task_id=uuid4(),
        run_id=uuid4(),
        status="completed",
        assistant_output={"content": str(sequence) * chars},
        artifact_refs=[],
        idempotency_key=f"turn-{sequence}",
    )


def test_context_selection_keeps_currently_newest_contiguous_turns() -> None:
    newest_first = [_turn(3, 80), _turn(2, 80), _turn(1, 80)]

    selected, omitted = _select_recent_turns(newest_first, history_budget=2_000)

    assert [turn.sequence for turn in selected] == [2, 3]
    assert omitted == 1


def test_context_selection_always_keeps_one_oversized_recent_turn() -> None:
    newest = _turn(2, 300_000)
    selected, omitted = _select_recent_turns([newest, _turn(1, 5_000)], 1_000)
    projected = _project_conversation_messages(selected, history_budget=1_000)

    assert [turn.sequence for turn in selected] == [2]
    assert omitted == 1
    assert all(len(message.content) < 1_000 for message in projected)
    assert all(message.content_truncated for message in projected)


def test_structured_assistant_output_is_normalized_and_bounded_without_mutation() -> None:
    turn = _turn(1, 40)
    original = {
        "result": {
            "content": "answer-" * 1_000,
            "metadata": {"provider": "fixture"},
        }
    }
    turn.assistant_output = original

    projected = _project_conversation_messages([turn], history_budget=1_000)

    assert [message.role for message in projected] == ["user", "assistant"]
    assert (
        len(
            json.dumps(
                [message.model_dump(mode="json") for message in projected],
                ensure_ascii=False,
            )
        )
        <= 1_000
    )
    assert projected[1].content.endswith("…[TRUNCATED]")
    assert projected[1].content_hash == hashlib.sha256(projected[1].content.encode()).hexdigest()
    assert projected[1].content_truncated is True
    assert turn.assistant_output == original


def test_unsupported_structured_assistant_output_has_deterministic_fallback() -> None:
    turn = _turn(1, 10)
    turn.assistant_output = {"z": [2, 1], "a": {"value": True}}

    first = _project_conversation_messages([turn], history_budget=4_000)
    second = _project_conversation_messages([turn], history_budget=4_000)

    assert first == second
    assert first[1].content == '{"a":{"value":true},"z":[2,1]}'
    assert first[1].source_ref == f"conversation-turn:{turn.id}"
    assert first[1].trust == "untrusted_data"
