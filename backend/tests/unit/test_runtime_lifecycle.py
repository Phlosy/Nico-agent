from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest

from nico_agent.database import TenantContext
from nico_agent.domain.errors import InvalidStateTransition
from nico_agent.domain.models import AuditRecord, Event, Run
from nico_agent.domain.states import RUN_TRANSITIONS, RunStatus
from nico_agent.runtime.contracts import RuntimeLoopState
from nico_agent.runtime.errors import RuntimeLeaseLost
from nico_agent.runtime.lifecycle import (
    LIFECYCLE_SQL_PORTS,
    LIFECYCLE_WRITER_ALLOWLIST,
    RunLifecycleAuthority,
    is_run_claimable,
    validate_loop_state_mapping,
)


def _run(status: RunStatus, *, revision: int = 3) -> Run:
    now = datetime.now(UTC)
    return Run(
        id=uuid4(),
        tenant_id=uuid4(),
        task_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        attempt=1,
        status=status.value,
        revision=revision,
        lifecycle_revision=0,
        lifecycle_metadata={},
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_transition_persists_redacted_reason_event_audit_and_revision() -> None:
    run = _run(RunStatus.PENDING)
    session = Mock()
    context = TenantContext(run.tenant_id, "worker:one", uuid4())

    outcome = await RunLifecycleAuthority.transition(
        session,
        context,
        run,
        target=RunStatus.PLANNING,
        reason="runtime_claim_prepared",
        metadata={
            "worker_id": "one",
            "trace_id": "trace-1",
            "lease_token": "must-not-leak",
            "nested": {"arguments": {"password": "must-not-leak"}},
        },
        loop_state=RuntimeLoopState.INITIALIZING,
        event_type="RunPlanningStarted",
        action="runtime.claim",
    )

    assert outcome.source is RunStatus.PENDING
    assert outcome.target is RunStatus.PLANNING
    assert outcome.claimable is True
    assert run.status == RunStatus.PLANNING.value
    assert run.revision == 4
    assert run.lifecycle_revision == 1
    assert run.lifecycle_reason == "runtime_claim_prepared"
    assert run.lifecycle_metadata == {
        "worker_id": "one",
        "trace_id": "trace-1",
        "lease_token": "[REDACTED]",
        "nested": {"arguments": "[REDACTED]"},
    }
    added = [call.args[0] for call in session.add.call_args_list]
    assert len([item for item in added if isinstance(item, Event)]) == 1
    assert len([item for item in added if isinstance(item, AuditRecord)]) == 1
    event = next(item for item in added if isinstance(item, Event))
    assert event.run_id == run.id
    assert event.payload["source"] == "pending"
    assert event.payload["target"] == "planning"
    assert event.payload["status"] == "planning"
    assert event.payload["reason"] == "runtime_claim_prepared"
    assert event.payload["metadata"] == run.lifecycle_metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "target"),
    [
        (RunStatus.RUNNING, RunStatus.WAITING_FOR_USER_INPUT),
        (RunStatus.WAITING_FOR_USER_INPUT, RunStatus.RUNNING),
        (RunStatus.WAITING_FOR_USER_INPUT, RunStatus.CANCELLED),
    ],
)
async def test_reserved_user_input_state_has_no_executable_transitions(
    source: RunStatus, target: RunStatus
) -> None:
    assert target not in RUN_TRANSITIONS[source]
    run = _run(source)
    session = Mock()
    context = TenantContext(run.tenant_id, "api:test", uuid4())

    with pytest.raises(InvalidStateTransition):
        await RunLifecycleAuthority.transition(
            session,
            context,
            run,
            target=target,
            reason="reserved_u1_transition",
        )

    assert run.status == source.value
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_approval_wake_preserves_only_a_live_handshake_lease() -> None:
    now = datetime.now(UTC)
    run = _run(RunStatus.WAITING_FOR_APPROVAL)
    run.lease_owner = "worker:one"
    run.lease_token = uuid4()
    run.lease_expires_at = now + timedelta(minutes=1)
    run.heartbeat_at = now
    lease_token = run.lease_token
    session = Mock()
    context = TenantContext(run.tenant_id, "operator:test", uuid4())

    await RunLifecycleAuthority.transition(
        session,
        context,
        run,
        target=RunStatus.RUNNING,
        reason="approval_decided",
        occurred_at=now,
    )

    assert run.lease_owner == "worker:one"
    assert run.lease_token == lease_token
    assert run.lease_expires_at == now + timedelta(minutes=1)
    assert run.heartbeat_at == now

    expired = _run(RunStatus.WAITING_FOR_APPROVAL)
    expired.lease_owner = "worker:old"
    expired.lease_token = uuid4()
    expired.lease_expires_at = now - timedelta(seconds=1)
    expired.heartbeat_at = now - timedelta(minutes=1)
    await RunLifecycleAuthority.transition(
        Mock(),
        context,
        expired,
        target=RunStatus.RUNNING,
        reason="approval_decided_after_lease_expiry",
        occurred_at=now,
    )
    assert expired.lease_owner is None
    assert expired.lease_token is None
    assert expired.lease_expires_at is None
    assert expired.heartbeat_at is None


