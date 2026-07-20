from __future__ import annotations

import pytest

from nico_agent.runtime.native.completion import evaluate_completion
from nico_agent.runtime.native.planner import PlanDraft, parse_plan
from nico_agent.runtime.native.reflection import parse_reflection


def test_planner_accepts_bounded_three_step_dag() -> None:
    plan = parse_plan(
        {
            "objective": "Produce a verified report",
            "steps": [
                {
                    "key": "collect",
                    "title": "Collect evidence",
                    "instruction": "Collect the required evidence.",
                    "acceptance": {"non_empty": True},
                },
                {
                    "key": "analyze",
                    "title": "Analyze evidence",
                    "instruction": "Analyze the collected evidence.",
                    "depends_on": ["collect"],
                    "acceptance": {"non_empty": True},
                },
                {
                    "key": "report",
                    "title": "Write report",
                    "instruction": "Write the final report.",
                    "depends_on": ["analyze"],
                    "acceptance": {"non_empty": True},
                },
            ],
        }
    )

    assert isinstance(plan, PlanDraft)
    assert [step.key for step in plan.steps] == ["collect", "analyze", "report"]
    assert plan.steps[-1].depends_on == ("analyze",)


@pytest.mark.parametrize(
    "steps",
    [
        [
            {"key": "a", "title": "A", "instruction": "A", "depends_on": ["b"]},
            {"key": "b", "title": "B", "instruction": "B", "depends_on": ["a"]},
        ],
        [
            {"key": "same", "title": "A", "instruction": "A"},
            {"key": "same", "title": "B", "instruction": "B"},
        ],
        [{"key": "a", "title": "A", "instruction": "A", "depends_on": ["missing"]}],
    ],
)
def test_planner_rejects_cycle_duplicate_and_unknown_dependency(steps: list[dict]) -> None:
    with pytest.raises(ValueError):
        parse_plan({"objective": "invalid", "steps": steps})


def test_planner_rejects_invalid_model_structure_and_step_budget() -> None:
    with pytest.raises(ValueError):
        parse_plan({"objective": "bad", "steps": "not-a-list"})
    with pytest.raises(ValueError, match="budget"):
        parse_plan(
            {
                "objective": "too large",
                "steps": [
                    {"key": f"s-{index}", "title": "S", "instruction": "S"} for index in range(3)
                ],
            },
            max_steps=2,
        )


def test_reflection_has_a_closed_decision_set() -> None:
    reflection = parse_reflection(
        {
            "decision": "replan",
            "reason": "The evidence source failed validation.",
            "recovery_instruction": "Use the fallback source and rebuild the report.",
        }
    )
    assert reflection.decision == "replan"

    with pytest.raises(ValueError):
        parse_reflection({"decision": "ignore-policy", "reason": "no"})


def test_completion_checks_acceptance_and_json_schema_deterministically() -> None:
    passed = evaluate_completion(
        {"content": "verified", "score": 0.9},
        acceptance={
            "non_empty": True,
            "required": ["content", "score"],
            "output_schema": {
                "type": "object",
                "required": ["content", "score"],
                "properties": {
                    "content": {"type": "string", "minLength": 1},
                    "score": {"type": "number", "minimum": 0.8},
                },
            },
        },
        evidence_refs=("plan:2:report",),
    )
    assert passed.passed is True
    assert passed.output_hash
    assert passed.evidence_refs == ("plan:2:report",)

    rejected = evaluate_completion(
        {"content": "", "score": 0.5},
        acceptance={
            "non_empty": True,
            "output_schema": {
                "type": "object",
                "properties": {"score": {"type": "number", "minimum": 0.8}},
            },
        },
    )
    assert rejected.passed is False
    assert {check.code for check in rejected.checks if not check.passed} == {
        "NON_EMPTY_REQUIRED",
        "OUTPUT_SCHEMA_INVALID",
    }
