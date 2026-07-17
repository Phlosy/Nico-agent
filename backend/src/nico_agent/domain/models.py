"""Goal C relational models for the generic control-plane core."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from nico_agent.database import Base
from nico_agent.domain.states import (
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
)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'suspended', 'archived')", name="ck_tenants_status"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class Project(Base, TimestampMixin):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'archived')", name="ck_projects_status"),
        UniqueConstraint("tenant_id", "id", name="uq_projects_tenant_id_id"),
        UniqueConstraint("tenant_id", "name", name="uq_projects_tenant_name"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=ProjectStatus.ACTIVE.value
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class Agent(Base, TimestampMixin):
    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'ready', 'running', 'paused', 'archived', 'error')",
            name="ck_agents_status",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "id", "current_version_id"],
            ["agent_versions.tenant_id", "agent_versions.agent_id", "agent_versions.id"],
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_agents_current_version",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_agents_tenant_id_id"),
        UniqueConstraint("tenant_id", "name", name="uq_agents_tenant_name"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=AgentStatus.DRAFT.value
    )
    current_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class AgentVersion(Base, TimestampMixin):
    __tablename__ = "agent_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'published', 'superseded')",
            name="ck_agent_versions_status",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="RESTRICT",
            name="fk_agent_versions_tenant_agent",
        ),
        UniqueConstraint("tenant_id", "agent_id", "id", name="uq_agent_versions_scope_id"),
        UniqueConstraint("tenant_id", "id", name="uq_agent_versions_tenant_id_id"),
        UniqueConstraint(
            "tenant_id", "agent_id", "version", name="uq_agent_versions_scope_version"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    agent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=AgentVersionStatus.DRAFT.value
    )
    role: Mapped[str] = mapped_column(String(120), nullable=False)
    mandate: Mapped[str] = mapped_column(Text, nullable=False)
    boundaries: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    long_term_goal: Mapped[str | None] = mapped_column(Text)
    current_goal: Mapped[str | None] = mapped_column(Text)
    model_config_json: Mapped[dict[str, Any]] = mapped_column(
        "model_config", JSONB, nullable=False, server_default="{}"
    )
    tool_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    memory_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    skill_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    plugin_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    budgets: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    run_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created', 'assigned', 'running', 'waiting_for_review', "
            "'revision_required', 'completed', 'failed', 'cancelled')",
            name="ck_tasks_status",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_tasks_tenant_project",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "assignee_agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="RESTRICT",
            name="fk_tasks_tenant_assignee",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "parent_task_id"],
            ["tasks.tenant_id", "tasks.id"],
            ondelete="RESTRICT",
            name="fk_tasks_tenant_parent",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_tasks_tenant_id_id"),
        Index("ix_tasks_tenant_status_created", "tenant_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    assignee_agent_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    parent_task_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    acceptance: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=TaskStatus.CREATED.value
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class Run(Base, TimestampMixin):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'planning', 'running', 'waiting_for_tool', "
            "'waiting_for_approval', 'paused', 'completed', 'failed', 'cancelled', "
            "'timed_out')",
            name="ck_runs_status",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.id"],
            ondelete="RESTRICT",
            name="fk_runs_tenant_task",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="RESTRICT",
            name="fk_runs_tenant_agent",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "agent_id", "agent_version_id"],
            ["agent_versions.tenant_id", "agent_versions.agent_id", "agent_versions.id"],
            ondelete="RESTRICT",
            name="fk_runs_tenant_agent_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "retry_of_run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_runs_tenant_retry",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_runs_tenant_id_id"),
        UniqueConstraint("tenant_id", "task_id", "attempt", name="uq_runs_task_attempt"),
        Index("ix_runs_claimable", "status", "lease_expires_at", "created_at"),
        Index("ix_runs_tenant_task_created", "tenant_id", "task_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    agent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    agent_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    retry_of_run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=RunStatus.PENDING.value
    )
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False, server_default="64")
    token_budget: Mapped[int | None] = mapped_column(BigInteger)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    budgets: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    cost: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class RuntimeSession(Base, TimestampMixin):
    __tablename__ = "runtime_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')",
            name="ck_runtime_sessions_status",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_runtime_sessions_tenant_run",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_runtime_sessions_tenant_id_id"),
        UniqueConstraint("tenant_id", "run_id", name="uq_runtime_sessions_tenant_run"),
        UniqueConstraint("tenant_id", "run_id", "id", name="uq_runtime_sessions_tenant_run_id"),
        Index("ix_runtime_sessions_tenant_status", "tenant_id", "status", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_version: Mapped[str] = mapped_column(String(100), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(32), nullable=False)
    external_session_id: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="created")
    capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    provider_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    trajectory: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    tool_policy_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    last_event_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class RunStep(Base, TimestampMixin):
    __tablename__ = "run_steps"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'waiting', 'completed', 'failed', 'cancelled')",
            name="ck_run_steps_status",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_run_steps_tenant_run",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_run_steps_tenant_id_id"),
        UniqueConstraint("tenant_id", "run_id", "id", name="uq_run_steps_tenant_run_id"),
        UniqueConstraint("tenant_id", "run_id", "sequence", name="uq_run_steps_sequence"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=RunStepStatus.PENDING.value
    )
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class ToolDefinition(Base, TimestampMixin):
    __tablename__ = "tool_definitions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'enabled', 'disabled')",
            name="ck_tool_definitions_status",
        ),
        CheckConstraint(
            "risk IN ('low', 'medium', 'high')",
            name="ck_tool_definitions_risk",
        ),
        CheckConstraint("timeout_seconds > 0", name="ck_tool_definitions_timeout"),
        CheckConstraint("max_output_bytes > 0", name="ck_tool_definitions_output_limit"),
        CheckConstraint(
            "length(implementation_hash) = 64 AND length(content_hash) = 64",
            name="ck_tool_definitions_hashes",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_tool_definitions_tenant_id_id"),
        UniqueConstraint(
            "tenant_id", "name", "version", name="uq_tool_definitions_tenant_name_version"
        ),
        Index(
            "ix_tool_definitions_tenant_status_name",
            "tenant_id",
            "status",
            "name",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=ToolDefinitionStatus.DRAFT.value
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    permission: Mapped[str] = mapped_column(String(150), nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    retry_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    isolation_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    risk: Mapped[str] = mapped_column(String(20), nullable=False, server_default="low")
    max_output_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    implementation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class ToolCall(Base, TimestampMixin):
    __tablename__ = "tool_calls"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'timed_out', 'cancelled')",
            name="ck_tool_calls_status",
        ),
        CheckConstraint("length(arguments_hash) = 64", name="ck_tool_calls_arguments_hash"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_tool_calls_tenant_run",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "run_step_id"],
            ["run_steps.tenant_id", "run_steps.run_id", "run_steps.id"],
            ondelete="RESTRICT",
            name="fk_tool_calls_tenant_run_step",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "tool_definition_id"],
            ["tool_definitions.tenant_id", "tool_definitions.id"],
            ondelete="RESTRICT",
            name="fk_tool_calls_tenant_definition",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_tool_calls_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "run_step_id",
            "id",
            name="uq_tool_calls_tenant_run_step_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "tool_definition_id",
            "idempotency_key",
            name="uq_tool_calls_run_tool_idempotency",
        ),
        Index("ix_tool_calls_tenant_run_created", "tenant_id", "run_id", "created_at"),
        Index("ix_tool_calls_tenant_status", "tenant_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_step_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tool_definition_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(50), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    caller: Mapped[str] = mapped_column(String(200), nullable=False)
    execution_owner: Mapped[str | None] = mapped_column(String(200))
    execution_lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=ToolCallStatus.PENDING.value
    )
    attempts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class Memory(Base, TimestampMixin):
    __tablename__ = "memories"
    __table_args__ = (
        CheckConstraint(
            "memory_type IN ('working', 'episodic', 'semantic', 'procedural')",
            name="ck_memories_type",
        ),
        CheckConstraint(
            "status IN ('candidate', 'active', 'invalidated', 'expired', 'deleted')",
            name="ck_memories_status",
        ),
        CheckConstraint(
            "scope_type IN ('tenant', 'project', 'agent')",
            name="ck_memories_scope_goal_f",
        ),
        CheckConstraint(
            "(scope_type = 'tenant' AND project_id IS NULL AND agent_id IS NULL) OR "
            "(scope_type = 'project' AND project_id IS NOT NULL AND agent_id IS NULL) OR "
            "(scope_type = 'agent' AND project_id IS NULL AND agent_id IS NOT NULL)",
            name="ck_memories_scope_owner",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_memories_confidence"),
        CheckConstraint("version > 0", name="ck_memories_version_positive"),
        CheckConstraint("length(content_hash) = 64", name="ck_memories_content_hash"),
        CheckConstraint(
            "memory_type <> 'working' OR expires_at IS NOT NULL",
            name="ck_memories_working_expiry",
        ),
        CheckConstraint(
            "status <> 'active' OR approved_at IS NOT NULL",
            name="ck_memories_active_approved",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_memories_tenant_project",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="RESTRICT",
            name="fk_memories_tenant_agent",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "supersedes_id"],
            ["memories.tenant_id", "memories.id"],
            ondelete="RESTRICT",
            name="fk_memories_tenant_supersedes",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_memories_tenant_id_id"),
        UniqueConstraint("tenant_id", "memory_key", "version", name="uq_memories_key_version"),
        Index(
            "uq_memories_active_key",
            "tenant_id",
            "memory_key",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_memories_tenant_scope_status",
            "tenant_id",
            "scope_type",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    memory_key: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, server_default=text("gen_random_uuid()")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    agent_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=MemoryStatus.CANDIDATE.value
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class MemoryChunk(Base):
    __tablename__ = "memory_chunks"
    __table_args__ = (
        CheckConstraint("chunk_index >= 0", name="ck_memory_chunks_index"),
        CheckConstraint(
            "start_offset >= 0 AND end_offset > start_offset",
            name="ck_memory_chunks_offsets",
        ),
        CheckConstraint("embedding_dimension = 384", name="ck_memory_chunks_dimension"),
        CheckConstraint(
            "length(content_hash) = 64 AND length(memory_content_hash) = 64",
            name="ck_memory_chunks_hashes",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "memory_id"],
            ["memories.tenant_id", "memories.id"],
            ondelete="RESTRICT",
            name="fk_memory_chunks_tenant_memory",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_memory_chunks_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "memory_id",
            "embedding_provider",
            "embedding_version",
            "chunker_version",
            "chunk_index",
            name="uq_memory_chunks_profile_index",
        ),
        Index(
            "ix_memory_chunks_tenant_memory",
            "tenant_id",
            "memory_id",
            "embedding_provider",
            "embedding_version",
        ),
        Index(
            "ix_memory_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    memory_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    memory_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    chunker_name: Mapped[str] = mapped_column(String(120), nullable=False)
    chunker_version: Mapped[str] = mapped_column(String(80), nullable=False)
    embedding_provider: Mapped[str] = mapped_column(String(120), nullable=False)
    embedding_version: Mapped[str] = mapped_column(String(80), nullable=False)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(384), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Skill(Base, TimestampMixin):
    __tablename__ = "skills"
    __table_args__ = (
        CheckConstraint(
            "status IN ('candidate', 'testing', 'approved', 'published', 'deprecated', 'disabled')",
            name="ck_skills_status",
        ),
        CheckConstraint(
            "scope_type IN ('tenant', 'project', 'agent')",
            name="ck_skills_scope_goal_f",
        ),
        CheckConstraint(
            "(scope_type = 'tenant' AND project_id IS NULL AND agent_id IS NULL) OR "
            "(scope_type = 'project' AND project_id IS NOT NULL AND agent_id IS NULL) OR "
            "(scope_type = 'agent' AND project_id IS NULL AND agent_id IS NOT NULL)",
            name="ck_skills_scope_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_skills_tenant_project",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="RESTRICT",
            name="fk_skills_tenant_agent",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "id", "current_version_id"],
            ["skill_versions.tenant_id", "skill_versions.skill_id", "skill_versions.id"],
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_skills_current_version",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_skills_tenant_id_id"),
        UniqueConstraint("tenant_id", "name", name="uq_skills_tenant_name"),
        Index("ix_skills_tenant_status", "tenant_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    agent_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=SkillStatus.CANDIDATE.value
    )
    current_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    success_stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class SkillVersion(Base, TimestampMixin):
    __tablename__ = "skill_versions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'testing', 'published', 'rejected')",
            name="ck_skill_versions_status",
        ),
        CheckConstraint("version > 0", name="ck_skill_versions_version_positive"),
        CheckConstraint("length(content_hash) = 64", name="ck_skill_versions_content_hash"),
        CheckConstraint(
            "status <> 'published' OR (approved_at IS NOT NULL AND published_at IS NOT NULL)",
            name="ck_skill_versions_published_approved",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "skill_id"],
            ["skills.tenant_id", "skills.id"],
            ondelete="RESTRICT",
            name="fk_skill_versions_tenant_skill",
        ),
        UniqueConstraint("tenant_id", "skill_id", "id", name="uq_skill_versions_tenant_skill_id"),
        UniqueConstraint("tenant_id", "id", name="uq_skill_versions_tenant_id_id"),
        UniqueConstraint(
            "tenant_id", "skill_id", "version", name="uq_skill_versions_skill_version"
        ),
        Index("ix_skill_versions_tenant_status", "tenant_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    skill_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=SkillVersionStatus.DRAFT.value
    )
    conditions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    preconditions: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    tools: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    validation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    failure_modes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class GrowthSource(Base):
    __tablename__ = "growth_sources"
    __table_args__ = (
        CheckConstraint(
            "subject_type IN ('memory', 'skill_version')",
            name="ck_growth_sources_subject_type",
        ),
        CheckConstraint(
            "(subject_type = 'memory' AND memory_id IS NOT NULL AND "
            "skill_version_id IS NULL) OR "
            "(subject_type = 'skill_version' AND memory_id IS NULL AND "
            "skill_version_id IS NOT NULL)",
            name="ck_growth_sources_subject",
        ),
        CheckConstraint(
            "length(trajectory_hash) = 64 AND length(source_hash) = 64",
            name="ck_growth_sources_hashes",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "memory_id"],
            ["memories.tenant_id", "memories.id"],
            ondelete="RESTRICT",
            name="fk_growth_sources_tenant_memory",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "skill_version_id"],
            ["skill_versions.tenant_id", "skill_versions.id"],
            ondelete="RESTRICT",
            name="fk_growth_sources_tenant_skill_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_growth_sources_tenant_run",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "run_step_id"],
            ["run_steps.tenant_id", "run_steps.run_id", "run_steps.id"],
            ondelete="RESTRICT",
            name="fk_growth_sources_tenant_run_step",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "run_step_id", "tool_call_id"],
            [
                "tool_calls.tenant_id",
                "tool_calls.run_id",
                "tool_calls.run_step_id",
                "tool_calls.id",
            ],
            ondelete="RESTRICT",
            name="fk_growth_sources_tenant_tool_call",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "runtime_session_id"],
            ["runtime_sessions.tenant_id", "runtime_sessions.run_id", "runtime_sessions.id"],
            ondelete="RESTRICT",
            name="fk_growth_sources_tenant_runtime_session",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "agent_version_id"],
            ["agent_versions.tenant_id", "agent_versions.id"],
            ondelete="RESTRICT",
            name="fk_growth_sources_tenant_agent_version",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_growth_sources_tenant_id_id"),
        Index(
            "uq_growth_sources_memory_hash",
            "tenant_id",
            "memory_id",
            "source_hash",
            unique=True,
            postgresql_where=text("subject_type = 'memory'"),
        ),
        Index(
            "uq_growth_sources_skill_hash",
            "tenant_id",
            "skill_version_id",
            "source_hash",
            unique=True,
            postgresql_where=text("subject_type = 'skill_version'"),
        ),
        Index("ix_growth_sources_tenant_run", "tenant_id", "run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    memory_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    skill_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_step_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tool_call_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    runtime_session_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    agent_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    trajectory_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    generator_name: Mapped[str] = mapped_column(String(120), nullable=False)
    generator_version: Mapped[str] = mapped_column(String(80), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Evaluation(Base, TimestampMixin):
    __tablename__ = "evaluations"
    __table_args__ = (
        CheckConstraint(
            "subject_type IN ('memory', 'skill_version')",
            name="ck_evaluations_subject_type",
        ),
        CheckConstraint(
            "(subject_type = 'memory' AND memory_id IS NOT NULL AND "
            "skill_version_id IS NULL) OR "
            "(subject_type = 'skill_version' AND memory_id IS NULL AND "
            "skill_version_id IS NOT NULL)",
            name="ck_evaluations_subject",
        ),
        CheckConstraint(
            "status IN ('pending', 'completed', 'failed')", name="ck_evaluations_status"
        ),
        CheckConstraint(
            "verdict IS NULL OR verdict IN ('pass', 'fail')",
            name="ck_evaluations_verdict",
        ),
        CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1)", name="ck_evaluations_score"
        ),
        CheckConstraint(
            "(status = 'pending' AND verdict IS NULL AND ended_at IS NULL) OR "
            "(status = 'completed' AND verdict IS NOT NULL AND ended_at IS NOT NULL) OR "
            "(status = 'failed' AND verdict IS NULL AND error IS NOT NULL AND "
            "ended_at IS NOT NULL)",
            name="ck_evaluations_terminal_shape",
        ),
        CheckConstraint("length(content_hash) = 64", name="ck_evaluations_content_hash"),
        ForeignKeyConstraint(
            ["tenant_id", "memory_id"],
            ["memories.tenant_id", "memories.id"],
            ondelete="RESTRICT",
            name="fk_evaluations_tenant_memory",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "skill_version_id"],
            ["skill_versions.tenant_id", "skill_versions.id"],
            ondelete="RESTRICT",
            name="fk_evaluations_tenant_skill_version",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_evaluations_tenant_id_id"),
        Index(
            "ix_evaluations_tenant_subject",
            "tenant_id",
            "subject_type",
            "memory_id",
            "skill_version_id",
            "created_at",
        ),
        Index(
            "uq_evaluations_memory_profile",
            "tenant_id",
            "memory_id",
            "evaluator_name",
            "evaluator_version",
            "content_hash",
            unique=True,
            postgresql_where=text("subject_type = 'memory'"),
        ),
        Index(
            "uq_evaluations_skill_profile",
            "tenant_id",
            "skill_version_id",
            "evaluator_name",
            "evaluator_version",
            "content_hash",
            unique=True,
            postgresql_where=text("subject_type = 'skill_version'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    memory_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    skill_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    evaluator_name: Mapped[str] = mapped_column(String(120), nullable=False)
    evaluator_version: Mapped[str] = mapped_column(String(80), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=EvaluationStatus.PENDING.value
    )
    score: Mapped[float | None] = mapped_column(Float)
    verdict: Mapped[str | None] = mapped_column(String(20))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class Approval(Base, TimestampMixin):
    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            "subject_type IN ('memory', 'skill_version')",
            name="ck_approvals_subject_type",
        ),
        CheckConstraint(
            "(subject_type = 'memory' AND memory_id IS NOT NULL AND "
            "skill_version_id IS NULL) OR "
            "(subject_type = 'skill_version' AND memory_id IS NULL AND "
            "skill_version_id IS NOT NULL)",
            name="ck_approvals_subject",
        ),
        CheckConstraint("action = 'publish'", name="ck_approvals_action_goal_f"),
        CheckConstraint(
            "status IN ('requested', 'approved', 'rejected', 'cancelled', 'expired')",
            name="ck_approvals_status",
        ),
        CheckConstraint("length(content_hash) = 64", name="ck_approvals_content_hash"),
        CheckConstraint(
            "(status = 'requested' AND reviewer IS NULL AND decided_at IS NULL) OR "
            "(status IN ('approved', 'rejected') AND reviewer IS NOT NULL AND "
            "reason IS NOT NULL AND decided_at IS NOT NULL) OR "
            "(status IN ('cancelled', 'expired') AND reason IS NOT NULL AND "
            "decided_at IS NOT NULL)",
            name="ck_approvals_terminal_shape",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "memory_id"],
            ["memories.tenant_id", "memories.id"],
            ondelete="RESTRICT",
            name="fk_approvals_tenant_memory",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "skill_version_id"],
            ["skill_versions.tenant_id", "skill_versions.id"],
            ondelete="RESTRICT",
            name="fk_approvals_tenant_skill_version",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_approvals_tenant_id_id"),
        Index("ix_approvals_tenant_status", "tenant_id", "status", "created_at"),
        Index(
            "uq_approvals_memory_requested",
            "tenant_id",
            "memory_id",
            "action",
            "content_hash",
            unique=True,
            postgresql_where=text("subject_type = 'memory' AND status = 'requested'"),
        ),
        Index(
            "uq_approvals_skill_requested",
            "tenant_id",
            "skill_version_id",
            "action",
            "content_hash",
            unique=True,
            postgresql_where=text("subject_type = 'skill_version' AND status = 'requested'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    memory_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    skill_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    action: Mapped[str] = mapped_column(String(32), nullable=False, server_default="publish")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=ApprovalStatus.REQUESTED.value
    )
    requester: Mapped[str] = mapped_column(String(200), nullable=False)
    reviewer: Mapped[str | None] = mapped_column(String(200))
    reason: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class SkillDeployment(Base, TimestampMixin):
    __tablename__ = "skill_deployments"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('project', 'agent')",
            name="ck_skill_deployments_scope_goal_f",
        ),
        CheckConstraint(
            "(scope_type = 'project' AND project_id IS NOT NULL AND agent_id IS NULL) OR "
            "(scope_type = 'agent' AND project_id IS NULL AND agent_id IS NOT NULL)",
            name="ck_skill_deployments_scope_owner",
        ),
        CheckConstraint(
            "rollout_percentage >= 1 AND rollout_percentage <= 99",
            name="ck_skill_deployments_rollout",
        ),
        CheckConstraint("status IN ('active', 'retired')", name="ck_skill_deployments_status"),
        CheckConstraint(
            "(status = 'active' AND retired_at IS NULL) OR "
            "(status = 'retired' AND retired_at IS NOT NULL)",
            name="ck_skill_deployments_terminal_shape",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "skill_id"],
            ["skills.tenant_id", "skills.id"],
            ondelete="RESTRICT",
            name="fk_skill_deployments_tenant_skill",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "skill_id", "skill_version_id"],
            ["skill_versions.tenant_id", "skill_versions.skill_id", "skill_versions.id"],
            ondelete="RESTRICT",
            name="fk_skill_deployments_tenant_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_skill_deployments_tenant_project",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="RESTRICT",
            name="fk_skill_deployments_tenant_agent",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_skill_deployments_tenant_id_id"),
        Index(
            "uq_skill_deployments_project_active",
            "tenant_id",
            "skill_id",
            "project_id",
            unique=True,
            postgresql_where=text("status = 'active' AND scope_type = 'project'"),
        ),
        Index(
            "uq_skill_deployments_agent_active",
            "tenant_id",
            "skill_id",
            "agent_id",
            unique=True,
            postgresql_where=text("status = 'active' AND scope_type = 'agent'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    skill_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    skill_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    agent_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    rollout_percentage: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=SkillDeploymentStatus.ACTIVE.value
    )
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    retired_by: Mapped[str | None] = mapped_column(String(200))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_events_tenant_run",
        ),
        UniqueConstraint("tenant_id", "sequence", name="uq_events_tenant_sequence"),
        Index("ix_events_tenant_aggregate", "tenant_id", "aggregate_type", "aggregate_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)
    event_type: Mapped[str] = mapped_column(String(150), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    actor_id: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    causation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AuditRecord(Base):
    __tablename__ = "audit_records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "sequence", name="uq_audit_tenant_sequence"),
        Index("ix_audit_tenant_resource", "tenant_id", "resource_type", "resource_id"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)
    action: Mapped[str] = mapped_column(String(150), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(200), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
