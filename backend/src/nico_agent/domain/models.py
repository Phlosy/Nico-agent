"""Relational models for the generic control-plane core."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
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
    ConversationStatus,
    ConversationTurnStatus,
    EvaluationStatus,
    MemoryStatus,
    ModelCallStatus,
    ModelEndpointStatus,
    PlanStatus,
    PlanStepStatus,
    ProjectMemberStatus,
    ProjectSessionStatus,
    ProjectStatus,
    ProjectSupervisionStatus,
    ProviderProbeKind,
    ProviderProbeStatus,
    RunInterventionStatus,
    RunStatus,
    RunStepStatus,
    RuntimeEvaluationStatus,
    SkillDeploymentStatus,
    SkillStatus,
    SkillVersionStatus,
    TaskStatus,
    ToolApprovalStatus,
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
        CheckConstraint("kind IN ('shared', 'personal')", name="ck_projects_kind"),
        CheckConstraint(
            "(kind = 'personal' AND owner_actor_id IS NOT NULL "
            "AND supervision_cadence_seconds IS NULL) OR "
            "(kind = 'shared' AND owner_actor_id IS NULL)",
            name="ck_projects_kind_owner",
        ),
        CheckConstraint(
            "supervision_cadence_seconds IS NULL OR "
            "supervision_cadence_seconds BETWEEN 300 AND 604800",
            name="ck_projects_supervision_cadence",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_projects_tenant_id_id"),
        UniqueConstraint("tenant_id", "name", name="uq_projects_tenant_name"),
        Index(
            "uq_projects_personal_owner",
            "tenant_id",
            "owner_actor_id",
            unique=True,
            postgresql_where=text("kind = 'personal'"),
        ),
        Index(
            "uq_projects_idempotency",
            "tenant_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, server_default="shared")
    owner_actor_id: Mapped[str | None] = mapped_column(String(200))
    supervision_cadence_seconds: Mapped[int | None] = mapped_column(Integer)
    next_supervision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
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
        CheckConstraint(
            "execution_mode IS NULL OR execution_mode IN ('direct', 'react', 'plan_and_execute')",
            name="ck_agent_versions_execution_mode",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="RESTRICT",
            name="fk_agent_versions_tenant_agent",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "model_endpoint_id"],
            ["model_endpoints.tenant_id", "model_endpoints.id"],
            ondelete="RESTRICT",
            name="fk_agent_versions_model_endpoint",
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
    runtime_provider: Mapped[str | None] = mapped_column(String(100))
    execution_mode: Mapped[str | None] = mapped_column(String(32))
    model_endpoint_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    model_name: Mapped[str | None] = mapped_column(String(200))
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
    coordination_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    budgets: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    run_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class ProjectMember(Base, TimestampMixin):
    __tablename__ = "project_members"
    __table_args__ = (
        CheckConstraint("role IN ('lead', 'member')", name="ck_project_members_role"),
        CheckConstraint(
            "status IN ('active', 'paused', 'removed')",
            name="ck_project_members_status",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_project_members_project",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="RESTRICT",
            name="fk_project_members_agent",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_project_members_tenant_id_id"),
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_project_members_project_id"),
        UniqueConstraint(
            "tenant_id", "project_id", "agent_id", name="uq_project_members_project_agent"
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "agent_id",
            "id",
            name="uq_project_members_scope_id",
        ),
        Index(
            "uq_project_members_active_lead",
            "tenant_id",
            "project_id",
            unique=True,
            postgresql_where=text("role = 'lead' AND status = 'active'"),
        ),
        Index(
            "ix_project_members_active",
            "tenant_id",
            "project_id",
            "status",
            "created_at",
        ),
        Index(
            "uq_project_members_idempotency",
            "tenant_id",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    agent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, server_default="member")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=ProjectMemberStatus.ACTIVE.value
    )
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    removal_reason: Mapped[str | None] = mapped_column(Text)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class Conversation(Base, TimestampMixin):
    """A durable user-facing dialogue spanning independent Tasks and Runs."""

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_conversations_status",
        ),
        CheckConstraint(
            "summary_through_sequence >= 0",
            name="ck_conversations_summary_sequence",
        ),
        CheckConstraint(
            "summary_input_hash IS NULL OR length(summary_input_hash) = 64",
            name="ck_conversations_summary_hash",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_conversations_project",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="RESTRICT",
            name="fk_conversations_agent",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "agent_id", "agent_version_id"],
            ["agent_versions.tenant_id", "agent_versions.agent_id", "agent_versions.id"],
            ondelete="RESTRICT",
            name="fk_conversations_agent_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "id", "last_turn_id"],
            [
                "conversation_turns.tenant_id",
                "conversation_turns.conversation_id",
                "conversation_turns.id",
            ],
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_conversations_last_turn",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "summary_model_call_id"],
            ["model_calls.tenant_id", "model_calls.id"],
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_conversations_summary_model_call",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "project_session_id"],
            [
                "project_sessions.tenant_id",
                "project_sessions.project_id",
                "project_sessions.id",
            ],
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_conversations_project_session",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_conversations_tenant_id_id"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_conversations_idempotency"),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "agent_id",
            "id",
            name="uq_conversations_project_agent_id",
        ),
        Index(
            "ix_conversations_recent",
            "tenant_id",
            "status",
            "updated_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    agent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    agent_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_session_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=ConversationStatus.ACTIVE.value
    )
    summary: Mapped[str | None] = mapped_column(Text)
    summary_through_sequence: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    summary_input_hash: Mapped[str | None] = mapped_column(String(64))
    summary_model_call_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    last_turn_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    @property
    def mode(self) -> str:
        """API projection populated by ConversationService without storing duplicate state."""

        return getattr(self, "_conversation_mode", "project")


class ProjectSession(Base, TimestampMixin):
    __tablename__ = "project_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'paused', 'archived')",
            name="ck_project_sessions_status",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_project_sessions_project",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "agent_id", "project_member_id"],
            [
                "project_members.tenant_id",
                "project_members.project_id",
                "project_members.agent_id",
                "project_members.id",
            ],
            ondelete="RESTRICT",
            name="fk_project_sessions_member",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "agent_id", "current_conversation_id"],
            [
                "conversations.tenant_id",
                "conversations.project_id",
                "conversations.agent_id",
                "conversations.id",
            ],
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_project_sessions_current_conversation",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_project_sessions_tenant_id_id"),
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_project_sessions_project_id"),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "project_member_id",
            "id",
            name="uq_project_sessions_member_id",
        ),
        UniqueConstraint(
            "tenant_id", "project_id", "agent_id", name="uq_project_sessions_project_agent"
        ),
        UniqueConstraint("tenant_id", "project_member_id", name="uq_project_sessions_member"),
        Index(
            "ix_project_sessions_status",
            "tenant_id",
            "project_id",
            "status",
            "updated_at",
        ),
        Index(
            "uq_project_sessions_idempotency",
            "tenant_id",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_member_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    agent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    current_conversation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=ProjectSessionStatus.ACTIVE.value
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class ConversationTurn(Base, TimestampMixin):
    """One immutable user message and its current authoritative Run projection."""

    __tablename__ = "conversation_turns"
    __table_args__ = (
        CheckConstraint("sequence > 0", name="ck_conversation_turns_sequence"),
        CheckConstraint(
            "status IN ('accepted', 'queued', 'running', 'waiting_for_approval', "
            "'completed', 'failed', 'cancelled')",
            name="ck_conversation_turns_status",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            ondelete="RESTRICT",
            name="fk_conversation_turns_conversation",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.id"],
            ondelete="RESTRICT",
            name="fk_conversation_turns_task",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "task_id", "run_id"],
            ["runs.tenant_id", "runs.task_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_conversation_turns_current_run",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_conversation_turns_tenant_id_id"),
        UniqueConstraint("tenant_id", "task_id", name="uq_conversation_turns_task"),
        UniqueConstraint("tenant_id", "run_id", name="uq_conversation_turns_current_run"),
        UniqueConstraint(
            "tenant_id",
            "conversation_id",
            "id",
            name="uq_conversation_turns_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "conversation_id",
            "sequence",
            name="uq_conversation_turns_sequence",
        ),
        UniqueConstraint(
            "tenant_id",
            "conversation_id",
            "idempotency_key",
            name="uq_conversation_turns_idempotency",
        ),
        Index(
            "ix_conversation_turns_history",
            "tenant_id",
            "conversation_id",
            "sequence",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    conversation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    user_input: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=ConversationTurnStatus.QUEUED.value
    )
    assistant_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    artifact_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class ConversationAttachment(Base, TimestampMixin):
    """Short-lived bytes staged for the next Turn of one Conversation."""

    __tablename__ = "conversation_attachments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('staged', 'consumed', 'deleted', 'expired')",
            name="ck_conversation_attachments_status",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_conversation_attachments_size"),
        CheckConstraint("length(sha256) = 64", name="ck_conversation_attachments_sha256"),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            ondelete="RESTRICT",
            name="fk_conversation_attachments_conversation",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "consumed_by_turn_id"],
            ["conversation_turns.tenant_id", "conversation_turns.id"],
            ondelete="RESTRICT",
            name="fk_conversation_attachments_turn",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "artifact_id"],
            ["artifacts.tenant_id", "artifacts.id"],
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_conversation_attachments_artifact",
        ),
        CheckConstraint(
            "status <> 'consumed' OR (consumed_by_turn_id IS NOT NULL AND artifact_id IS NOT NULL)",
            name="ck_conversation_attachments_consumed",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_conversation_attachments_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "conversation_id",
            "idempotency_key",
            name="uq_conversation_attachments_idempotency",
        ),
        Index(
            "ix_conversation_attachments_staged",
            "tenant_id",
            "conversation_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    conversation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    content_type: Mapped[str] = mapped_column(String(200), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="staged")
    uploaded_by: Mapped[str] = mapped_column(String(200), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_by_turn_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    artifact_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    text_excerpt: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


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
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "project_session_id"],
            [
                "project_sessions.tenant_id",
                "project_sessions.project_id",
                "project_sessions.id",
            ],
            ondelete="RESTRICT",
            name="fk_tasks_project_session",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_tasks_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "project_session_id",
            "id",
            name="uq_tasks_project_session_id",
        ),
        Index("ix_tasks_tenant_status_created", "tenant_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    assignee_agent_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    parent_task_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    project_session_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
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
            "'waiting_for_approval', 'waiting_for_subagent', 'paused', "
            "'completed', 'failed', 'cancelled', "
            "'timed_out')",
            name="ck_runs_status",
        ),
        CheckConstraint("checkpoint_schema_version > 0", name="ck_runs_checkpoint_schema"),
        CheckConstraint("checkpoint_revision >= 0", name="ck_runs_checkpoint_revision"),
        CheckConstraint(
            "checkpoint_hash IS NULL OR length(checkpoint_hash) = 64",
            name="ck_runs_checkpoint_hash",
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
        UniqueConstraint("tenant_id", "task_id", "id", name="uq_runs_tenant_task_id"),
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
    checkpoint_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    checkpoint_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    checkpoint_hash: Mapped[str | None] = mapped_column(String(64))
    cost: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class ProjectSupervisionCycle(Base, TimestampMixin):
    __tablename__ = "project_supervision_cycles"
    __table_args__ = (
        CheckConstraint("trigger IN ('manual', 'scheduled')", name="ck_supervision_trigger"),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_supervision_status",
        ),
        CheckConstraint(
            "run_id IS NULL OR task_id IS NOT NULL",
            name="ck_supervision_run_requires_task",
        ),
        CheckConstraint("revision > 0", name="ck_supervision_revision"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_supervision_project",
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "project_id",
                "lead_project_member_id",
                "lead_project_session_id",
            ],
            [
                "project_sessions.tenant_id",
                "project_sessions.project_id",
                "project_sessions.project_member_id",
                "project_sessions.id",
            ],
            ondelete="RESTRICT",
            name="fk_supervision_lead_session",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "lead_project_session_id", "task_id"],
            [
                "tasks.tenant_id",
                "tasks.project_id",
                "tasks.project_session_id",
                "tasks.id",
            ],
            ondelete="RESTRICT",
            name="fk_supervision_task",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "task_id", "run_id"],
            ["runs.tenant_id", "runs.task_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_supervision_run",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_supervision_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "trigger",
            "cadence_slot",
            name="uq_supervision_project_slot",
        ),
        UniqueConstraint(
            "tenant_id", "project_id", "idempotency_key", name="uq_supervision_idempotency"
        ),
        Index(
            "ix_supervision_due",
            "status",
            "scheduled_for",
            "lease_expires_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    lead_project_member_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    lead_project_session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    cadence_slot: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=ProjectSupervisionStatus.PENDING.value
    )
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    narrative_summary: Mapped[str | None] = mapped_column(Text)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class RunIntervention(Base, TimestampMixin):
    __tablename__ = "run_interventions"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('local_guidance', 'project_change')",
            name="ck_run_interventions_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'consumed', 'rejected', 'withdrawn')",
            name="ck_run_interventions_status",
        ),
        CheckConstraint(
            "length(content) BETWEEN 1 AND 16000",
            name="ck_run_interventions_content",
        ),
        CheckConstraint("length(content_hash) = 64", name="ck_run_interventions_hash"),
        CheckConstraint("expected_run_revision > 0", name="ck_run_interventions_run_revision"),
        CheckConstraint("revision > 0", name="ck_run_interventions_revision"),
        CheckConstraint(
            "status <> 'consumed' OR consumed_at IS NOT NULL",
            name="ck_run_interventions_consumed",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "project_session_id"],
            [
                "project_sessions.tenant_id",
                "project_sessions.project_id",
                "project_sessions.id",
            ],
            ondelete="RESTRICT",
            name="fk_run_interventions_session",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "project_session_id", "task_id"],
            [
                "tasks.tenant_id",
                "tasks.project_id",
                "tasks.project_session_id",
                "tasks.id",
            ],
            ondelete="RESTRICT",
            name="fk_run_interventions_task",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "task_id", "run_id"],
            ["runs.tenant_id", "runs.task_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_run_interventions_run",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_run_interventions_tenant_id_id"),
        UniqueConstraint(
            "tenant_id", "run_id", "idempotency_key", name="uq_run_interventions_idempotency"
        ),
        Index(
            "ix_run_interventions_pending",
            "tenant_id",
            "run_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_run_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=RunInterventionStatus.PENDING.value
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    consumed_by: Mapped[str | None] = mapped_column(String(200))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class RuntimeSession(Base, TimestampMixin):
    __tablename__ = "runtime_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created', 'running', 'paused', 'suspended', "
            "'completed', 'failed', 'cancelled')",
            name="ck_runtime_sessions_status",
        ),
        CheckConstraint(
            "execution_mode IN ('direct', 'react', 'plan_and_execute')",
            name="ck_runtime_sessions_execution_mode",
        ),
        CheckConstraint(
            "checkpoint_schema_version > 0",
            name="ck_runtime_sessions_checkpoint_schema",
        ),
        CheckConstraint(
            "checkpoint_revision >= 0",
            name="ck_runtime_sessions_checkpoint_revision",
        ),
        CheckConstraint(
            "checkpoint_hash IS NULL OR length(checkpoint_hash) = 64",
            name="ck_runtime_sessions_checkpoint_hash",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_runtime_sessions_tenant_run",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "current_context_snapshot_id"],
            ["context_snapshots.tenant_id", "context_snapshots.run_id", "context_snapshots.id"],
            ondelete="RESTRICT",
            name="fk_runtime_sessions_current_context",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "last_model_call_id"],
            ["model_calls.tenant_id", "model_calls.run_id", "model_calls.id"],
            ondelete="RESTRICT",
            name="fk_runtime_sessions_last_model_call",
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
    provider_resolution_source: Mapped[str | None] = mapped_column(String(64))
    legacy_resolver_used: Mapped[bool | None] = mapped_column(Boolean)
    provider_compatibility: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    external_session_id: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="created")
    capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    provider_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    execution_mode: Mapped[str] = mapped_column(String(32), nullable=False, server_default="direct")
    loop_state: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="initializing"
    )
    execution_manifest: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    model_endpoint_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    current_context_snapshot_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    last_model_call_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    checkpoint_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    checkpoint_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    checkpoint_hash: Mapped[str | None] = mapped_column(String(64))
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    trajectory: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    tool_policy_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    coordination_policy_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    knowledge_policy_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    knowledge_selection_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    last_event_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class Delegation(Base, TimestampMixin):
    """Immutable definition and monotonic lifecycle of one dynamic Child Run."""

    __tablename__ = "delegations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'accepted', 'running', 'completed', 'failed', "
            "'cancelled', 'rejected')",
            name="ck_delegations_status",
        ),
        CheckConstraint(
            "execution_mode IN ('serial', 'parallel')", name="ck_delegations_execution_mode"
        ),
        CheckConstraint("length(task_fingerprint) = 64", name="ck_delegations_fingerprint"),
        CheckConstraint("revision > 0", name="ck_delegations_revision"),
        ForeignKeyConstraint(
            ["tenant_id", "parent_run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_delegations_parent_run",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "parent_run_id", "parent_run_step_id"],
            ["run_steps.tenant_id", "run_steps.run_id", "run_steps.id"],
            ondelete="RESTRICT",
            name="fk_delegations_parent_step",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "child_task_id"],
            ["tasks.tenant_id", "tasks.id"],
            ondelete="RESTRICT",
            name="fk_delegations_child_task",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "child_run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_delegations_child_run",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "target_agent_id", "target_agent_version_id"],
            ["agent_versions.tenant_id", "agent_versions.agent_id", "agent_versions.id"],
            ondelete="RESTRICT",
            name="fk_delegations_target_version",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_delegations_tenant_id_id"),
        UniqueConstraint("tenant_id", "parent_run_id", "id", name="uq_delegations_parent_id"),
        UniqueConstraint("tenant_id", "child_run_id", name="uq_delegations_child_run"),
        UniqueConstraint(
            "tenant_id",
            "parent_run_id",
            "idempotency_key",
            name="uq_delegations_parent_idempotency",
        ),
        UniqueConstraint(
            "tenant_id",
            "parent_run_id",
            "task_fingerprint",
            name="uq_delegations_parent_fingerprint",
        ),
        Index("ix_delegations_parent_status", "tenant_id", "parent_run_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    parent_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    parent_run_step_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    child_task_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    child_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    target_agent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    target_agent_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    acceptance: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    context_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    budget_grant: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    permission_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    task_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="proposed")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class AgentRunRelation(Base, TimestampMixin):
    """Tenant-scoped transitive closure for one dynamic Run tree."""

    __tablename__ = "agent_run_relations"
    __table_args__ = (
        CheckConstraint("ancestor_run_id <> descendant_run_id", name="ck_run_relations_no_self"),
        CheckConstraint("depth > 0", name="ck_run_relations_depth"),
        CheckConstraint("NOT is_direct OR depth = 1", name="ck_run_relations_direct_depth"),
        ForeignKeyConstraint(
            ["tenant_id", "ancestor_run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_run_relations_ancestor",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "descendant_run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_run_relations_descendant",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "delegation_id"],
            ["delegations.tenant_id", "delegations.id"],
            ondelete="RESTRICT",
            name="fk_run_relations_delegation",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_run_relations_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "ancestor_run_id",
            "descendant_run_id",
            name="uq_run_relations_closure",
        ),
        Index("ix_run_relations_descendant_depth", "tenant_id", "descendant_run_id", "depth"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    ancestor_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    descendant_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    delegation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    is_direct: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class AgentMessage(Base, TimestampMixin):
    __tablename__ = "agent_messages"
    __table_args__ = (
        CheckConstraint(
            "message_type IN ('task_assignment', 'progress', 'question', 'answer', 'result', "
            "'critique', 'retry_request', 'cancel', 'system_notice')",
            name="ck_agent_messages_type",
        ),
        CheckConstraint(
            "status IN ('queued', 'delivered', 'acknowledged')",
            name="ck_agent_messages_status",
        ),
        CheckConstraint(
            "visibility IN ('sender_receiver', 'delegation_tree', 'parent_only')",
            name="ck_agent_messages_visibility",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "sender_run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_agent_messages_sender",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "receiver_run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_agent_messages_receiver",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "delegation_id"],
            ["delegations.tenant_id", "delegations.id"],
            ondelete="RESTRICT",
            name="fk_agent_messages_delegation",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_agent_messages_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "sender_run_id",
            "idempotency_key",
            name="uq_agent_messages_sender_idempotency",
        ),
        Index(
            "ix_agent_messages_receiver_status",
            "tenant_id",
            "receiver_run_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    delegation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sender_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    receiver_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    message_type: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    visibility: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="sender_receiver"
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="queued")
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    correlation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunBudgetLedger(Base, TimestampMixin):
    __tablename__ = "run_budget_ledgers"
    __table_args__ = (
        CheckConstraint(
            "token_limit >= 0 AND token_direct_consumed >= 0 AND token_child_consumed >= 0 "
            "AND token_child_reserved >= 0",
            name="ck_budget_ledgers_token_nonnegative",
        ),
        CheckConstraint(
            "token_direct_consumed + token_child_consumed + token_child_reserved <= token_limit",
            name="ck_budget_ledgers_token_bound",
        ),
        CheckConstraint(
            "cost_limit_microunits >= 0 AND cost_direct_consumed_microunits >= 0 "
            "AND cost_child_consumed_microunits >= 0 AND cost_child_reserved_microunits >= 0",
            name="ck_budget_ledgers_cost_nonnegative",
        ),
        CheckConstraint(
            "cost_direct_consumed_microunits + cost_child_consumed_microunits + "
            "cost_child_reserved_microunits <= cost_limit_microunits",
            name="ck_budget_ledgers_cost_bound",
        ),
        CheckConstraint(
            "tool_call_limit >= 0 AND tool_calls_direct_consumed >= 0 "
            "AND tool_calls_child_consumed >= 0 AND tool_calls_child_reserved >= 0",
            name="ck_budget_ledgers_tool_nonnegative",
        ),
        CheckConstraint(
            "tool_calls_direct_consumed + tool_calls_child_consumed + "
            "tool_calls_child_reserved <= tool_call_limit",
            name="ck_budget_ledgers_tool_bound",
        ),
        CheckConstraint("child_count >= 0", name="ck_budget_ledgers_child_count"),
        CheckConstraint("revision > 0", name="ck_budget_ledgers_revision"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_budget_ledgers_run",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_budget_ledgers_tenant_id_id"),
        UniqueConstraint("tenant_id", "run_id", name="uq_budget_ledgers_tenant_run"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    token_limit: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    token_direct_consumed: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    token_child_consumed: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    token_child_reserved: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    cost_limit_microunits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    cost_direct_consumed_microunits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    cost_child_consumed_microunits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    cost_child_reserved_microunits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    tool_call_limit: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tool_calls_direct_consumed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    tool_calls_child_consumed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    tool_calls_child_reserved: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    child_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    wall_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class Artifact(Base, TimestampMixin):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('uploading', 'available', 'failed', 'deleted')",
            name="ck_artifacts_status",
        ),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_artifacts_size"),
        CheckConstraint("sha256 IS NULL OR length(sha256) = 64", name="ck_artifacts_sha256"),
        CheckConstraint(
            "status <> 'available' OR "
            "(sha256 IS NOT NULL AND size_bytes IS NOT NULL AND object_key IS NOT NULL)",
            name="ck_artifacts_available_content",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_artifacts_project",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "owner_run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_artifacts_owner_run",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_artifacts_tenant_id_id"),
        UniqueConstraint("tenant_id", "id", "owner_run_id", name="uq_artifacts_owner_id"),
        UniqueConstraint(
            "tenant_id", "owner_run_id", "idempotency_key", name="uq_artifacts_run_idempotency"
        ),
        Index("ix_artifacts_owner_created", "tenant_id", "owner_run_id", "created_at"),
        Index("ix_artifacts_hash", "tenant_id", "sha256"),
        Index(
            "ix_artifacts_project_available",
            "tenant_id",
            "project_id",
            text("created_at DESC"),
            text("id DESC"),
            postgresql_where=text("status = 'available'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    project_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    content_type: Mapped[str] = mapped_column(String(200), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False, server_default="file")
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="uploading")
    temp_object_key: Mapped[str | None] = mapped_column(String(1000))
    object_key: Mapped[str | None] = mapped_column(String(1000))
    sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class SharedArtifactLink(Base, TimestampMixin):
    __tablename__ = "shared_artifact_links"
    __table_args__ = (
        CheckConstraint(
            "visibility IN ('parent', 'child')", name="ck_shared_artifact_links_visibility"
        ),
        CheckConstraint(
            "status IN ('active', 'revoked', 'expired')", name="ck_shared_artifact_links_status"
        ),
        CheckConstraint("owner_run_id <> grantee_run_id", name="ck_shared_artifact_links_runs"),
        ForeignKeyConstraint(
            ["tenant_id", "artifact_id", "owner_run_id"],
            ["artifacts.tenant_id", "artifacts.id", "artifacts.owner_run_id"],
            ondelete="RESTRICT",
            name="fk_shared_artifact_links_artifact_owner",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "grantee_run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_shared_artifact_links_grantee",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_shared_artifact_links_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "artifact_id",
            "grantee_run_id",
            "purpose",
            name="uq_shared_artifact_links_grant",
        ),
        Index(
            "ix_shared_artifact_links_grantee",
            "tenant_id",
            "grantee_run_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    artifact_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    owner_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    grantee_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class ModelEndpoint(Base, TimestampMixin):
    __tablename__ = "model_endpoints"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="ck_model_endpoints_status"),
        CheckConstraint(
            "protocol IN ('openai_compatible', 'anthropic_messages', 'google_gemini')",
            name="ck_model_endpoints_protocol",
        ),
        CheckConstraint("revision > 0", name="ck_model_endpoints_revision"),
        ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="RESTRICT", name="fk_model_endpoints_tenant"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "verified_probe_id"],
            ["provider_probes.tenant_id", "provider_probes.id"],
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_model_endpoints_verified_probe",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_model_endpoints_tenant_id_id"),
        UniqueConstraint(
            "tenant_id", "stable_key", "revision", name="uq_model_endpoints_key_revision"
        ),
        Index("ix_model_endpoints_tenant_status", "tenant_id", "status", "stable_key"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    stable_key: Mapped[str] = mapped_column(String(120), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    protocol: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default="openai_compatible"
    )
    base_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    credential_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    provider_key: Mapped[str] = mapped_column(String(120), nullable=False, server_default="custom")
    catalog_revision: Mapped[str | None] = mapped_column(String(64))
    provider_options: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    verified_probe_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=ModelEndpointStatus.ACTIVE.value
    )
    allowed_models: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    rate_limit: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    tls_policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class ProviderProbe(Base, TimestampMixin):
    __tablename__ = "provider_probes"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('discover_models', 'verify_completion')",
            name="ck_provider_probes_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled', 'activated')",
            name="ck_provider_probes_status",
        ),
        CheckConstraint(
            "protocol IN ('openai_compatible', 'anthropic_messages', 'google_gemini')",
            name="ck_provider_probes_protocol",
        ),
        CheckConstraint("length(candidate_hash) = 64", name="ck_provider_probes_hash"),
        CheckConstraint("attempt >= 0", name="ck_provider_probes_attempt"),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_provider_probes_tenant",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_provider_probes_tenant_id_id"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_provider_probes_idempotency"),
        Index(
            "ix_provider_probes_claimable",
            "status",
            "lease_expires_at",
            "created_at",
        ),
        Index(
            "ix_provider_probes_tenant_provider",
            "tenant_id",
            "provider_key",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=ProviderProbeKind.VERIFY_COMPLETION.value
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=ProviderProbeStatus.PENDING.value
    )
    provider_key: Mapped[str] = mapped_column(String(120), nullable=False)
    protocol: Mapped[str] = mapped_column(String(50), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    credential_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    provider_options: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    model_name: Mapped[str | None] = mapped_column(String(200))
    catalog_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_detail: Mapped[str | None] = mapped_column(String(500))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    worker_id: Mapped[str | None] = mapped_column(String(200))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activation_correlation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class ContextSnapshot(Base):
    __tablename__ = "context_snapshots"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="ck_context_snapshots_schema_version"),
        CheckConstraint("version > 0", name="ck_context_snapshots_version"),
        CheckConstraint("length(content_hash) = 64", name="ck_context_snapshots_hash"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_context_snapshots_tenant_run",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "runtime_session_id"],
            ["runtime_sessions.tenant_id", "runtime_sessions.run_id", "runtime_sessions.id"],
            ondelete="RESTRICT",
            name="fk_context_snapshots_runtime_session",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "parent_snapshot_id"],
            ["context_snapshots.tenant_id", "context_snapshots.run_id", "context_snapshots.id"],
            ondelete="RESTRICT",
            name="fk_context_snapshots_parent",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            ondelete="RESTRICT",
            name="fk_context_snapshots_conversation",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "conversation_turn_id"],
            [
                "conversation_turns.tenant_id",
                "conversation_turns.conversation_id",
                "conversation_turns.id",
            ],
            ondelete="RESTRICT",
            name="fk_context_snapshots_conversation_turn",
        ),
        CheckConstraint(
            "conversation_turn_id IS NULL OR conversation_id IS NOT NULL",
            name="ck_context_snapshots_conversation_ref",
        ),
        CheckConstraint(
            "conversation_summary_hash IS NULL OR length(conversation_summary_hash) = 64",
            name="ck_context_snapshots_conversation_summary_hash",
        ),
        CheckConstraint(
            "token_budget IS NULL OR token_budget > 0",
            name="ck_context_snapshots_token_budget",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_context_snapshots_tenant_id_id"),
        UniqueConstraint("tenant_id", "run_id", "id", name="uq_context_snapshots_tenant_run_id"),
        UniqueConstraint("tenant_id", "run_id", "version", name="uq_context_snapshots_run_version"),
        Index("ix_context_snapshots_run_created", "tenant_id", "run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    runtime_session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    parent_snapshot_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    conversation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    conversation_turn_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(100), nullable=False)
    source_refs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    memory_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    skill_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    effect_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    rendered_messages: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    truncation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    selected_turn_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    conversation_summary_hash: Mapped[str | None] = mapped_column(String(64))
    artifact_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    token_budget: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RuntimeKnowledgeUsage(Base, TimestampMixin):
    """Frozen Memory/Skill selection and its observed effect in one Run."""

    __tablename__ = "runtime_knowledge_usages"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('memory', 'skill_version')",
            name="ck_runtime_knowledge_usages_source_type",
        ),
        CheckConstraint(
            "(source_type = 'memory' AND memory_id IS NOT NULL AND "
            "skill_id IS NULL AND skill_version_id IS NULL) OR "
            "(source_type = 'skill_version' AND memory_id IS NULL AND "
            "skill_id IS NOT NULL AND skill_version_id IS NOT NULL)",
            name="ck_runtime_knowledge_usages_source",
        ),
        CheckConstraint(
            "status IN ('selected', 'consumed', 'succeeded', 'failed', 'cancelled', 'timed_out')",
            name="ck_runtime_knowledge_usages_status",
        ),
        CheckConstraint("source_version > 0", name="ck_runtime_knowledge_usages_version"),
        CheckConstraint("length(content_hash) = 64", name="ck_runtime_knowledge_usages_hash"),
        CheckConstraint(
            "result_hash IS NULL OR length(result_hash) = 64",
            name="ck_runtime_knowledge_usages_result_hash",
        ),
        CheckConstraint(
            "context_count >= 0 AND model_call_count >= 0",
            name="ck_runtime_knowledge_usages_counts",
        ),
        CheckConstraint("revision > 0", name="ck_runtime_knowledge_usages_revision"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_runtime_knowledge_usages_run",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "runtime_session_id"],
            ["runtime_sessions.tenant_id", "runtime_sessions.run_id", "runtime_sessions.id"],
            ondelete="RESTRICT",
            name="fk_runtime_knowledge_usages_session",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "memory_id"],
            ["memories.tenant_id", "memories.id"],
            ondelete="RESTRICT",
            name="fk_runtime_knowledge_usages_memory",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "skill_id", "skill_version_id"],
            ["skill_versions.tenant_id", "skill_versions.skill_id", "skill_versions.id"],
            ondelete="RESTRICT",
            name="fk_runtime_knowledge_usages_skill_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "first_context_snapshot_id"],
            ["context_snapshots.tenant_id", "context_snapshots.run_id", "context_snapshots.id"],
            ondelete="RESTRICT",
            name="fk_runtime_knowledge_usages_first_context",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "first_model_call_id"],
            ["model_calls.tenant_id", "model_calls.run_id", "model_calls.id"],
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_runtime_knowledge_usages_first_model_call",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_runtime_knowledge_usages_tenant_id_id"),
        Index(
            "uq_runtime_knowledge_usages_run_memory",
            "tenant_id",
            "run_id",
            "memory_id",
            unique=True,
            postgresql_where=text("source_type = 'memory'"),
        ),
        Index(
            "uq_runtime_knowledge_usages_run_skill",
            "tenant_id",
            "run_id",
            "skill_version_id",
            unique=True,
            postgresql_where=text("source_type = 'skill_version'"),
        ),
        Index(
            "ix_runtime_knowledge_usages_run",
            "tenant_id",
            "run_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    runtime_session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    memory_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    skill_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    skill_version_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    selection: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="selected")
    first_context_snapshot_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    first_model_call_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    context_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    model_call_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    outcome_status: Mapped[str | None] = mapped_column(String(32))
    result_hash: Mapped[str | None] = mapped_column(String(64))
    effect_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    first_consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class ModelCall(Base, TimestampMixin):
    __tablename__ = "model_calls"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'streaming', 'completed', 'failed', 'interrupted', 'cancelled')",
            name="ck_model_calls_status",
        ),
        CheckConstraint(
            "usage_status IN ('missing', 'partial', 'exact')",
            name="ck_model_calls_usage_status",
        ),
        CheckConstraint(
            "cost_status IN ('unknown', 'estimated', 'exact')",
            name="ck_model_calls_cost_status",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_model_calls_tenant_run",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "runtime_session_id"],
            ["runtime_sessions.tenant_id", "runtime_sessions.run_id", "runtime_sessions.id"],
            ondelete="RESTRICT",
            name="fk_model_calls_runtime_session",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "context_snapshot_id"],
            ["context_snapshots.tenant_id", "context_snapshots.run_id", "context_snapshots.id"],
            ondelete="RESTRICT",
            name="fk_model_calls_context_snapshot",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "model_endpoint_id"],
            ["model_endpoints.tenant_id", "model_endpoints.id"],
            ondelete="RESTRICT",
            name="fk_model_calls_endpoint",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "replay_of_model_call_id"],
            ["model_calls.tenant_id", "model_calls.run_id", "model_calls.id"],
            ondelete="RESTRICT",
            name="fk_model_calls_replay_of",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_model_calls_tenant_id_id"),
        UniqueConstraint("tenant_id", "run_id", "id", name="uq_model_calls_tenant_run_id"),
        UniqueConstraint("tenant_id", "run_id", "call_key", name="uq_model_calls_run_key"),
        Index("ix_model_calls_run_created", "tenant_id", "run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    runtime_session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    context_snapshot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    model_endpoint_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    replay_of_model_call_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    call_key: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=ModelCallStatus.PENDING.value
    )
    request_redacted: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    response_redacted: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_hash: Mapped[str | None] = mapped_column(String(64))
    provider_request_id: Mapped[str | None] = mapped_column(String(300))
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    usage_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="missing")
    cost: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    cost_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="unknown")
    pricing_revision: Mapped[str | None] = mapped_column(String(100))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "parent_step_id"],
            ["run_steps.tenant_id", "run_steps.run_id", "run_steps.id"],
            ondelete="RESTRICT",
            name="fk_run_steps_parent",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "context_snapshot_id"],
            ["context_snapshots.tenant_id", "context_snapshots.run_id", "context_snapshots.id"],
            ondelete="RESTRICT",
            name="fk_run_steps_context_snapshot",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "model_call_id"],
            ["model_calls.tenant_id", "model_calls.run_id", "model_calls.id"],
            ondelete="RESTRICT",
            name="fk_run_steps_model_call",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_run_steps_tenant_id_id"),
        UniqueConstraint("tenant_id", "run_id", "id", name="uq_run_steps_tenant_run_id"),
        UniqueConstraint("tenant_id", "run_id", "sequence", name="uq_run_steps_sequence"),
        UniqueConstraint("tenant_id", "run_id", "step_key", name="uq_run_steps_step_key"),
        CheckConstraint(
            "step_type IS NULL OR step_type IN ('reasoning', 'tool', 'observation', "
            "'planning', 'reflection', 'delegation', 'aggregation')",
            name="ck_run_steps_step_type",
        ),
        CheckConstraint("iteration IS NULL OR iteration > 0", name="ck_run_steps_iteration"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    step_key: Mapped[str | None] = mapped_column(String(200))
    step_type: Mapped[str | None] = mapped_column(String(32))
    iteration: Mapped[int | None] = mapped_column(Integer)
    parent_step_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    context_snapshot_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    model_call_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
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


class Plan(Base, TimestampMixin):
    """One immutable-semantic revision of a Run's execution plan."""

    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_plans_revision"),
        CheckConstraint(
            "status IN ('active', 'superseded', 'completed', 'failed')",
            name="ck_plans_status",
        ),
        CheckConstraint("reason IN ('initial', 'replan')", name="ck_plans_reason"),
        CheckConstraint("length(content_hash) = 64", name="ck_plans_content_hash"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_plans_tenant_run",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "runtime_session_id"],
            ["runtime_sessions.tenant_id", "runtime_sessions.run_id", "runtime_sessions.id"],
            ondelete="RESTRICT",
            name="fk_plans_runtime_session",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "supersedes_plan_id"],
            ["plans.tenant_id", "plans.run_id", "plans.id"],
            ondelete="RESTRICT",
            name="fk_plans_supersedes",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "created_by_model_call_id"],
            ["model_calls.tenant_id", "model_calls.run_id", "model_calls.id"],
            ondelete="RESTRICT",
            name="fk_plans_created_by_model_call",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_plans_tenant_id_id"),
        UniqueConstraint("tenant_id", "run_id", "id", name="uq_plans_tenant_run_id"),
        UniqueConstraint("tenant_id", "run_id", "revision", name="uq_plans_run_revision"),
        Index("ix_plans_run_revision", "tenant_id", "run_id", "revision"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    runtime_session_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=PlanStatus.ACTIVE.value
    )
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes_plan_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_by_model_call_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class PlanStep(Base, TimestampMixin):
    """A Plan revision step with immutable definition and guarded execution state."""

    __tablename__ = "plan_steps"
    __table_args__ = (
        CheckConstraint("position > 0", name="ck_plan_steps_position"),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'skipped')",
            name="ck_plan_steps_status",
        ),
        CheckConstraint(
            "output_hash IS NULL OR length(output_hash) = 64",
            name="ck_plan_steps_output_hash",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "plan_id"],
            ["plans.tenant_id", "plans.run_id", "plans.id"],
            ondelete="RESTRICT",
            name="fk_plan_steps_plan",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "run_step_id"],
            ["run_steps.tenant_id", "run_steps.run_id", "run_steps.id"],
            ondelete="RESTRICT",
            name="fk_plan_steps_run_step",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_plan_steps_tenant_id_id"),
        UniqueConstraint("tenant_id", "run_id", "id", name="uq_plan_steps_tenant_run_id"),
        UniqueConstraint(
            "tenant_id", "run_id", "plan_id", "step_key", name="uq_plan_steps_plan_key"
        ),
        UniqueConstraint(
            "tenant_id", "run_id", "plan_id", "position", name="uq_plan_steps_plan_position"
        ),
        Index("ix_plan_steps_plan_position", "tenant_id", "run_id", "plan_id", "position"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    plan_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    step_key: Mapped[str] = mapped_column(String(64), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    acceptance: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    dependencies: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=PlanStepStatus.PENDING.value
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    run_step_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    output_hash: Mapped[str | None] = mapped_column(String(64))
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RuntimeEvaluation(Base):
    """Append-only Plan, Reflection, validation, and completion evaluation fact."""

    __tablename__ = "runtime_evaluations"
    __table_args__ = (
        CheckConstraint(
            "evaluation_type IN ('step_validation', 'reflection', 'completion')",
            name="ck_runtime_evaluations_type",
        ),
        CheckConstraint(
            "method IN ('deterministic', 'model')", name="ck_runtime_evaluations_method"
        ),
        CheckConstraint("status IN ('completed', 'failed')", name="ck_runtime_evaluations_status"),
        CheckConstraint(
            "verdict IN ('passed', 'failed', 'retry', 'replan', 'complete', 'continue')",
            name="ck_runtime_evaluations_verdict",
        ),
        CheckConstraint("sequence > 0", name="ck_runtime_evaluations_sequence"),
        CheckConstraint("length(input_hash) = 64", name="ck_runtime_evaluations_input_hash"),
        CheckConstraint(
            "output_hash IS NULL OR length(output_hash) = 64",
            name="ck_runtime_evaluations_output_hash",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_runtime_evaluations_run",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "plan_id"],
            ["plans.tenant_id", "plans.run_id", "plans.id"],
            ondelete="RESTRICT",
            name="fk_runtime_evaluations_plan",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "plan_step_id"],
            ["plan_steps.tenant_id", "plan_steps.run_id", "plan_steps.id"],
            ondelete="RESTRICT",
            name="fk_runtime_evaluations_plan_step",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "model_call_id"],
            ["model_calls.tenant_id", "model_calls.run_id", "model_calls.id"],
            ondelete="RESTRICT",
            name="fk_runtime_evaluations_model_call",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_runtime_evaluations_tenant_id_id"),
        UniqueConstraint("tenant_id", "run_id", "id", name="uq_runtime_evaluations_tenant_run_id"),
        UniqueConstraint(
            "tenant_id", "run_id", "sequence", name="uq_runtime_evaluations_run_sequence"
        ),
        Index("ix_runtime_evaluations_run_sequence", "tenant_id", "run_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    plan_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    plan_step_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    model_call_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=RuntimeEvaluationStatus.COMPLETED.value
    )
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_hash: Mapped[str | None] = mapped_column(String(64))
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


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


class ToolApprovalRequest(Base, TimestampMixin):
    """Durable human decision boundary for one proposed ToolCall."""

    __tablename__ = "tool_approval_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('requested', 'approved', 'rejected', 'expired', 'cancelled')",
            name="ck_tool_approval_requests_status",
        ),
        CheckConstraint(
            "risk_level IN ('medium', 'high')",
            name="ck_tool_approval_requests_risk",
        ),
        CheckConstraint(
            "allowed_scope IS NULL OR allowed_scope IN ('none', 'once', 'run')",
            name="ck_tool_approval_requests_scope",
        ),
        CheckConstraint(
            "length(arguments_hash) = 64",
            name="ck_tool_approval_requests_arguments_hash",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_tool_approval_requests_expiry",
        ),
        CheckConstraint(
            "(status = 'requested' AND allowed_scope IS NULL AND decided_by IS NULL "
            "AND decided_at IS NULL AND decision_idempotency_key IS NULL) OR "
            "(status = 'approved' AND allowed_scope IN ('once', 'run') "
            "AND decided_by IS NOT NULL AND decided_at IS NOT NULL "
            "AND decision_idempotency_key IS NOT NULL) OR "
            "(status IN ('rejected', 'expired', 'cancelled') AND allowed_scope = 'none' "
            "AND decided_by IS NOT NULL AND decided_at IS NOT NULL "
            "AND decision_idempotency_key IS NOT NULL)",
            name="ck_tool_approval_requests_decision_shape",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_tool_approval_requests_tenant_run",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "run_step_id"],
            ["run_steps.tenant_id", "run_steps.run_id", "run_steps.id"],
            ondelete="RESTRICT",
            name="fk_tool_approval_requests_tenant_run_step",
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
            name="fk_tool_approval_requests_tenant_tool_call",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "tool_definition_id"],
            ["tool_definitions.tenant_id", "tool_definitions.id"],
            ondelete="RESTRICT",
            name="fk_tool_approval_requests_tenant_definition",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_tool_approval_requests_tenant_id_id"),
        UniqueConstraint("tenant_id", "tool_call_id", name="uq_tool_approval_requests_tool_call"),
        Index(
            "ix_tool_approval_requests_tenant_status",
            "tenant_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_tool_approval_requests_run_scope",
            "tenant_id",
            "run_id",
            "tool_definition_id",
            "status",
            "allowed_scope",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    run_step_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tool_call_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tool_definition_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=ToolApprovalStatus.REQUESTED.value
    )
    allowed_scope: Mapped[str | None] = mapped_column(String(20))
    requester: Mapped[str] = mapped_column(String(200), nullable=False)
    arguments_redacted: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(200))
    decision: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    decision_idempotency_key: Mapped[str | None] = mapped_column(String(200))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
        Index("ix_events_tenant_run_sequence", "tenant_id", "run_id", "sequence"),
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
