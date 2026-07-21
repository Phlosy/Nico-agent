from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nico_agent.projects.orchestration import (
    bounded_narrative,
    next_cadence_slot,
    supervision_facts,
)


def test_cadence_slot_is_bounded_and_skips_missed_backlog() -> None:
    now = datetime(2026, 7, 21, 8, 0, tzinfo=UTC)

    assert next_cadence_slot(now, 3600) == now + timedelta(hours=1)
    assert next_cadence_slot(now, None) is None


def test_supervision_facts_are_deterministic_and_model_narrative_is_bounded() -> None:
    facts = supervision_facts(
        project_id="project-1",
        member_statuses=["active", "active", "paused"],
        task_statuses=["completed", "failed", "running", "completed"],
        run_statuses=["completed", "failed", "timed_out"],
        delegation_statuses=["completed", "failed"],
        artifacts=[
            {"artifact_id": "artifact-2", "name": "report.txt", "run_id": "run-2"},
            {"artifact_id": "artifact-1", "name": "data.json", "run_id": "run-1"},
        ],
    )

    assert facts["progress"] == {"completed_tasks": 2, "total_tasks": 4, "percent": 50}
    assert facts["members"] == {"active": 2, "paused": 1}
    assert facts["blockers"] == [
        {"kind": "failed_tasks", "count": 1},
        {"kind": "failed_runs", "count": 1},
        {"kind": "timed_out_runs", "count": 1},
    ]
    assert [item["artifact_id"] for item in facts["artifact_refs"]] == [
        "artifact-1",
        "artifact-2",
    ]
    assert bounded_narrative({"content": " summary "}) == "summary"
    assert len(bounded_narrative({"content": "x" * 10_000}) or "") == 4_000
    assert bounded_narrative(None) is None
