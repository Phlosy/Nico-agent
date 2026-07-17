"""Explicit state machines with no framework or persistence dependency."""

from __future__ import annotations

from enum import StrEnum
from typing import TypeVar

from nico_agent.domain.errors import InvalidStateTransition, RevisionConflict


class AgentStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    ARCHIVED = "archived"
    ERROR = "error"


class AgentVersionStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class TaskStatus(StrEnum):
    CREATED = "created"
    ASSIGNED = "assigned"
    RUNNING = "running"
    WAITING_FOR_REVIEW = "waiting_for_review"
    REVISION_REQUIRED = "revision_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_FOR_TOOL = "waiting_for_tool"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class RunStepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolDefinitionStatus(StrEnum):
    DRAFT = "draft"
    ENABLED = "enabled"
    DISABLED = "disabled"


class ToolCallStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class MemoryType(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class MemoryScope(StrEnum):
    TENANT = "tenant"
    PROJECT = "project"
    AGENT = "agent"
    TEAM = "team"


class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"
    DELETED = "deleted"


class SkillStatus(StrEnum):
    CANDIDATE = "candidate"
    TESTING = "testing"
    APPROVED = "approved"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"


class SkillVersionStatus(StrEnum):
    DRAFT = "draft"
    TESTING = "testing"
    PUBLISHED = "published"
    REJECTED = "rejected"


class EvaluationStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class ApprovalStatus(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class SkillDeploymentStatus(StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"


AGENT_TRANSITIONS = {
    AgentStatus.DRAFT: {AgentStatus.READY, AgentStatus.ARCHIVED},
    AgentStatus.READY: {AgentStatus.RUNNING, AgentStatus.ARCHIVED},
    AgentStatus.RUNNING: {AgentStatus.READY, AgentStatus.PAUSED, AgentStatus.ERROR},
    AgentStatus.PAUSED: {AgentStatus.RUNNING, AgentStatus.ARCHIVED},
    AgentStatus.ARCHIVED: {AgentStatus.READY},
    AgentStatus.ERROR: {AgentStatus.READY},
}

AGENT_VERSION_TRANSITIONS = {
    AgentVersionStatus.DRAFT: {AgentVersionStatus.PUBLISHED},
    AgentVersionStatus.PUBLISHED: {AgentVersionStatus.SUPERSEDED},
    AgentVersionStatus.SUPERSEDED: {AgentVersionStatus.PUBLISHED},
}

PROJECT_TRANSITIONS = {
    ProjectStatus.ACTIVE: {ProjectStatus.ARCHIVED},
    ProjectStatus.ARCHIVED: {ProjectStatus.ACTIVE},
}

TASK_TRANSITIONS = {
    TaskStatus.CREATED: {TaskStatus.ASSIGNED, TaskStatus.CANCELLED},
    TaskStatus.ASSIGNED: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {
        TaskStatus.WAITING_FOR_REVIEW,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.WAITING_FOR_REVIEW: {
        TaskStatus.REVISION_REQUIRED,
        TaskStatus.COMPLETED,
    },
    TaskStatus.REVISION_REQUIRED: {TaskStatus.RUNNING, TaskStatus.FAILED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}

RUN_TRANSITIONS = {
    RunStatus.PENDING: {
        RunStatus.PLANNING,
        RunStatus.CANCELLED,
        RunStatus.TIMED_OUT,
    },
    RunStatus.PLANNING: {
        RunStatus.RUNNING,
        RunStatus.PAUSED,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.TIMED_OUT,
    },
    RunStatus.RUNNING: {
        RunStatus.WAITING_FOR_TOOL,
        RunStatus.WAITING_FOR_APPROVAL,
        RunStatus.PAUSED,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.TIMED_OUT,
    },
    RunStatus.WAITING_FOR_TOOL: {RunStatus.RUNNING, RunStatus.CANCELLED},
    RunStatus.WAITING_FOR_APPROVAL: {
        RunStatus.RUNNING,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.PAUSED: {RunStatus.RUNNING, RunStatus.CANCELLED},
    RunStatus.COMPLETED: set(),
    RunStatus.FAILED: set(),
    RunStatus.CANCELLED: set(),
    RunStatus.TIMED_OUT: set(),
}

RUN_STEP_TRANSITIONS = {
    RunStepStatus.PENDING: {RunStepStatus.RUNNING, RunStepStatus.CANCELLED},
    RunStepStatus.RUNNING: {
        RunStepStatus.WAITING,
        RunStepStatus.COMPLETED,
        RunStepStatus.FAILED,
        RunStepStatus.CANCELLED,
    },
    RunStepStatus.WAITING: {
        RunStepStatus.RUNNING,
        RunStepStatus.FAILED,
        RunStepStatus.CANCELLED,
    },
    RunStepStatus.COMPLETED: set(),
    RunStepStatus.FAILED: set(),
    RunStepStatus.CANCELLED: set(),
}

TOOL_DEFINITION_TRANSITIONS = {
    ToolDefinitionStatus.DRAFT: {
        ToolDefinitionStatus.ENABLED,
        ToolDefinitionStatus.DISABLED,
    },
    ToolDefinitionStatus.ENABLED: {ToolDefinitionStatus.DISABLED},
    ToolDefinitionStatus.DISABLED: {ToolDefinitionStatus.ENABLED},
}

TOOL_CALL_TRANSITIONS = {
    ToolCallStatus.PENDING: {ToolCallStatus.RUNNING, ToolCallStatus.CANCELLED},
    ToolCallStatus.RUNNING: {
        ToolCallStatus.SUCCEEDED,
        ToolCallStatus.FAILED,
        ToolCallStatus.TIMED_OUT,
        ToolCallStatus.CANCELLED,
    },
    ToolCallStatus.SUCCEEDED: set(),
    ToolCallStatus.FAILED: set(),
    ToolCallStatus.TIMED_OUT: set(),
    ToolCallStatus.CANCELLED: set(),
}

MEMORY_TRANSITIONS = {
    MemoryStatus.CANDIDATE: {MemoryStatus.ACTIVE, MemoryStatus.DELETED},
    MemoryStatus.ACTIVE: {
        MemoryStatus.INVALIDATED,
        MemoryStatus.EXPIRED,
        MemoryStatus.DELETED,
    },
    MemoryStatus.INVALIDATED: {MemoryStatus.DELETED},
    MemoryStatus.EXPIRED: {MemoryStatus.DELETED},
    MemoryStatus.DELETED: set(),
}

SKILL_TRANSITIONS = {
    SkillStatus.CANDIDATE: {SkillStatus.TESTING, SkillStatus.DISABLED},
    SkillStatus.TESTING: {
        SkillStatus.CANDIDATE,
        SkillStatus.APPROVED,
        SkillStatus.DISABLED,
    },
    SkillStatus.APPROVED: {SkillStatus.PUBLISHED, SkillStatus.DISABLED},
    SkillStatus.PUBLISHED: {SkillStatus.DEPRECATED, SkillStatus.DISABLED},
    SkillStatus.DEPRECATED: {SkillStatus.PUBLISHED, SkillStatus.DISABLED},
    SkillStatus.DISABLED: set(),
}

SKILL_VERSION_TRANSITIONS = {
    SkillVersionStatus.DRAFT: {SkillVersionStatus.TESTING, SkillVersionStatus.REJECTED},
    SkillVersionStatus.TESTING: {
        SkillVersionStatus.PUBLISHED,
        SkillVersionStatus.REJECTED,
    },
    SkillVersionStatus.PUBLISHED: set(),
    SkillVersionStatus.REJECTED: set(),
}

EVALUATION_TRANSITIONS = {
    EvaluationStatus.PENDING: {EvaluationStatus.COMPLETED, EvaluationStatus.FAILED},
    EvaluationStatus.COMPLETED: set(),
    EvaluationStatus.FAILED: set(),
}

APPROVAL_TRANSITIONS = {
    ApprovalStatus.REQUESTED: {
        ApprovalStatus.APPROVED,
        ApprovalStatus.REJECTED,
        ApprovalStatus.CANCELLED,
        ApprovalStatus.EXPIRED,
    },
    ApprovalStatus.APPROVED: set(),
    ApprovalStatus.REJECTED: set(),
    ApprovalStatus.CANCELLED: set(),
    ApprovalStatus.EXPIRED: set(),
}

SKILL_DEPLOYMENT_TRANSITIONS = {
    SkillDeploymentStatus.ACTIVE: {SkillDeploymentStatus.RETIRED},
    SkillDeploymentStatus.RETIRED: set(),
}

StateT = TypeVar("StateT", bound=StrEnum)


def transition_state(
    entity: str,
    current: StateT,
    target: StateT,
    transitions: dict[StateT, set[StateT]],
) -> StateT:
    if target not in transitions[current]:
        raise InvalidStateTransition(entity, current.value, target.value)
    return target


def require_revision(entity: str, *, expected: int, actual: int) -> None:
    if expected != actual:
        raise RevisionConflict(entity, expected, actual)
