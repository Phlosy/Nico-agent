"""Frozen DTOs for source snapshots, reflection providers, and generated candidates."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import to_jsonable_python


def canonical_json(value: Any) -> str:
    jsonable = to_jsonable_python(value)
    return json.dumps(jsonable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def skill_content_hash(value: SkillCandidateDraft | Any) -> str:
    if isinstance(value, SkillCandidateDraft):
        payload = value.model_dump(mode="json")
    else:
        payload = {
            "conditions": value.conditions,
            "preconditions": value.preconditions,
            "input_schema": value.input_schema,
            "steps": value.steps,
            "tools": value.tools,
            "output_schema": value.output_schema,
            "validation": value.validation,
            "failure_modes": value.failure_modes,
        }
    payload.pop("name_hint", None)
    payload.pop("description", None)
    return canonical_hash(payload)


class SnapshotToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    tool_definition_id: UUID
    name: str
    version: str
    status: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    usage: dict[str, Any] = Field(default_factory=dict)


class SnapshotStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    sequence: int
    kind: str
    status: str
    input: dict[str, Any]
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    tool_calls: tuple[SnapshotToolCall, ...] = ()


class SnapshotRuntime(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    provider: str
    provider_version: str
    protocol_version: str
    status: str
    usage: dict[str, Any]
    trajectory: dict[str, Any]


class TrajectorySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    run_id: UUID
    task_id: UUID
    project_id: UUID
    agent_id: UUID
    agent_version_id: UUID
    agent_version_content_hash: str
    platform_version: str
    run_status: str
    attempt: int
    task_title: str
    task_input: dict[str, Any]
    acceptance: dict[str, Any]
    role: str
    mandate: str
    model_config_data: dict[str, Any]
    run_result: dict[str, Any] | None = None
    run_error: dict[str, Any] | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    ended_at: datetime
    steps: tuple[SnapshotStep, ...]
    runtime: SnapshotRuntime | None = None
    snapshot_hash: str

    def payload_without_hash(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"snapshot_hash"})


class MemoryCandidateDraft(BaseModel):
    model_config = ConfigDict(frozen=True)

    memory_type: Literal["working", "episodic", "semantic", "procedural"]
    content: str = Field(min_length=1, max_length=100_000)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=2_000)


class SkillCandidateDraft(BaseModel):
    model_config = ConfigDict(frozen=True)

    name_hint: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=10_000)
    conditions: dict[str, Any]
    preconditions: list[dict[str, Any]]
    input_schema: dict[str, Any]
    steps: list[dict[str, Any]] = Field(min_length=1, max_length=128)
    tools: list[dict[str, Any]]
    output_schema: dict[str, Any]
    validation: dict[str, Any]
    failure_modes: list[dict[str, Any]]


class ReflectionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    memories: tuple[MemoryCandidateDraft, ...] = ()
    skill: SkillCandidateDraft | None = None
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_candidates(self) -> ReflectionResult:
        if not self.memories and self.skill is None:
            raise ValueError("reflection must produce at least one candidate")
        if len(self.memories) > 8:
            raise ValueError("reflection cannot produce more than 8 memory candidates")
        memory_types = [item.memory_type for item in self.memories]
        if len(memory_types) != len(set(memory_types)):
            raise ValueError("reflection cannot produce duplicate memory types")
        return self


@runtime_checkable
class ReflectionProvider(Protocol):
    name: str
    version: str

    async def reflect(self, snapshot: TrajectorySnapshot) -> ReflectionResult: ...


class GrowthPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    memory_scope: Literal["tenant", "project", "agent"] = "project"
    skill_scope: Literal["tenant", "project", "agent"] = "project"
    working_ttl_seconds: int = Field(default=86_400, ge=300, le=2_592_000)
    generate_semantic: bool = True
    generate_procedural: bool = True
    generate_skill: bool = True

    @property
    def content_hash(self) -> str:
        return canonical_hash(self)


class MemoryCandidateReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    memory_key: UUID
    memory_type: str
    scope_type: str
    content_hash: str
    source_hash: str


class SkillCandidateReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    skill_id: UUID
    skill_version_id: UUID
    name: str
    content_hash: str
    source_hash: str


class CandidateGenerationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: UUID
    snapshot_hash: str
    generator_name: str
    generator_version: str
    policy_hash: str
    memories: tuple[MemoryCandidateReference, ...]
    skill: SkillCandidateReference | None = None
    already_generated: bool
