"""Frozen public contracts and deterministic helpers for versioned Skills."""

from __future__ import annotations

import hashlib
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from nico_agent.growth.contracts import canonical_hash

SKILL_CONTENT_FIELDS = (
    "conditions",
    "preconditions",
    "input_schema",
    "steps",
    "tools",
    "output_schema",
    "validation",
    "failure_modes",
)


class SkillVersionDraft(BaseModel):
    model_config = ConfigDict(frozen=True)

    conditions: dict[str, Any]
    preconditions: tuple[dict[str, Any], ...]
    input_schema: dict[str, Any]
    steps: tuple[dict[str, Any], ...] = Field(min_length=1, max_length=128)
    tools: tuple[dict[str, Any], ...]
    output_schema: dict[str, Any]
    validation: dict[str, Any]
    failure_modes: tuple[dict[str, Any], ...] = Field(min_length=1, max_length=128)


class SkillVersionReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    skill_id: UUID
    skill_version_id: UUID
    version: int
    skill_status: str
    version_status: str
    content_hash: str
    skill_revision: int
    version_revision: int
    current_version_id: UUID | None
    already_in_state: bool = False


class SkillFieldChange(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: str
    before_hash: str
    after_hash: str


class SkillVersionComparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    skill_id: UUID
    from_version_id: UUID
    from_version: int
    from_content_hash: str
    to_version_id: UUID
    to_version: int
    to_content_hash: str
    direction: Literal["same", "upgrade", "rollback"]
    changed_fields: tuple[str, ...]
    changes: tuple[SkillFieldChange, ...]
    added_tools: tuple[str, ...]
    removed_tools: tuple[str, ...]
    identical: bool


class SkillDeploymentReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    skill_id: UUID
    skill_version_id: UUID
    scope_type: str
    project_id: UUID | None
    agent_id: UUID | None
    rollout_percentage: int
    status: str
    revision: int
    skill_revision: int
    already_in_state: bool = False


class SkillResolution(BaseModel):
    model_config = ConfigDict(frozen=True)

    skill_id: UUID
    skill_version_id: UUID
    version: int
    run_id: UUID
    selection: Literal["stable", "canary"]
    deployment_id: UUID | None = None
    rollout_percentage: int | None = None
    bucket: int | None = None


def stable_rollout_bucket(run_id: UUID, deployment_id: UUID) -> int:
    """Map a persisted Run/deployment pair to a stable bucket in ``[0, 99]``."""

    digest = hashlib.sha256(f"{run_id}:{deployment_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 100


def compare_skill_versions(
    *,
    skill_id: UUID,
    from_version_id: UUID,
    from_version: int,
    from_content_hash: str,
    from_payload: dict[str, Any],
    to_version_id: UUID,
    to_version: int,
    to_content_hash: str,
    to_payload: dict[str, Any],
) -> SkillVersionComparison:
    changes: list[SkillFieldChange] = []
    for field in SKILL_CONTENT_FIELDS:
        before_hash = canonical_hash(from_payload[field])
        after_hash = canonical_hash(to_payload[field])
        if before_hash != after_hash:
            changes.append(
                SkillFieldChange(
                    field=field,
                    before_hash=before_hash,
                    after_hash=after_hash,
                )
            )
    before_tools = _tool_identities(from_payload["tools"])
    after_tools = _tool_identities(to_payload["tools"])
    direction: Literal["same", "upgrade", "rollback"]
    if to_version == from_version:
        direction = "same"
    elif to_version > from_version:
        direction = "upgrade"
    else:
        direction = "rollback"
    return SkillVersionComparison(
        skill_id=skill_id,
        from_version_id=from_version_id,
        from_version=from_version,
        from_content_hash=from_content_hash,
        to_version_id=to_version_id,
        to_version=to_version,
        to_content_hash=to_content_hash,
        direction=direction,
        changed_fields=tuple(item.field for item in changes),
        changes=tuple(changes),
        added_tools=tuple(sorted(after_tools - before_tools)),
        removed_tools=tuple(sorted(before_tools - after_tools)),
        identical=not changes and from_content_hash == to_content_hash,
    )


def version_payload(value: Any) -> dict[str, Any]:
    return {field: getattr(value, field) for field in SKILL_CONTENT_FIELDS}


def _tool_identities(tools: Any) -> set[str]:
    result: set[str] = set()
    for item in tools if isinstance(tools, (list, tuple)) else ():
        if not isinstance(item, dict):
            result.add(f"malformed:{canonical_hash(item)}")
            continue
        result.add(
            f"{item.get('name', '')}@{item.get('version', '')}#{item.get('tool_definition_id', '')}"
        )
    return result
