from __future__ import annotations

from uuid import UUID

from nico_agent.growth.contracts import canonical_hash
from nico_agent.skills.contracts import compare_skill_versions, stable_rollout_bucket

SKILL_ID = UUID("00000000-0000-0000-0000-000000000001")
VERSION_ONE_ID = UUID("00000000-0000-0000-0000-000000000011")
VERSION_TWO_ID = UUID("00000000-0000-0000-0000-000000000012")
RUN_ID = UUID("00000000-0000-0000-0000-000000000021")
DEPLOYMENT_ID = UUID("00000000-0000-0000-0000-000000000031")


def _payload(*, condition: str = "ready", tools: list[dict] | None = None) -> dict:
    return {
        "conditions": {"when": condition},
        "preconditions": [{"kind": "terminal_source"}],
        "input_schema": {"type": "object"},
        "steps": [{"id": "validate", "action": "evaluate"}],
        "tools": tools or [],
        "output_schema": {"type": "object"},
        "validation": {"minimum_score": 0.8},
        "failure_modes": [{"code": "VALIDATION_FAILED"}],
    }


def test_rollout_bucket_is_stable_and_bounded() -> None:
    first = stable_rollout_bucket(RUN_ID, DEPLOYMENT_ID)
    second = stable_rollout_bucket(RUN_ID, DEPLOYMENT_ID)

    assert first == second
    assert 0 <= first <= 99
    assert (
        stable_rollout_bucket(UUID("00000000-0000-0000-0000-000000000022"), DEPLOYMENT_ID) != first
    )


def test_skill_comparison_reports_changed_fields_and_exact_tools() -> None:
    old_tool = {
        "name": "http.read",
        "version": "1.0.0",
        "tool_definition_id": "00000000-0000-0000-0000-000000000041",
    }
    new_tool = {
        "name": "database.read",
        "version": "1.0.0",
        "tool_definition_id": "00000000-0000-0000-0000-000000000042",
    }
    before = _payload(tools=[old_tool])
    after = _payload(condition="reviewed", tools=[new_tool])

    comparison = compare_skill_versions(
        skill_id=SKILL_ID,
        from_version_id=VERSION_ONE_ID,
        from_version=1,
        from_content_hash=canonical_hash(before),
        from_payload=before,
        to_version_id=VERSION_TWO_ID,
        to_version=2,
        to_content_hash=canonical_hash(after),
        to_payload=after,
    )

    assert comparison.direction == "upgrade"
    assert comparison.changed_fields == ("conditions", "tools")
    assert comparison.removed_tools == ("http.read@1.0.0#00000000-0000-0000-0000-000000000041",)
    assert comparison.added_tools == ("database.read@1.0.0#00000000-0000-0000-0000-000000000042",)
    assert comparison.identical is False


def test_identical_comparison_is_stable_and_directional() -> None:
    payload = _payload()
    digest = canonical_hash(payload)
    comparison = compare_skill_versions(
        skill_id=SKILL_ID,
        from_version_id=VERSION_TWO_ID,
        from_version=2,
        from_content_hash=digest,
        from_payload=payload,
        to_version_id=VERSION_ONE_ID,
        to_version=1,
        to_content_hash=digest,
        to_payload=payload,
    )

    assert comparison.direction == "rollback"
    assert comparison.changed_fields == ()
    assert comparison.identical is True
