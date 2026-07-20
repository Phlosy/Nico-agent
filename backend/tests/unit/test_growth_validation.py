from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from nico_agent.growth.contracts import skill_content_hash
from nico_agent.growth.evaluation import (
    DeterministicGrowthValidator,
    MemoryValidationPayload,
    ResolvedValidationTool,
    SkillValidationPayload,
    ValidationCheck,
    ValidationResult,
    ValidationSource,
    ValidationSubject,
)
from nico_agent.memory.chunking import content_hash

TENANT_ID = UUID("10000000-0000-0000-0000-000000000001")
SUBJECT_ID = UUID("10000000-0000-0000-0000-000000000002")
SKILL_ID = UUID("10000000-0000-0000-0000-000000000003")
RUN_ID = UUID("10000000-0000-0000-0000-000000000004")
STEP_ID = UUID("10000000-0000-0000-0000-000000000005")
AGENT_VERSION_ID = UUID("10000000-0000-0000-0000-000000000006")
TOOL_ID = UUID("10000000-0000-0000-0000-000000000007")


def _source(*, bound: bool = True) -> ValidationSource:
    return ValidationSource(
        id=UUID("10000000-0000-0000-0000-000000000008"),
        run_id=RUN_ID,
        run_step_id=STEP_ID,
        tool_call_id=None,
        runtime_session_id=None,
        agent_version_id=AGENT_VERSION_ID,
        trajectory_hash="a" * 64,
        generator_name="fixture",
        generator_version="1.0.0",
        source_hash="b" * 64,
        snapshot_bound=bound,
        run_terminal=True,
        step_terminal=True,
        tool_calls_terminal=True,
        runtime_terminal=True,
        observed_tools=(("research.search", "2.1.0", TOOL_ID),),
    )


def _memory_subject(*, stored_hash: str | None = None) -> ValidationSubject:
    content = "Approved memories remain scoped and auditable."
    memory = MemoryValidationPayload(
        id=SUBJECT_ID,
        memory_key=UUID("10000000-0000-0000-0000-000000000009"),
        version=1,
        memory_type="semantic",
        scope_type="tenant",
        project_id=None,
        agent_id=None,
        status="candidate",
        content=content,
        confidence=0.8,
        content_hash=stored_hash or content_hash(content),
        supersedes_id=None,
        expires_at=None,
    )
    return ValidationSubject(
        tenant_id=TENANT_ID,
        subject_type="memory",
        subject_id=SUBJECT_ID,
        content_hash=memory.content_hash,
        memory=memory,
        sources=(_source(),),
        snapshot_hash="c" * 64,
    )


def _skill_subject(*, schema: dict | None = None, bound: bool = True) -> ValidationSubject:
    payload = SkillValidationPayload(
        id=SUBJECT_ID,
        skill_id=SKILL_ID,
        skill_status="candidate",
        version=1,
        status="draft",
        conditions={"topic": "research"},
        preconditions=({"kind": "scope_authorized"},),
        input_schema=schema or {"type": "object", "properties": {}},
        steps=({"id": "search", "sequence": 1},),
        tools=(
            {
                "name": "research.search",
                "version": "2.1.0",
                "tool_definition_id": str(TOOL_ID),
            },
        ),
        output_schema={"type": "object", "properties": {}},
        validation={"required": True},
        failure_modes=({"code": "SOURCE_NOT_REPRODUCED"},),
        content_hash="0" * 64,
    )
    payload = payload.model_copy(update={"content_hash": skill_content_hash(payload)})
    return ValidationSubject(
        tenant_id=TENANT_ID,
        subject_type="skill_version",
        subject_id=SUBJECT_ID,
        content_hash=payload.content_hash,
        skill_version=payload,
        sources=(_source(bound=bound),),
        resolved_tools=(
            ResolvedValidationTool(
                id=TOOL_ID,
                name="research.search",
                version="2.1.0",
                status="enabled",
            ),
        ),
        snapshot_hash="d" * 64,
    )


@pytest.mark.asyncio
async def test_deterministic_memory_validation_passes_all_required_checks() -> None:
    result = await DeterministicGrowthValidator().validate(_memory_subject())

    assert result.verdict == "pass"
    assert result.score == 1.0
    assert all(check.passed for check in result.checks)
    assert {check.code for check in result.checks} >= {
        "content_hash",
        "scope_shape",
        "source_binding",
        "terminal_trajectory",
    }


@pytest.mark.asyncio
async def test_memory_hash_mismatch_is_a_failed_evaluation_not_an_exception() -> None:
    result = await DeterministicGrowthValidator().validate(_memory_subject(stored_hash="f" * 64))

    assert result.verdict == "fail"
    assert next(item for item in result.checks if item.code == "content_hash").passed is False


@pytest.mark.asyncio
async def test_skill_validation_requires_schema_enabled_tools_and_bound_source() -> None:
    valid = await DeterministicGrowthValidator().validate(_skill_subject())
    invalid = await DeterministicGrowthValidator().validate(
        _skill_subject(schema={"type": "not-a-json-schema-type"}, bound=False)
    )

    assert valid.verdict == "pass"
    assert invalid.verdict == "fail"
    failed = {item.code for item in invalid.checks if not item.passed}
    assert {"input_schema", "source_binding"}.issubset(failed)


def test_validation_result_cannot_turn_a_failed_check_into_a_pass() -> None:
    with pytest.raises(ValidationError, match="verdict must match"):
        ValidationResult(
            verdict="pass",
            score=1.0,
            checks=(
                ValidationCheck(
                    code="source_binding",
                    passed=False,
                    message="source binding failed",
                ),
            ),
        )
