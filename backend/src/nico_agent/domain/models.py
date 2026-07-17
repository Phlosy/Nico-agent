"""Goal C relational models for the generic control-plane core."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
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
    ProjectStatus,
    RunStatus,
    RunStepStatus,
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
