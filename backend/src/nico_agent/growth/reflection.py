"""Deterministic no-tool reflection baseline producing candidate DTOs only."""

from __future__ import annotations

import json
from typing import Any

from nico_agent.growth.contracts import (
    MemoryCandidateDraft,
    ReflectionResult,
    SkillCandidateDraft,
    TrajectorySnapshot,
)
from nico_agent.memory.chunking import normalize_text


class DeterministicReflectionProvider:
    name = "nico-deterministic-reflection"
    version = "1.0.0"

    async def reflect(self, snapshot: TrajectorySnapshot) -> ReflectionResult:
        memories = [self._episodic(snapshot)]
        skill: SkillCandidateDraft | None = None
        if snapshot.run_status == "completed":
            memories.append(self._semantic(snapshot))
            memories.append(self._procedural(snapshot))
            skill = self._skill(snapshot)
        else:
            memories.append(self._working(snapshot))
        return ReflectionResult(
            memories=tuple(memories),
            skill=skill,
            notes=(
                "Candidates require deterministic validation and explicit approval.",
                "No candidate is active or published by the reflection provider.",
            ),
        )

    def _episodic(self, snapshot: TrajectorySnapshot) -> MemoryCandidateDraft:
        lines = [
            f'Task "{snapshot.task_title}" ended as {snapshot.run_status} '
            f"on attempt {snapshot.attempt}.",
            f"Result: {_compact(snapshot.run_result or snapshot.run_error or {})}",
            "Observed steps:",
        ]
        lines.extend(
            f"{step.sequence}. {step.kind} [{step.status}] "
            f"{_compact(step.output or step.error or {})}"
            for step in snapshot.steps
        )
        return MemoryCandidateDraft(
            memory_type="episodic",
            content=normalize_text("\n".join(lines)),
            confidence=0.8 if snapshot.run_status == "completed" else 0.65,
            rationale=("Preserve the auditable outcome and observations of one terminal run."),
        )

    def _semantic(self, snapshot: TrajectorySnapshot) -> MemoryCandidateDraft:
        result = snapshot.run_result or {}
        content = normalize_text(
            f'Candidate knowledge extracted from completed task "{snapshot.task_title}": '
            f"{_compact(result)}"
        )
        return MemoryCandidateDraft(
            memory_type="semantic",
            content=content,
            confidence=0.7,
            rationale=(
                "Extract a candidate fact from a successful result; validation remains required."
            ),
        )

    def _procedural(self, snapshot: TrajectorySnapshot) -> MemoryCandidateDraft:
        lines = [f'Observed procedure for "{snapshot.task_title}":']
        lines.extend(
            f"{index}. Execute {step.kind}; observed outcome: "
            f"{_compact(step.output or step.error or {})}"
            for index, step in enumerate(snapshot.steps, start=1)
        )
        return MemoryCandidateDraft(
            memory_type="procedural",
            content=normalize_text("\n".join(lines)),
            confidence=0.65,
            rationale=(
                "Capture an observed sequence as candidate procedure, not executable authority."
            ),
        )

    def _working(self, snapshot: TrajectorySnapshot) -> MemoryCandidateDraft:
        content = normalize_text(
            f'Follow-up context for unfinished outcome "{snapshot.task_title}" '
            f"({snapshot.run_status}): {_compact(snapshot.run_error or snapshot.run_result or {})}"
        )
        return MemoryCandidateDraft(
            memory_type="working",
            content=content,
            confidence=0.6,
            rationale=(
                "Retain short-lived recovery context for a failed or interrupted terminal run."
            ),
        )

    def _skill(self, snapshot: TrajectorySnapshot) -> SkillCandidateDraft:
        tools: dict[tuple[str, str], dict[str, Any]] = {}
        failure_modes: list[dict[str, Any]] = []
        for step in snapshot.steps:
            if step.status != "completed":
                failure_modes.append(
                    {"code": f"STEP_{step.status.upper()}", "step_kind": step.kind}
                )
            for call in step.tool_calls:
                tools[(call.name, call.version)] = {
                    "name": call.name,
                    "version": call.version,
                    "tool_definition_id": str(call.tool_definition_id),
                }
                if call.status != "succeeded":
                    failure_modes.append(
                        {
                            "code": f"TOOL_{call.status.upper()}",
                            "tool": f"{call.name}@{call.version}",
                        }
                    )
        if not failure_modes:
            failure_modes.append(
                {"code": "SOURCE_OUTCOME_NOT_REPRODUCED", "handling": "reject validation"}
            )
        return SkillCandidateDraft(
            name_hint=snapshot.task_title[:160],
            description=f"Candidate skill observed from completed task: {snapshot.task_title}",
            conditions={"task_title": snapshot.task_title},
            preconditions=[
                {"kind": "source_status", "required": "completed"},
                {"kind": "scope_authorized", "required": True},
            ],
            input_schema=_schema_for(snapshot.task_input),
            steps=[
                {
                    "id": f"step-{step.sequence}",
                    "sequence": step.sequence,
                    "kind": step.kind,
                    "instruction": f"Execute the observed {step.kind} step.",
                }
                for step in snapshot.steps
            ],
            tools=[tools[key] for key in sorted(tools)],
            output_schema=_schema_for(snapshot.run_result or {}),
            validation={
                "source_run_status": "completed",
                "acceptance": snapshot.acceptance,
                "required_step_count": len(snapshot.steps),
            },
            failure_modes=failure_modes,
        )


def _compact(value: Any, *, max_chars: int = 2_000) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return rendered if len(rendered) <= max_chars else rendered[:max_chars] + "[TRUNCATED]"


def _schema_for(value: dict[str, Any]) -> dict[str, Any]:
    properties = {key: {"type": _json_type(item)} for key, item in sorted(value.items())}
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(properties),
        "additionalProperties": False,
    }


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "null"
