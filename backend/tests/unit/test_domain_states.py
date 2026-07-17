import pytest

from nico_agent.domain.errors import InvalidStateTransition, RevisionConflict
from nico_agent.domain.states import (
    AGENT_TRANSITIONS,
    AGENT_VERSION_TRANSITIONS,
    APPROVAL_TRANSITIONS,
    EVALUATION_TRANSITIONS,
    MEMORY_TRANSITIONS,
    PROJECT_TRANSITIONS,
    RUN_STEP_TRANSITIONS,
    RUN_TRANSITIONS,
    SKILL_DEPLOYMENT_TRANSITIONS,
    SKILL_TRANSITIONS,
    SKILL_VERSION_TRANSITIONS,
    TASK_TRANSITIONS,
    TOOL_CALL_TRANSITIONS,
    TOOL_DEFINITION_TRANSITIONS,
    AgentStatus,
    AgentVersionStatus,
    ApprovalStatus,
    EvaluationStatus,
    MemoryStatus,
    ProjectStatus,
    RunStatus,
    RunStepStatus,
    SkillDeploymentStatus,
    SkillStatus,
    SkillVersionStatus,
    TaskStatus,
    ToolCallStatus,
    ToolDefinitionStatus,
    require_revision,
    transition_state,
)


@pytest.mark.parametrize(
    ("entity", "transitions"),
    [
        ("agent", AGENT_TRANSITIONS),
        ("agent_version", AGENT_VERSION_TRANSITIONS),
        ("project", PROJECT_TRANSITIONS),
        ("task", TASK_TRANSITIONS),
        ("run", RUN_TRANSITIONS),
        ("run_step", RUN_STEP_TRANSITIONS),
        ("tool_definition", TOOL_DEFINITION_TRANSITIONS),
        ("tool_call", TOOL_CALL_TRANSITIONS),
        ("memory", MEMORY_TRANSITIONS),
        ("skill", SKILL_TRANSITIONS),
        ("skill_version", SKILL_VERSION_TRANSITIONS),
        ("evaluation", EVALUATION_TRANSITIONS),
        ("approval", APPROVAL_TRANSITIONS),
        ("skill_deployment", SKILL_DEPLOYMENT_TRANSITIONS),
    ],
)
def test_every_declared_transition_is_accepted(entity: str, transitions: dict) -> None:
    for current, targets in transitions.items():
        for target in targets:
            assert transition_state(entity, current, target, transitions) is target


@pytest.mark.parametrize(
    ("entity", "current", "target", "transitions"),
    [
        ("agent", AgentStatus.DRAFT, AgentStatus.RUNNING, AGENT_TRANSITIONS),
        (
            "agent_version",
            AgentVersionStatus.PUBLISHED,
            AgentVersionStatus.DRAFT,
            AGENT_VERSION_TRANSITIONS,
        ),
        ("project", ProjectStatus.ACTIVE, ProjectStatus.ACTIVE, PROJECT_TRANSITIONS),
        ("task", TaskStatus.CREATED, TaskStatus.COMPLETED, TASK_TRANSITIONS),
        ("run", RunStatus.COMPLETED, RunStatus.RUNNING, RUN_TRANSITIONS),
        ("run_step", RunStepStatus.COMPLETED, RunStepStatus.RUNNING, RUN_STEP_TRANSITIONS),
        (
            "tool_definition",
            ToolDefinitionStatus.ENABLED,
            ToolDefinitionStatus.DRAFT,
            TOOL_DEFINITION_TRANSITIONS,
        ),
        (
            "tool_call",
            ToolCallStatus.SUCCEEDED,
            ToolCallStatus.RUNNING,
            TOOL_CALL_TRANSITIONS,
        ),
        ("memory", MemoryStatus.DELETED, MemoryStatus.ACTIVE, MEMORY_TRANSITIONS),
        ("skill", SkillStatus.DISABLED, SkillStatus.PUBLISHED, SKILL_TRANSITIONS),
        (
            "skill_version",
            SkillVersionStatus.PUBLISHED,
            SkillVersionStatus.TESTING,
            SKILL_VERSION_TRANSITIONS,
        ),
        (
            "evaluation",
            EvaluationStatus.COMPLETED,
            EvaluationStatus.PENDING,
            EVALUATION_TRANSITIONS,
        ),
        (
            "approval",
            ApprovalStatus.APPROVED,
            ApprovalStatus.REQUESTED,
            APPROVAL_TRANSITIONS,
        ),
        (
            "skill_deployment",
            SkillDeploymentStatus.RETIRED,
            SkillDeploymentStatus.ACTIVE,
            SKILL_DEPLOYMENT_TRANSITIONS,
        ),
    ],
)
def test_invalid_transitions_return_a_stable_error(
    entity: str,
    current,
    target,
    transitions: dict,
) -> None:
    with pytest.raises(InvalidStateTransition) as captured:
        transition_state(entity, current, target, transitions)

    assert captured.value.code == "INVALID_STATE_TRANSITION"
    assert captured.value.details == {
        "entity": entity,
        "current": current.value,
        "target": target.value,
    }


def test_revision_guard_accepts_only_the_current_revision() -> None:
    require_revision("agent", expected=3, actual=3)

    with pytest.raises(RevisionConflict) as captured:
        require_revision("agent", expected=2, actual=3)

    assert captured.value.code == "REVISION_CONFLICT"
    assert captured.value.details["actual_revision"] == 3
