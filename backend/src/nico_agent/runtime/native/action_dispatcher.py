"""Ordered, crash-safe dispatch for committed provider-neutral AgentActions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from nico_agent.runtime.actions import AgentAction, AgentActionBatch
from nico_agent.runtime.contracts import (
    RuntimeActionBatchState,
    RuntimeActionHandler,
    RuntimeActionOutcome,
    RuntimeActionState,
)

ExecuteAction = Callable[[AgentAction, int], Awaitable[RuntimeActionOutcome]]


class ActionDispatchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ActionDispatchSuspended(Exception):
    """An authoritative handler suspended before the Action effect was committed."""

    def __init__(self, cause: BaseException, *, release_action: bool = True) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.release_action = release_action


@dataclass(frozen=True, slots=True)
class DispatchedAction:
    ordinal: int
    action: AgentAction
    outcome: RuntimeActionOutcome
    recovered: bool


@dataclass(frozen=True, slots=True)
class DispatchResult:
    actions: tuple[DispatchedAction, ...]
    completed: bool
    terminal_outcome: RuntimeActionOutcome | None = None


class InMemoryRuntimeActionHandler:
    """Unit/provider-test persistence boundary with production-equivalent transitions."""

    def __init__(self) -> None:
        self._batches: dict[str, RuntimeActionBatchState] = {}

    def commit(self, batch: AgentActionBatch) -> RuntimeActionBatchState:
        existing = self._batches.get(batch.content_hash)
        if existing is not None:
            return existing
        state = RuntimeActionBatchState(
            batch_key=batch.content_hash,
            action_count=len(batch.actions),
            dispatch_cursor=0,
            status="pending",
            actions=tuple(
                RuntimeActionState(
                    ordinal=ordinal,
                    action_id=action.action_id,
                    status="pending",
                )
                for ordinal, action in enumerate(batch.actions)
            ),
        )
        self._batches[batch.content_hash] = state
        return state

    async def wait_for_batch(self, batch_key: str) -> RuntimeActionBatchState:
        try:
            return self._batches[batch_key]
        except KeyError as exc:
            raise ActionDispatchError(
                "ACTION_BATCH_NOT_COMMITTED",
                "AgentAction batch was not committed before dispatch",
            ) from exc

    async def begin_action(
        self,
        batch_key: str,
        *,
        ordinal: int,
        action_id: str,
    ) -> RuntimeActionBatchState:
        state = await self.wait_for_batch(batch_key)
        action = _state_action(state, ordinal, action_id)
        if action.status != "pending" or ordinal != state.dispatch_cursor:
            raise ActionDispatchError(
                "ACTION_DISPATCH_STATE_INVALID",
                "AgentAction is not the first unresolved pending Action",
            )
        return self._replace(
            state,
            ordinal,
            action.model_copy(update={"status": "dispatched"}),
            status="dispatching",
        )

    async def complete_action(
        self,
        batch_key: str,
        *,
        ordinal: int,
        action_id: str,
        outcome: RuntimeActionOutcome,
    ) -> RuntimeActionBatchState:
        state = await self.wait_for_batch(batch_key)
        action = _state_action(state, ordinal, action_id)
        if action.status != "dispatched" or ordinal != state.dispatch_cursor:
            raise ActionDispatchError(
                "ACTION_DISPATCH_STATE_INVALID",
                "AgentAction outcome does not match the active cursor",
            )
        status = outcome.status if outcome.status != "unknown" else "failed"
        next_cursor = ordinal + 1 if status == "succeeded" else ordinal
        batch_status = (
            "completed"
            if status == "succeeded" and next_cursor == state.action_count
            else ("dispatching" if status == "succeeded" else "failed")
        )
        return self._replace(
            state,
            ordinal,
            action.model_copy(
                update={
                    "status": status,
                    "outcome_ref": outcome.outcome_ref,
                    "observation_ref": outcome.observation_ref,
                    "outcome": outcome,
                }
            ),
            cursor=next_cursor,
            status=batch_status,
        )

    async def release_action(
        self,
        batch_key: str,
        *,
        ordinal: int,
        action_id: str,
    ) -> RuntimeActionBatchState:
        state = await self.wait_for_batch(batch_key)
        action = _state_action(state, ordinal, action_id)
        if action.status != "dispatched" or ordinal != state.dispatch_cursor:
            raise ActionDispatchError(
                "ACTION_DISPATCH_STATE_INVALID",
                "only the active dispatched Action can be released",
            )
        return self._replace(
            state,
            ordinal,
            action.model_copy(update={"status": "pending"}),
            status="dispatching",
        )

    def seed(self, state: RuntimeActionBatchState) -> None:
        self._batches[state.batch_key] = state

    def _replace(
        self,
        state: RuntimeActionBatchState,
        ordinal: int,
        action: RuntimeActionState,
        *,
        cursor: int | None = None,
        status: str | None = None,
    ) -> RuntimeActionBatchState:
        actions = list(state.actions)
        actions[ordinal] = action
        updated = state.model_copy(
            update={
                "actions": tuple(actions),
                "dispatch_cursor": state.dispatch_cursor if cursor is None else cursor,
                "status": state.status if status is None else status,
            }
        )
        self._batches[state.batch_key] = updated
        return updated


class AgentActionDispatcher:
    def __init__(self, handler: RuntimeActionHandler) -> None:
        self.handler = handler

    async def dispatch(
        self,
        batch: AgentActionBatch,
        execute: ExecuteAction,
    ) -> DispatchResult:
        state = await self.handler.wait_for_batch(batch.content_hash)
        _validate_batch(state, batch)
        dispatched: list[DispatchedAction] = []

        for ordinal, action in enumerate(batch.actions):
            persisted = state.actions[ordinal]
            if ordinal < state.dispatch_cursor:
                if persisted.status != "succeeded" or persisted.outcome is None:
                    raise ActionDispatchError(
                        "ACTION_DISPATCH_STATE_INVALID",
                        "advanced Action cursor has no authoritative successful outcome",
                    )
                dispatched.append(
                    DispatchedAction(ordinal, action, persisted.outcome, recovered=True)
                )
                continue
            if ordinal > state.dispatch_cursor:
                break
            if persisted.status == "dispatched":
                unknown = RuntimeActionOutcome(
                    status="unknown",
                    outcome_ref=persisted.outcome_ref,
                    observation_ref=persisted.observation_ref,
                )
                return DispatchResult(tuple(dispatched), False, unknown)
            if persisted.status in {"failed", "blocked"}:
                terminal = persisted.outcome or RuntimeActionOutcome(
                    status=persisted.status,
                    outcome_ref=persisted.outcome_ref,
                    observation_ref=persisted.observation_ref,
                )
                return DispatchResult(tuple(dispatched), False, terminal)
            if persisted.status != "pending":
                raise ActionDispatchError(
                    "ACTION_DISPATCH_STATE_INVALID",
                    "AgentAction state does not match its batch cursor",
                )

            state = await self.handler.begin_action(
                batch.content_hash,
                ordinal=ordinal,
                action_id=action.action_id,
            )
            try:
                outcome = await execute(action, ordinal)
            except ActionDispatchSuspended as exc:
                if exc.release_action:
                    await self.handler.release_action(
                        batch.content_hash,
                        ordinal=ordinal,
                        action_id=action.action_id,
                    )
                raise
            except BaseException:
                # Keep `dispatched`: the effect boundary may be uncertain, so a
                # replacement Worker must stop rather than repeat it.
                raise
            state = await self.handler.complete_action(
                batch.content_hash,
                ordinal=ordinal,
                action_id=action.action_id,
                outcome=outcome,
            )
            dispatched.append(DispatchedAction(ordinal, action, outcome, recovered=False))
            if outcome.status != "succeeded":
                return DispatchResult(tuple(dispatched), False, outcome)

        return DispatchResult(
            tuple(dispatched),
            state.status == "completed",
            None if state.status == "completed" else _terminal_from_state(state),
        )


def _validate_batch(state: RuntimeActionBatchState, batch: AgentActionBatch) -> None:
    if (
        state.batch_key != batch.content_hash
        or state.action_count != len(batch.actions)
        or len(state.actions) != len(batch.actions)
        or state.dispatch_cursor > state.action_count
    ):
        raise ActionDispatchError(
            "ACTION_BATCH_MISMATCH",
            "committed AgentAction batch does not match the normalized response",
        )
    for ordinal, (persisted, action) in enumerate(zip(state.actions, batch.actions, strict=True)):
        if persisted.ordinal != ordinal or persisted.action_id != action.action_id:
            raise ActionDispatchError(
                "ACTION_BATCH_MISMATCH",
                "committed AgentAction identity or ordinal does not match",
            )


def _state_action(
    state: RuntimeActionBatchState,
    ordinal: int,
    action_id: str,
) -> RuntimeActionState:
    if ordinal >= len(state.actions):
        raise ActionDispatchError("ACTION_ORDINAL_INVALID", "AgentAction ordinal is out of range")
    action = state.actions[ordinal]
    if action.ordinal != ordinal or action.action_id != action_id:
        raise ActionDispatchError(
            "ACTION_BATCH_MISMATCH",
            "AgentAction identity does not match the committed ordinal",
        )
    return action


def _terminal_from_state(state: RuntimeActionBatchState) -> RuntimeActionOutcome | None:
    if state.dispatch_cursor >= len(state.actions):
        return None
    action = state.actions[state.dispatch_cursor]
    if action.status == "dispatched":
        return RuntimeActionOutcome(
            status="unknown",
            outcome_ref=action.outcome_ref,
            observation_ref=action.observation_ref,
        )
    if action.status in {"failed", "blocked"}:
        return action.outcome or RuntimeActionOutcome(
            status=action.status,
            outcome_ref=action.outcome_ref,
            observation_ref=action.observation_ref,
        )
    return None


def outcome_value(value: Any) -> dict[str, Any]:
    """Normalize handler DTOs into a bounded JSON projection for recovery tests."""

    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    raise TypeError("Action outcome value must be a DTO or object")
