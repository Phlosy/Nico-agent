import pytest

from nico_agent.domain.errors import InvalidStateTransition, RevisionConflict
from nico_agent.domain.states import (
    AGENT_TRANSITIONS,
    AGENT_VERSION_TRANSITIONS,
    PROJECT_TRANSITIONS,
    RUN_STEP_TRANSITIONS,
    RUN_TRANSITIONS,
    TASK_TRANSITIONS,
    AgentStatus,
    AgentVersionStatus,
    ProjectStatus,
    RunStatus,
    RunStepStatus,
    TaskStatus,
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