@pytest.mark.asyncio
async def test_illegal_and_terminal_transitions_do_not_mutate_or_emit() -> None:
    run = _run(RunStatus.COMPLETED, revision=8)
    session = Mock()
    context = TenantContext(run.tenant_id, "api:test", uuid4())

    with pytest.raises(InvalidStateTransition):
        await RunLifecycleAuthority.transition(
            session,
            context,
            run,
            target=RunStatus.RUNNING,
            reason="late_wake",
        )

    assert run.status == RunStatus.COMPLETED.value
    assert run.revision == 8
    assert run.lifecycle_revision == 0
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_stale_lease_owner_is_rejected_before_mutation() -> None:
    run = _run(RunStatus.RUNNING, revision=5)
    run.lease_owner = "worker:new"
    run.lease_token = uuid4()
    session = Mock()
    context = TenantContext(run.tenant_id, "worker:old", uuid4())

    with pytest.raises(RuntimeLeaseLost):
        await RunLifecycleAuthority.transition(
            session,
            context,
            run,
            target=RunStatus.COMPLETED,
            reason="provider_completed",
            lease_owner="worker:old",
            lease_token=uuid4(),
        )

    assert run.status == RunStatus.RUNNING.value
    assert run.revision == 5
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_expected_source_conflict_rejects_otherwise_legal_transition() -> None:
    run = _run(RunStatus.RUNNING, revision=5)
    session = Mock()
    context = TenantContext(run.tenant_id, "worker:one", uuid4())

    with pytest.raises(Exception) as captured:
        await RunLifecycleAuthority.transition(
            session,
            context,
            run,
            target=RunStatus.COMPLETED,
            reason="stale_completion",
            expected_source=RunStatus.PLANNING,
        )

    assert getattr(captured.value, "code", None) == "RUN_SOURCE_CONFLICT"
    assert run.status == RunStatus.RUNNING.value
    assert run.revision == 5
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_oversized_metadata_is_rejected_before_mutation() -> None:
    run = _run(RunStatus.PENDING)
    session = Mock()
    context = TenantContext(run.tenant_id, "worker:one", uuid4())

    with pytest.raises(ValueError, match="must not exceed 16384 bytes"):
        await RunLifecycleAuthority.transition(
            session,
            context,
            run,
            target=RunStatus.PLANNING,
            reason="metadata_too_large",
            metadata={f"field_{index}": "x" * 1_000 for index in range(32)},
        )

    assert run.status == RunStatus.PENDING.value
    assert run.revision == 3
    session.add.assert_not_called()


