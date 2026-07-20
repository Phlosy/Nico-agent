"""Validated, provider-neutral contracts for native Plan revisions."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_STEP_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
DEFAULT_MAX_PLAN_STEPS = 16


class PlanStepDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    instruction: str = Field(min_length=1, max_length=8_000)
    acceptance: dict[str, Any] = Field(default_factory=dict)
    depends_on: tuple[str, ...] = ()

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not _STEP_KEY.fullmatch(value):
            raise ValueError("step key must be a stable lowercase identifier")
        return value


class PlanDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    objective: str = Field(min_length=1, max_length=4_000)
    steps: tuple[PlanStepDraft, ...] = Field(min_length=1)


def planner_response_format() -> dict[str, Any]:
    """OpenAI-compatible strict schema used for a billed planner ModelCall."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "nico_plan",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["objective", "steps"],
                "properties": {
                    "objective": {"type": "string", "minLength": 1},
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": DEFAULT_MAX_PLAN_STEPS,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "key",
                                "title",
                                "instruction",
                                "acceptance",
                                "depends_on",
                            ],
                            "properties": {
                                "key": {"type": "string"},
                                "title": {"type": "string"},
                                "instruction": {"type": "string"},
                                "acceptance": {"type": "object"},
                                "depends_on": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
        },
    }


def parse_plan(
    value: str | dict[str, Any], *, max_steps: int = DEFAULT_MAX_PLAN_STEPS
) -> PlanDraft:
    if max_steps < 1:
        raise ValueError("plan step budget must be positive")
    try:
        payload = json.loads(value) if isinstance(value, str) else value
        plan = PlanDraft.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValidationError) as exc:
        raise ValueError("planner returned an invalid structured Plan") from exc
    if len(plan.steps) > max_steps:
        raise ValueError("planner exceeded the Plan step budget")
    _validate_graph(plan)
    return plan


def _validate_graph(plan: PlanDraft) -> None:
    keys = [step.key for step in plan.steps]
    if len(keys) != len(set(keys)):
        raise ValueError("Plan step keys must be unique")
    known = set(keys)
    graph: dict[str, tuple[str, ...]] = {}
    for step in plan.steps:
        if step.key in step.depends_on:
            raise ValueError("Plan step cannot depend on itself")
        unknown = set(step.depends_on) - known
        if unknown:
            raise ValueError("Plan step references an unknown dependency")
        graph[step.key] = step.depends_on

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise ValueError("Plan dependency graph contains a cycle")
        if key in visited:
            return
        visiting.add(key)
        for dependency in graph[key]:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in keys:
        visit(key)


def ordered_steps(plan: PlanDraft) -> tuple[PlanStepDraft, ...]:
    """Return a deterministic topological order, preserving declared order for ties."""
    remaining = list(plan.steps)
    completed: set[str] = set()
    result: list[PlanStepDraft] = []
    while remaining:
        ready = [step for step in remaining if set(step.depends_on) <= completed]
        if not ready:  # Defensive; parse_plan already rejects cycles.
            raise ValueError("Plan has no executable step")
        for step in ready:
            result.append(step)
            completed.add(step.key)
            remaining.remove(step)
    return tuple(result)


def plan_content_hash(plan: PlanDraft) -> str:
    encoded = json.dumps(
        plan.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
