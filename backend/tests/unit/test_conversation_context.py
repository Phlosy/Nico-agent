from uuid import uuid4

from nico_agent.conversations.context import _select_recent_turns
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

    selected, omitted = _select_recent_turns(newest_first, history_budget=800)

    assert [turn.sequence for turn in selected] == [2, 3]
    assert omitted == 1


def test_context_selection_always_keeps_one_oversized_recent_turn() -> None:
    selected, omitted = _select_recent_turns([_turn(2, 5_000), _turn(1, 5_000)], 1_000)

    assert [turn.sequence for turn in selected] == [2]
    assert omitted == 1
