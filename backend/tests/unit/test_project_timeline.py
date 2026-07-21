from __future__ import annotations

from uuid import UUID

from nico_agent.projects.read_service import ProjectSessionReadService


def test_timeline_payload_removes_private_runtime_fields_and_bounds_values() -> None:
    payload = {
        "status": "completed",
        "reason": "verified by tests",
        "trajectory": [{"thought": "private"}],
        "raw_reasoning": "private chain",
        "object_key": "tenant/private/result.txt",
        "arguments": {"token": "secret"},
        "nested": {
            "summary": "safe",
            "authorization": "Bearer secret",
            "long": "x" * 3_000,
        },
        "items": list(range(100)),
    }

    sanitized = ProjectSessionReadService.sanitize_payload(payload)

    assert sanitized["status"] == "completed"
    assert sanitized["reason"] == "verified by tests"
    assert sanitized["nested"]["summary"] == "safe"
    assert len(sanitized["nested"]["long"]) < 3_000
    assert len(sanitized["items"]) == 50
    assert {"trajectory", "raw_reasoning", "object_key", "arguments"}.isdisjoint(sanitized)
    assert "authorization" not in sanitized["nested"]


def test_timeline_entries_are_typed_and_link_to_authoritative_resources() -> None:
    run_id = UUID("11111111-1111-4111-8111-111111111111")
    resource_id = UUID("22222222-2222-4222-8222-222222222222")
    project_id = UUID("33333333-3333-4333-8333-333333333333")
    session_id = UUID("44444444-4444-4444-8444-444444444444")

    assert ProjectSessionReadService.entry_kind("PlanUpdated", "plan") == "plan"
    assert ProjectSessionReadService.entry_kind("ToolCallCompleted", "tool_call") == "tool"
    assert ProjectSessionReadService.entry_kind("ArtifactAvailable", "artifact") == "artifact"
    assert ProjectSessionReadService.entry_kind("DelegationAccepted", "delegation") == "delegation"
    assert ProjectSessionReadService.entry_kind("RunCompleted", "run") == "run"
    assert ProjectSessionReadService.links(
        project_id=project_id,
        session_id=session_id,
        aggregate_type="artifact",
        aggregate_id=resource_id,
        run_id=run_id,
    ) == {
        "session": (
            "/api/v1/projects/33333333-3333-4333-8333-333333333333/"
            "sessions/44444444-4444-4444-8444-444444444444"
        ),
        "run": "/api/v1/runs/11111111-1111-4111-8111-111111111111",
        "resource": "/api/v1/runs/11111111-1111-4111-8111-111111111111/artifacts",
        "content": (
            "/api/v1/runs/11111111-1111-4111-8111-111111111111/artifacts/"
            "22222222-2222-4222-8222-222222222222/content"
        ),
    }
