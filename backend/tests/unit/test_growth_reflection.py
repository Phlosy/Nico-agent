from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from nico_agent.growth.contracts import (
    GrowthPolicy,
    MemoryCandidateDraft,
    ReflectionResult,
    SnapshotStep,
    SnapshotToolCall,
    TrajectorySnapshot,
    canonical_hash,
)
from nico_agent.growth.reflection import DeterministicReflectionProvider
from nico_agent.growth.snapshot import _bounded_redact

TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")
RUN_ID = UUID("00000000-0000-0000-0000-000000000002")
TASK_ID = UUID("00000000-0000-0000-0000-000000000003")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000004")
AGENT_ID = UUID("00000000-0000-0000-0000-000000000005")
AGENT_VERSION_ID = UUID("00000000-0000-0000-0000-000000000006")
STEP_ID = UUID("00000000-0000-0000-0000-000000000007")
CALL_ID = UUID("00000000-0000-0000-0000-000000000008")
TOOL_ID = UUID("00000000-0000-0000-0000-000000000009")


def _snapshot(*, status: str = "completed") -> TrajectorySnapshot:
    payload = {
        "tenant_id": TENANT_ID,
        "run_id": RUN_ID,
        "task_id": TASK_ID,
        "project_id": PROJECT_ID,
        "agent_id": AGENT_ID,
        "agent_version_id": AGENT_VERSION_ID,
        "agent_version_content_hash": "a" * 64,
        "platform_version": "0.1.0",
        "run_status": status,
        "attempt": 1,
        "task_title": "Verify candidate publication",
        "task_input": {"candidate": "memory"},
        "acceptance": {"required": "approved"},
        "role": "researcher",
        "mandate": "Produce auditable evidence",
        "model_config_data": {"model": "fixture"},
        "run_result": {"summary": "validated"} if status == "completed" else None,
        "run_error": None if status == "completed" else {"code": "VALIDATION_FAILED"},
        "usage": {"input_tokens": 10},
        "ended_at": datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        "steps": (
            SnapshotStep(
                id=STEP_ID,
                sequence=1,
                kind="tool",
                status="completed",
                input={"query": "candidate"},
                output={"validated": status == "completed"},
                tool_calls=(
                    SnapshotToolCall(
                        id=CALL_ID,
                        tool_definition_id=TOOL_ID,
                        name="memory.validate",
                        version="2.3.1",
                        status="succeeded",
                        arguments={"candidate": "memory"},
                        result={"valid": True},
                    ),
                ),
            ),
        ),
        "runtime": None,
    }
    return TrajectorySnapshot(**payload, snapshot_hash=canonical_hash(payload))


def _memory_draft(memory_type: str = "semantic") -> MemoryCandidateDraft:
    return MemoryCandidateDraft(
        memory_type=memory_type,
        content="A validated candidate remains inactive until approval.",
        confidence=0.8,
        rationale="Observed in the source trajectory.",
    )


@pytest.mark.asyncio
async def test_completed_reflection_produces_bounded_candidate_dtos() -> None:
    snapshot = _snapshot()

    result = await DeterministicReflectionProvider().reflect(snapshot)

    assert [item.memory_type for item in result.memories] == [
        "episodic",
        "semantic",
        "procedural",
    ]
    assert result.skill is not None
    assert result.skill.tools == [
        {
            "name": "memory.validate",
            "version": "2.3.1",
            "tool_definition_id": str(TOOL_ID),
        }
    ]
    assert result.skill.validation["source_run_status"] == "completed"
    assert result.skill.steps[0]["sequence"] == 1


@pytest.mark.asyncio
async def test_unsuccessful_reflection_is_recovery_only() -> None:
    snapshot = _snapshot(status="failed")

    result = await DeterministicReflectionProvider().reflect(snapshot)

    assert [item.memory_type for item in result.memories] == ["episodic", "working"]
    assert result.skill is None


def test_snapshot_hash_is_stable_across_nested_dto_serialization() -> None:
    snapshot = _snapshot()

    assert canonical_hash(snapshot.payload_without_hash()) == snapshot.snapshot_hash
    assert canonical_hash(snapshot) == canonical_hash(snapshot.model_dump(mode="json"))


def test_growth_contracts_reject_team_scope_and_duplicate_memory_types() -> None:
    with pytest.raises(ValidationError):
        GrowthPolicy(memory_scope="team")  # type: ignore[arg-type]

    with pytest.raises(ValidationError, match="duplicate memory types"):
        ReflectionResult(memories=(_memory_draft(), _memory_draft()))


def test_snapshot_redaction_is_recursive_bounded_and_deterministic() -> None:
    value = {
        "api_key": "never-store-this",
        "nested": {
            "message": "authorization=Bearer-deadbeef",
            "items": list(range(205)),
        },
        "long": "x" * 8_100,
    }

    first = _bounded_redact(value)
    second = _bounded_redact(value)

    assert first == second
    assert first["api_key"] == "[REDACTED]"
    assert first["nested"]["message"] == "[REDACTED]"
    assert first["nested"]["items"][-1] == "[TRUNCATED]"
    assert first["long"].endswith("[TRUNCATED]")
    assert "never-store-this" not in str(first)
    assert "deadbeef" not in str(first)
