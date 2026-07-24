from __future__ import annotations

import pytest

from nico_agent.models.contracts import ModelResponse, ModelToolCall
from nico_agent.runtime.contracts import (
    RuntimeActionBatchState,
    RuntimeActionOutcome,
    RuntimeActionState,
)
from nico_agent.runtime.native.action_dispatcher import (
    ActionDispatchSuspended,
    AgentActionDispatcher,
    InMemoryRuntimeActionHandler,
)
from nico_agent.runtime.native.action_parser import parse_agent_actions


def _batch():
    return parse_agent_actions(
        ModelResponse(
            tool_calls=(
                ModelToolCall(id="call-a", name="search", arguments={"q": "one"}),
                ModelToolCall(id="call-b", name="search", arguments={"q": "two"}),
                ModelToolCall(id="call-c", name="search", arguments={"q": "three"}),
            )
        )
    )


@pytest.mark.asyncio
async def test_dispatches_committed_actions_in_provider_ordinal_order() -> None:
    batch = _batch()
    handler = InMemoryRuntimeActionHandler()
    handler.commit(batch)
    seen: list[int] = []

    async def execute(_action, ordinal):
        seen.append(ordinal)
        return RuntimeActionOutcome(
            status="succeeded",
            outcome_ref=f"tool_call:{ordinal}",
        )

    result = await AgentActionDispatcher(handler).dispatch(batch, execute)

    assert seen == [0, 1, 2]
    assert result.completed is True
    assert [item.ordinal for item in result.actions] == [0, 1, 2]
    state = await handler.wait_for_batch(batch.content_hash)
    assert state.dispatch_cursor == 3
    assert state.status == "completed"


@pytest.mark.asyncio
async def test_resume_skips_committed_ordinals_and_starts_at_first_unresolved() -> None:
    batch = _batch()
    first = RuntimeActionOutcome(status="succeeded", outcome_ref="tool_call:first")
    handler = InMemoryRuntimeActionHandler()
    handler.seed(
        RuntimeActionBatchState(
            batch_key=batch.content_hash,
            action_count=3,
            dispatch_cursor=1,
            status="dispatching",
            actions=(
                RuntimeActionState(
                    ordinal=0,
                    action_id=batch.actions[0].action_id,
                    status="succeeded",
                    outcome_ref=first.outcome_ref,
                    outcome=first,
                ),
                RuntimeActionState(
                    ordinal=1,
                    action_id=batch.actions[1].action_id,
                    status="pending",
                ),
                RuntimeActionState(
                    ordinal=2,
                    action_id=batch.actions[2].action_id,
                    status="pending",
                ),
            ),
        )
    )
    seen: list[int] = []

    async def execute(_action, ordinal):
        seen.append(ordinal)
        return RuntimeActionOutcome(status="succeeded", outcome_ref=f"tool_call:{ordinal}")

    result = await AgentActionDispatcher(handler).dispatch(batch, execute)

    assert seen == [1, 2]
    assert result.completed is True
    assert result.actions[0].recovered is True
    assert result.actions[0].outcome.outcome_ref == "tool_call:first"


@pytest.mark.asyncio
async def test_unknown_dispatched_effect_stops_without_calling_executor() -> None:
    batch = _batch()
    handler = InMemoryRuntimeActionHandler()
    handler.seed(
        RuntimeActionBatchState(
            batch_key=batch.content_hash,
            action_count=3,
            dispatch_cursor=1,
            status="dispatching",
            actions=(
                RuntimeActionState(
                    ordinal=0,
                    action_id=batch.actions[0].action_id,
                    status="succeeded",
                    outcome=RuntimeActionOutcome(
                        status="succeeded",
                        outcome_ref="tool_call:first",
                    ),
                ),
                RuntimeActionState(
                    ordinal=1,
                    action_id=batch.actions[1].action_id,
                    status="dispatched",
                ),
                RuntimeActionState(
                    ordinal=2,
                    action_id=batch.actions[2].action_id,
                    status="pending",
                ),
            ),
        )
    )
    seen: list[int] = []

    async def execute(_action, ordinal):
        seen.append(ordinal)
        return RuntimeActionOutcome(status="succeeded")

    result = await AgentActionDispatcher(handler).dispatch(batch, execute)

    assert seen == []
    assert result.completed is False
    assert result.terminal_outcome is not None
    assert result.terminal_outcome.status == "unknown"


@pytest.mark.asyncio
async def test_failed_outcome_prevents_later_ordinal_dispatch() -> None:
    batch = _batch()
    handler = InMemoryRuntimeActionHandler()
    handler.commit(batch)
    seen: list[int] = []

    async def execute(_action, ordinal):
        seen.append(ordinal)
        return RuntimeActionOutcome(
            status="failed" if ordinal == 1 else "succeeded",
            outcome_ref=f"tool_call:{ordinal}",
        )

    result = await AgentActionDispatcher(handler).dispatch(batch, execute)

    assert seen == [0, 1]
    assert result.completed is False
    assert result.terminal_outcome is not None
    assert result.terminal_outcome.status == "failed"


@pytest.mark.asyncio
async def test_suspension_releases_active_action_for_safe_resume() -> None:
    batch = _batch()
    handler = InMemoryRuntimeActionHandler()
    handler.commit(batch)

    async def execute(_action, _ordinal):
        raise ActionDispatchSuspended(RuntimeError("approval required"))

    with pytest.raises(ActionDispatchSuspended):
        await AgentActionDispatcher(handler).dispatch(batch, execute)

    state = await handler.wait_for_batch(batch.content_hash)
    assert state.dispatch_cursor == 0
    assert state.actions[0].status == "pending"


@pytest.mark.asyncio
async def test_durable_user_input_suspension_keeps_active_action_dispatched() -> None:
    batch = _batch()
    handler = InMemoryRuntimeActionHandler()
    handler.commit(batch)

    async def execute(_action, _ordinal):
        raise ActionDispatchSuspended(
            RuntimeError("user input required"),
            release_action=False,
        )

    with pytest.raises(ActionDispatchSuspended):
        await AgentActionDispatcher(handler).dispatch(batch, execute)

    state = await handler.wait_for_batch(batch.content_hash)
    assert state.dispatch_cursor == 0
    assert state.actions[0].status == "dispatched"
