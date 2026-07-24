"""Transaction-aware authority for durable Run lifecycle transitions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.database import TenantContext
from nico_agent.domain.errors import DomainConflict
from nico_agent.domain.models import AuditRecord, Event, Run
from nico_agent.domain.states import (
    RUN_TRANSITIONS,
    RunStatus,
    require_revision,
    transition_state,
)
from nico_agent.runtime.contracts import RuntimeLoopState
from nico_agent.runtime.errors import RuntimeLeaseLost

LIFECYCLE_CONTRACT_VERSION = 1
MAX_LIFECYCLE_METADATA_BYTES = 16_384

# SQL functions are deliberately narrow lifecycle ports. They are defined by
# migration 0027 and parity-tested against RunLifecycleAuthority.
LIFECYCLE_SQL_PORTS = (
    "claim_next_run",
    "reconcile_expired_tool_approvals",
    "reconcile_coordination_waiters",
    "reconcile_user_input_requests",
)

# Executable inventory used by focused tests to keep new state writers visible.
# Run status writers named here must call RunLifecycleAuthority; the SQL entries
# above enforce the same transition matrix in PostgreSQL.
LIFECYCLE_WRITER_ALLOWLIST: dict[str, tuple[str, ...]] = {
    "run": (
        "control_plane.transition_run",
        "runtime.prepare_claim",
        "runtime.record_event",
        "runtime.complete_claim",
        "runtime.suspend_claim",
        "runtime.complete_child_coordination",
        "runtime.fail_unprepared_claim",
        "tools.begin_call",
        "tools.finish_call",
        "tool_approvals.decide",
        "user_inputs.request",
        "user_inputs.answer",
        "coordination.cancel_tree",
    ),
    "runtime_session": (
        "runtime.prepare_claim",
        "runtime.bind_session",
        "runtime.record_event",
        "runtime.complete_claim",
        "runtime.suspend_claim",
        "runtime.native_projection",
        "tools.persist_pre_action_checkpoint",
        "tool_approvals.decide",
        "user_inputs.request",
        "user_inputs.answer",
        "coordination.cancel_tree",
    ),
    "run_step": (
        "control_plane.transition_run",
        "runtime.native_projection",
        "runtime.plan_projection",
        "tools.begin_call",
        "tools.finish_call",
        "tool_approvals.decide",
        "coordination.cancel_tree",
    ),
}

_TERMINAL = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.TIMED_OUT,
    }
)
_DURABLE_WAITS = frozenset(
    {
        RunStatus.WAITING_FOR_APPROVAL,
        RunStatus.WAITING_FOR_USER_INPUT,
        RunStatus.WAITING_FOR_SUBAGENT,
    }
)
_CLAIMABLE_ACTIVE = frozenset(
    {
        RunStatus.PENDING,
        RunStatus.PLANNING,
        RunStatus.RUNNING,
    }
)

RUN_LOOP_STATE_MAPPING: dict[RunStatus, frozenset[RuntimeLoopState]] = {
    RunStatus.PENDING: frozenset(),
    RunStatus.PLANNING: frozenset({RuntimeLoopState.INITIALIZING, RuntimeLoopState.PLANNING}),
    RunStatus.RUNNING: frozenset(
        {
            RuntimeLoopState.REASONING,
            RuntimeLoopState.EXECUTING,
            RuntimeLoopState.SEARCHING,
            RuntimeLoopState.FETCHING,
            RuntimeLoopState.OBSERVING,
            RuntimeLoopState.DELEGATING,
            RuntimeLoopState.REFLECTING,
            RuntimeLoopState.FINALIZING,
            RuntimeLoopState.CANCELLING,
        }
    ),
    RunStatus.WAITING_FOR_TOOL: frozenset({RuntimeLoopState.WAITING_FOR_TOOL}),
    RunStatus.WAITING_FOR_APPROVAL: frozenset({RuntimeLoopState.WAITING_FOR_APPROVAL}),
    RunStatus.WAITING_FOR_USER_INPUT: frozenset({RuntimeLoopState.WAITING_FOR_USER_INPUT}),
    RunStatus.WAITING_FOR_SUBAGENT: frozenset({RuntimeLoopState.WAITING_FOR_SUBAGENT}),
    RunStatus.PAUSED: frozenset({RuntimeLoopState.PAUSED}),
    RunStatus.COMPLETED: frozenset({RuntimeLoopState.COMPLETED}),
    RunStatus.FAILED: frozenset({RuntimeLoopState.FAILED, RuntimeLoopState.BUDGET_EXHAUSTED}),
    RunStatus.CANCELLED: frozenset({RuntimeLoopState.CANCELLED}),
    RunStatus.TIMED_OUT: frozenset({RuntimeLoopState.TIMED_OUT}),
}

_SENSITIVE_METADATA_KEYS = frozenset(
    {
        "argument",
        "arguments",
        "authorization",
        "credential",
        "lease_token",
        "password",
        "prompt",
        "raw",
        "secret",
        "token",
    }
)


@dataclass(frozen=True, slots=True)
class LifecycleTransition:
    source: RunStatus
    target: RunStatus
    reason: str
    metadata: dict[str, Any]
    revision: int
    lifecycle_revision: int
    claimable: bool


def validate_loop_state_mapping(status: RunStatus, loop_state: RuntimeLoopState) -> None:
    if loop_state not in RUN_LOOP_STATE_MAPPING[status]:
        raise ValueError(
            f"runtime loop state {loop_state.value} does not map to Run status {status.value}"
        )


def is_run_claimable(run: Run, *, at: datetime | None = None) -> bool:
    """Mirror the queue contract without treating durable waits as runnable."""

    now = at or datetime.now(UTC)
    status = RunStatus(run.status)
    lease_expired = run.lease_expires_at is None or run.lease_expires_at <= now
    if status in _CLAIMABLE_ACTIVE:
        return lease_expired
    # waiting_for_tool represents an in-flight effect. Only lease expiry opens
    # its recovery path; unlike the durable waits, it has no separate wake row.
    return (
        status is RunStatus.WAITING_FOR_TOOL and run.lease_expires_at is not None and lease_expired
    )


class RunLifecycleAuthority:
    """Apply one guarded transition inside the caller-owned transaction.

    The caller supplies an AsyncSession and an already locked Run. This method
    never opens, commits, or rolls back a transaction.
    """

    @classmethod
    async def transition(
        cls,
        session: AsyncSession,
        context: TenantContext,
        run: Run,
        *,
        target: RunStatus,
        reason: str,
        metadata: dict[str, Any] | None = None,
        loop_state: RuntimeLoopState | None = None,
        expected_source: RunStatus | None = None,
        expected_revision: int | None = None,
        lease_owner: str | None = None,
        lease_token: UUID | None = None,
        event_type: str = "RunStatusChanged",
        action: str = "run.lifecycle.transition",
        clear_lease: bool | None = None,
        occurred_at: datetime | None = None,
    ) -> LifecycleTransition:
        source = RunStatus(run.status)
        if expected_source is not None and source != expected_source:
            raise DomainConflict(
                "RUN_SOURCE_CONFLICT",
                f"run status is {source.value}, expected {expected_source.value}",
                details={
                    "actual_status": source.value,
                    "expected_status": expected_source.value,
                    "target_status": target.value,
                },
            )
        if expected_revision is not None:
            require_revision("run", expected=expected_revision, actual=run.revision)
        if lease_owner is not None or lease_token is not None:
            if (
                lease_owner is None
                or lease_token is None
                or run.lease_owner != lease_owner
                or run.lease_token != lease_token
            ):
                raise RuntimeLeaseLost(str(run.id))
        transition_state("run", source, target, RUN_TRANSITIONS)
        if loop_state is not None:
            validate_loop_state_mapping(target, loop_state)

        normalized_reason = cls._reason(reason)
        public_metadata = cls._metadata(metadata or {})
        now = occurred_at or datetime.now(UTC)
        run.status = target.value
        run.revision += 1
        run.lifecycle_revision = int(run.lifecycle_revision or 0) + 1
        run.lifecycle_reason = normalized_reason
        run.lifecycle_metadata = public_metadata
        if source is RunStatus.PENDING and target is RunStatus.PLANNING:
            run.started_at = run.started_at or now
        if target in _TERMINAL:
            run.ended_at = run.ended_at or now

        should_clear_lease = clear_lease
        if should_clear_lease is None:
            preserve_wait_handshake_lease = (
                source
                in {
                    RunStatus.WAITING_FOR_APPROVAL,
                    RunStatus.WAITING_FOR_USER_INPUT,
                }
                and target is RunStatus.RUNNING
                and run.lease_owner is not None
                and run.lease_token is not None
                and run.lease_expires_at is not None
                and run.lease_expires_at > now
            )
            should_clear_lease = (
                target in _TERMINAL
                or target in _DURABLE_WAITS
                or target is RunStatus.PAUSED
                or source in _DURABLE_WAITS
            ) and not preserve_wait_handshake_lease
        if should_clear_lease:
            cls.clear_lease(run)

        claimable = is_run_claimable(run, at=now)
        payload = {
            "contract_version": LIFECYCLE_CONTRACT_VERSION,
            "source": source.value,
            "target": target.value,
            "status": target.value,
            "reason": normalized_reason,
            "metadata": public_metadata,
            "revision": run.revision,
            "lifecycle_revision": run.lifecycle_revision,
            "claimable": claimable,
        }
        session.add(
            Event(
                tenant_id=run.tenant_id,
                event_type=event_type,
                aggregate_type="run",
                aggregate_id=run.id,
                run_id=run.id,
                actor_id=context.actor_id,
                payload=payload,
                correlation_id=context.correlation_id,
            )
        )
        session.add(
            AuditRecord(
                tenant_id=run.tenant_id,
                action=action,
                resource_type="run",
                resource_id=run.id,
                actor_id=context.actor_id,
                details=payload,
                correlation_id=context.correlation_id,
            )
        )
        return LifecycleTransition(
            source=source,
            target=target,
            reason=normalized_reason,
            metadata=public_metadata,
            revision=run.revision,
            lifecycle_revision=run.lifecycle_revision,
            claimable=claimable,
        )

    @staticmethod
    def clear_lease(run: Run) -> None:
        run.lease_owner = None
        run.lease_token = None
        run.lease_expires_at = None
        run.heartbeat_at = None

    @staticmethod
    def _reason(reason: str) -> str:
        value = reason.strip()
        if not value or len(value) > 150:
            raise ValueError("lifecycle reason must contain between 1 and 150 characters")
        return value

    @classmethod
    def _metadata(cls, metadata: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(metadata, dict):
            raise ValueError("lifecycle metadata must be an object")
        redacted = cls._redact_metadata(metadata)
        encoded = json.dumps(
            redacted,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if len(encoded) > MAX_LIFECYCLE_METADATA_BYTES:
            raise ValueError("lifecycle metadata must not exceed 16384 bytes")
        return redacted

    @classmethod
    def _redact_metadata(cls, value: Any, *, depth: int = 0) -> Any:
        if depth >= 4:
            return "[TRUNCATED]"
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for index, (raw_key, item) in enumerate(value.items()):
                if index >= 32:
                    result["_truncated"] = True
                    break
                key = str(raw_key)[:100]
                normalized = key.lower().replace("-", "_")
                if any(part in _SENSITIVE_METADATA_KEYS for part in normalized.split("_")):
                    result[key] = "[REDACTED]"
                else:
                    result[key] = cls._redact_metadata(item, depth=depth + 1)
            return result
        if isinstance(value, (list, tuple)):
            return [cls._redact_metadata(item, depth=depth + 1) for item in value[:32]]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value[:1000] if isinstance(value, str) else value
        return str(value)[:1000]