@pytest.mark.parametrize(
    ("status", "loop_state"),
    [
        (RunStatus.PLANNING, RuntimeLoopState.INITIALIZING),
        (RunStatus.PLANNING, RuntimeLoopState.PLANNING),
        (RunStatus.RUNNING, RuntimeLoopState.REASONING),
        (RunStatus.RUNNING, RuntimeLoopState.OBSERVING),
        (RunStatus.RUNNING, RuntimeLoopState.DELEGATING),
        (RunStatus.RUNNING, RuntimeLoopState.REFLECTING),
        (RunStatus.RUNNING, RuntimeLoopState.FINALIZING),
        (RunStatus.RUNNING, RuntimeLoopState.CANCELLING),
        (RunStatus.WAITING_FOR_TOOL, RuntimeLoopState.WAITING_FOR_TOOL),
        (RunStatus.WAITING_FOR_APPROVAL, RuntimeLoopState.WAITING_FOR_APPROVAL),
        (RunStatus.WAITING_FOR_USER_INPUT, RuntimeLoopState.WAITING_FOR_USER_INPUT),
        (RunStatus.WAITING_FOR_SUBAGENT, RuntimeLoopState.WAITING_FOR_SUBAGENT),
        (RunStatus.FAILED, RuntimeLoopState.BUDGET_EXHAUSTED),
        (RunStatus.COMPLETED, RuntimeLoopState.COMPLETED),
        (RunStatus.CANCELLED, RuntimeLoopState.CANCELLED),
        (RunStatus.TIMED_OUT, RuntimeLoopState.TIMED_OUT),
    ],
)
def test_explicit_coarse_and_detailed_state_mapping(
    status: RunStatus, loop_state: RuntimeLoopState
) -> None:
    validate_loop_state_mapping(status, loop_state)


def test_incompatible_coarse_and_detailed_state_is_rejected() -> None:
    with pytest.raises(ValueError, match="does not map"):
        validate_loop_state_mapping(RunStatus.COMPLETED, RuntimeLoopState.REASONING)


def test_wait_claimability_and_expired_tool_recovery_contract() -> None:
    now = datetime.now(UTC)
    pending = _run(RunStatus.PENDING)
    assert is_run_claimable(pending, at=now)

    for status in (
        RunStatus.WAITING_FOR_APPROVAL,
        RunStatus.WAITING_FOR_USER_INPUT,
        RunStatus.WAITING_FOR_SUBAGENT,
    ):
        assert not is_run_claimable(_run(status), at=now)

    tool_wait = _run(RunStatus.WAITING_FOR_TOOL)
    tool_wait.lease_owner = "worker:dead"
    tool_wait.lease_token = uuid4()
    tool_wait.lease_expires_at = now + timedelta(seconds=1)
    assert not is_run_claimable(tool_wait, at=now)
    tool_wait.lease_expires_at = now - timedelta(seconds=1)
    assert is_run_claimable(tool_wait, at=now)


def test_executable_writer_allowlist_names_every_exception_port() -> None:
    assert set(LIFECYCLE_SQL_PORTS) == {
        "claim_next_run",
        "reconcile_expired_tool_approvals",
        "reconcile_coordination_waiters",
    }
    assert {
        "control_plane.transition_run",
        "runtime.prepare_claim",
        "runtime.record_event",
        "runtime.complete_claim",
        "runtime.suspend_claim",
        "runtime.fail_unprepared_claim",
        "tools.begin_call",
        "tools.finish_call",
        "tool_approvals.decide",
        "coordination.cancel_tree",
    } <= set(LIFECYCLE_WRITER_ALLOWLIST["run"])


def test_application_run_status_writers_are_routed_through_the_authority() -> None:
    backend = Path(__file__).parents[2]
    writer_files = (
        "src/nico_agent/control_plane.py",
        "src/nico_agent/runtime/service.py",
        "src/nico_agent/tools/gateway.py",
        "src/nico_agent/tool_approvals/service.py",
        "src/nico_agent/coordination/service.py",
    )
    direct_assignment = re.compile(r"\b(?:run|parent)\.status\s*=(?!=)")

    violations = {
        path: [
            line_number
            for line_number, line in enumerate(
                (backend / path).read_text(encoding="utf-8").splitlines(), start=1
            )
            if direct_assignment.search(line)
        ]
        for path in writer_files
    }

    assert violations == {path: [] for path in writer_files}
