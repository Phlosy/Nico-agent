"""Add tenant-scoped controlled Memory and Skill growth records.

Revision ID: 20260717_0006
Revises: 20260717_0005
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import SchemaItem

revision: str = "20260717_0006"
down_revision: str | None = "20260717_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")
UUID_DEFAULT = sa.text("gen_random_uuid()")
JSON_OBJECT = sa.text("'{}'::jsonb")
JSON_ARRAY = sa.text("'[]'::jsonb")


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    ]


def _enable_rls(table: str) -> None:
    predicate = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "tenant_isolation" ON "{table}" '
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def upgrade() -> None:
    _add_source_composite_keys()
    _create_memories()
    _create_skills()
    _create_growth_sources()
    _create_evaluations_and_approvals()
    _create_skill_deployments()

    tables = (
        "memories",
        "skills",
        "skill_versions",
        "growth_sources",
        "evaluations",
        "approvals",
        "skill_deployments",
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON " + ", ".join(tables) + " TO nico_runtime")
    for table in tables:
        _enable_rls(table)

    _create_guard_functions_and_triggers()


def _add_source_composite_keys() -> None:
    op.create_unique_constraint(
        "uq_agent_versions_tenant_id_id", "agent_versions", ["tenant_id", "id"]
    )
    op.create_unique_constraint(
        "uq_runtime_sessions_tenant_run_id",
        "runtime_sessions",
        ["tenant_id", "run_id", "id"],
    )
    op.create_unique_constraint(
        "uq_tool_calls_tenant_run_step_id",
        "tool_calls",
        ["tenant_id", "run_id", "run_step_id", "id"],
    )


def _create_memories() -> None:
    op.create_table(
        "memories",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("memory_key", sa.Uuid(), nullable=False, server_default=UUID_DEFAULT),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("memory_type", sa.String(32), nullable=False),
        sa.Column("scope_type", sa.String(32), nullable=False),
        sa.Column("project_id", sa.Uuid()),
        sa.Column("agent_id", sa.Uuid()),
        sa.Column("status", sa.String(32), nullable=False, server_default="candidate"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("supersedes_id", sa.Uuid()),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("invalidated_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "memory_type IN ('working', 'episodic', 'semantic', 'procedural')",
            name="ck_memories_type",
        ),
        sa.CheckConstraint(
            "status IN ('candidate', 'active', 'invalidated', 'expired', 'deleted')",
            name="ck_memories_status",
        ),
        sa.CheckConstraint(
            "scope_type IN ('tenant', 'project', 'agent')",
            name="ck_memories_scope_goal_f",
        ),
        sa.CheckConstraint(
            "(scope_type = 'tenant' AND project_id IS NULL AND agent_id IS NULL) OR "
            "(scope_type = 'project' AND project_id IS NOT NULL AND agent_id IS NULL) OR "
            "(scope_type = 'agent' AND project_id IS NULL AND agent_id IS NOT NULL)",
            name="ck_memories_scope_owner",
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_memories_confidence"),
        sa.CheckConstraint("version > 0", name="ck_memories_version_positive"),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_memories_content_hash"),
        sa.CheckConstraint(
            "memory_type <> 'working' OR expires_at IS NOT NULL",
            name="ck_memories_working_expiry",
        ),
        sa.CheckConstraint(
            "status <> 'active' OR approved_at IS NOT NULL",
            name="ck_memories_active_approved",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_memories_tenant_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="RESTRICT",
            name="fk_memories_tenant_agent",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "supersedes_id"],
            ["memories.tenant_id", "memories.id"],
            ondelete="RESTRICT",
            name="fk_memories_tenant_supersedes",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_memories_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "memory_key", "version", name="uq_memories_key_version"),
    )
    op.create_index(
        "uq_memories_active_key",
        "memories",
        ["tenant_id", "memory_key"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_memories_tenant_scope_status",
        "memories",
        ["tenant_id", "scope_type", "status", "created_at"],
    )


def _create_skills() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("scope_type", sa.String(32), nullable=False),
        sa.Column("project_id", sa.Uuid()),
        sa.Column("agent_id", sa.Uuid()),
        sa.Column("status", sa.String(32), nullable=False, server_default="candidate"),
        sa.Column("current_version_id", sa.Uuid()),
        sa.Column("success_stats", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('candidate', 'testing', 'approved', 'published', 'deprecated', 'disabled')",
            name="ck_skills_status",
        ),
        sa.CheckConstraint(
            "scope_type IN ('tenant', 'project', 'agent')",
            name="ck_skills_scope_goal_f",
        ),
        sa.CheckConstraint(
            "(scope_type = 'tenant' AND project_id IS NULL AND agent_id IS NULL) OR "
            "(scope_type = 'project' AND project_id IS NOT NULL AND agent_id IS NULL) OR "
            "(scope_type = 'agent' AND project_id IS NULL AND agent_id IS NOT NULL)",
            name="ck_skills_scope_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_skills_tenant_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="RESTRICT",
            name="fk_skills_tenant_agent",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_skills_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_skills_tenant_name"),
    )
    op.create_index("ix_skills_tenant_status", "skills", ["tenant_id", "status", "created_at"])

    op.create_table(
        "skill_versions",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("conditions", postgresql.JSONB(), nullable=False),
        sa.Column("preconditions", postgresql.JSONB(), nullable=False),
        sa.Column("input_schema", postgresql.JSONB(), nullable=False),
        sa.Column("steps", postgresql.JSONB(), nullable=False),
        sa.Column("tools", postgresql.JSONB(), nullable=False),
        sa.Column("output_schema", postgresql.JSONB(), nullable=False),
        sa.Column("validation", postgresql.JSONB(), nullable=False),
        sa.Column("failure_modes", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('draft', 'testing', 'published', 'rejected')",
            name="ck_skill_versions_status",
        ),
        sa.CheckConstraint("version > 0", name="ck_skill_versions_version_positive"),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_skill_versions_content_hash"),
        sa.CheckConstraint(
            "status <> 'published' OR (approved_at IS NOT NULL AND published_at IS NOT NULL)",
            name="ck_skill_versions_published_approved",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "skill_id"],
            ["skills.tenant_id", "skills.id"],
            ondelete="RESTRICT",
            name="fk_skill_versions_tenant_skill",
        ),
        sa.UniqueConstraint(
            "tenant_id", "skill_id", "id", name="uq_skill_versions_tenant_skill_id"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_skill_versions_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "skill_id", "version", name="uq_skill_versions_skill_version"
        ),
    )
    op.create_index(
        "ix_skill_versions_tenant_status",
        "skill_versions",
        ["tenant_id", "status", "created_at"],
    )
    op.create_foreign_key(
        "fk_skills_current_version",
        "skills",
        "skill_versions",
        ["tenant_id", "id", "current_version_id"],
        ["tenant_id", "skill_id", "id"],
        ondelete="RESTRICT",
    )


def _create_growth_sources() -> None:
    op.create_table(
        "growth_sources",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("memory_id", sa.Uuid()),
        sa.Column("skill_version_id", sa.Uuid()),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("run_step_id", sa.Uuid(), nullable=False),
        sa.Column("tool_call_id", sa.Uuid()),
        sa.Column("runtime_session_id", sa.Uuid()),
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("trajectory_hash", sa.String(64), nullable=False),
        sa.Column("generator_name", sa.String(120), nullable=False),
        sa.Column("generator_version", sa.String(80), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "subject_type IN ('memory', 'skill_version')",
            name="ck_growth_sources_subject_type",
        ),
        sa.CheckConstraint(
            "(subject_type = 'memory' AND memory_id IS NOT NULL AND "
            "skill_version_id IS NULL) OR "
            "(subject_type = 'skill_version' AND memory_id IS NULL AND "
            "skill_version_id IS NOT NULL)",
            name="ck_growth_sources_subject",
        ),
        sa.CheckConstraint(
            "length(trajectory_hash) = 64 AND length(source_hash) = 64",
            name="ck_growth_sources_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "memory_id"],
            ["memories.tenant_id", "memories.id"],
            ondelete="RESTRICT",
            name="fk_growth_sources_tenant_memory",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "skill_version_id"],
            ["skill_versions.tenant_id", "skill_versions.id"],
            ondelete="RESTRICT",
            name="fk_growth_sources_tenant_skill_version",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_growth_sources_tenant_run",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "run_step_id"],
            ["run_steps.tenant_id", "run_steps.run_id", "run_steps.id"],
            ondelete="RESTRICT",
            name="fk_growth_sources_tenant_run_step",
        ),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "runtime_session_id"],
            ["runtime_sessions.tenant_id", "runtime_sessions.run_id", "runtime_sessions.id"],
            ondelete="RESTRICT",
            name="fk_growth_sources_tenant_runtime_session",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_version_id"],
            ["agent_versions.tenant_id", "agent_versions.id"],
            ondelete="RESTRICT",
            name="fk_growth_sources_tenant_agent_version",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_growth_sources_tenant_id_id"),
    )
    op.create_index(
        "uq_growth_sources_memory_hash",
        "growth_sources",
        ["tenant_id", "memory_id", "source_hash"],
        unique=True,
        postgresql_where=sa.text("subject_type = 'memory'"),
    )
    op.create_index(
        "uq_growth_sources_skill_hash",
        "growth_sources",
        ["tenant_id", "skill_version_id", "source_hash"],
        unique=True,
        postgresql_where=sa.text("subject_type = 'skill_version'"),
    )
    op.create_index(
        "ix_growth_sources_tenant_run",
        "growth_sources",
        ["tenant_id", "run_id", "created_at"],
    )


def _subject_constraints(prefix: str) -> list[SchemaItem]:
    return [
        sa.CheckConstraint(
            "subject_type IN ('memory', 'skill_version')",
            name=f"ck_{prefix}_subject_type",
        ),
        sa.CheckConstraint(
            "(subject_type = 'memory' AND memory_id IS NOT NULL AND "
            "skill_version_id IS NULL) OR "
            "(subject_type = 'skill_version' AND memory_id IS NULL AND "
            "skill_version_id IS NOT NULL)",
            name=f"ck_{prefix}_subject",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "memory_id"],
            ["memories.tenant_id", "memories.id"],
            ondelete="RESTRICT",
            name=f"fk_{prefix}_tenant_memory",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "skill_version_id"],
            ["skill_versions.tenant_id", "skill_versions.id"],
            ondelete="RESTRICT",
            name=f"fk_{prefix}_tenant_skill_version",
        ),
    ]


def _create_evaluations_and_approvals() -> None:
    op.create_table(
        "evaluations",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("memory_id", sa.Uuid()),
        sa.Column("skill_version_id", sa.Uuid()),
        sa.Column("evaluator_name", sa.String(120), nullable=False),
        sa.Column("evaluator_version", sa.String(80), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("score", sa.Float()),
        sa.Column("verdict", sa.String(20)),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default=JSON_ARRAY),
        sa.Column("error", postgresql.JSONB()),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        *_subject_constraints("evaluations"),
        sa.CheckConstraint(
            "status IN ('pending', 'completed', 'failed')", name="ck_evaluations_status"
        ),
        sa.CheckConstraint(
            "verdict IS NULL OR verdict IN ('pass', 'fail')",
            name="ck_evaluations_verdict",
        ),
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1)", name="ck_evaluations_score"
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND verdict IS NULL AND ended_at IS NULL) OR "
            "(status = 'completed' AND verdict IS NOT NULL AND ended_at IS NOT NULL) OR "
            "(status = 'failed' AND verdict IS NULL AND error IS NOT NULL AND "
            "ended_at IS NOT NULL)",
            name="ck_evaluations_terminal_shape",
        ),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_evaluations_content_hash"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_evaluations_tenant_id_id"),
    )
    op.create_index(
        "ix_evaluations_tenant_subject",
        "evaluations",
        ["tenant_id", "subject_type", "memory_id", "skill_version_id", "created_at"],
    )

    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("memory_id", sa.Uuid()),
        sa.Column("skill_version_id", sa.Uuid()),
        sa.Column("action", sa.String(32), nullable=False, server_default="publish"),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="requested"),
        sa.Column("requester", sa.String(200), nullable=False),
        sa.Column("reviewer", sa.String(200)),
        sa.Column("reason", sa.Text()),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        *_subject_constraints("approvals"),
        sa.CheckConstraint("action = 'publish'", name="ck_approvals_action_goal_f"),
        sa.CheckConstraint(
            "status IN ('requested', 'approved', 'rejected', 'cancelled', 'expired')",
            name="ck_approvals_status",
        ),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_approvals_content_hash"),
        sa.CheckConstraint(
            "(status = 'requested' AND reviewer IS NULL AND decided_at IS NULL) OR "
            "(status IN ('approved', 'rejected') AND reviewer IS NOT NULL AND "
            "reason IS NOT NULL AND decided_at IS NOT NULL) OR "
            "(status IN ('cancelled', 'expired') AND reason IS NOT NULL AND "
            "decided_at IS NOT NULL)",
            name="ck_approvals_terminal_shape",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_approvals_tenant_id_id"),
    )
    op.create_index(
        "ix_approvals_tenant_status",
        "approvals",
        ["tenant_id", "status", "created_at"],
    )


def _create_skill_deployments() -> None:
    op.create_table(
        "skill_deployments",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column("scope_type", sa.String(32), nullable=False),
        sa.Column("project_id", sa.Uuid()),
        sa.Column("agent_id", sa.Uuid()),
        sa.Column("rollout_percentage", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("retired_by", sa.String(200)),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "scope_type IN ('project', 'agent')",
            name="ck_skill_deployments_scope_goal_f",
        ),
        sa.CheckConstraint(
            "(scope_type = 'project' AND project_id IS NOT NULL AND agent_id IS NULL) OR "
            "(scope_type = 'agent' AND project_id IS NULL AND agent_id IS NOT NULL)",
            name="ck_skill_deployments_scope_owner",
        ),
        sa.CheckConstraint(
            "rollout_percentage >= 1 AND rollout_percentage <= 99",
            name="ck_skill_deployments_rollout",
        ),
        sa.CheckConstraint("status IN ('active', 'retired')", name="ck_skill_deployments_status"),
        sa.CheckConstraint(
            "(status = 'active' AND retired_at IS NULL) OR "
            "(status = 'retired' AND retired_at IS NOT NULL)",
            name="ck_skill_deployments_terminal_shape",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "skill_id"],
            ["skills.tenant_id", "skills.id"],
            ondelete="RESTRICT",
            name="fk_skill_deployments_tenant_skill",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "skill_id", "skill_version_id"],
            ["skill_versions.tenant_id", "skill_versions.skill_id", "skill_versions.id"],
            ondelete="RESTRICT",
            name="fk_skill_deployments_tenant_version",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_skill_deployments_tenant_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="RESTRICT",
            name="fk_skill_deployments_tenant_agent",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_skill_deployments_tenant_id_id"),
    )
    op.create_index(
        "uq_skill_deployments_project_active",
        "skill_deployments",
        ["tenant_id", "skill_id", "project_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND scope_type = 'project'"),
    )
    op.create_index(
        "uq_skill_deployments_agent_active",
        "skill_deployments",
        ["tenant_id", "skill_id", "agent_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND scope_type = 'agent'"),
    )


def _create_guard_functions_and_triggers() -> None:
    op.execute(_MEMORY_GUARD_SQL)
    op.execute(
        "CREATE TRIGGER guard_memory_write BEFORE INSERT OR UPDATE ON memories "
        "FOR EACH ROW EXECUTE FUNCTION guard_memory_write()"
    )
    op.execute(_SKILL_VERSION_GUARD_SQL)
    op.execute(
        "CREATE TRIGGER guard_skill_version_write BEFORE INSERT OR UPDATE ON skill_versions "
        "FOR EACH ROW EXECUTE FUNCTION guard_skill_version_write()"
    )
    op.execute(_SKILL_GUARD_SQL)
    op.execute(
        "CREATE TRIGGER guard_skill_write BEFORE INSERT OR UPDATE ON skills "
        "FOR EACH ROW EXECUTE FUNCTION guard_skill_write()"
    )
    op.execute(_GROWTH_SOURCE_GUARD_SQL)
    op.execute(
        "CREATE TRIGGER guard_growth_source_insert BEFORE INSERT ON growth_sources "
        "FOR EACH ROW EXECUTE FUNCTION guard_growth_source_insert()"
    )
    op.execute(_EVALUATION_GUARD_SQL)
    op.execute(
        "CREATE TRIGGER guard_evaluation_write BEFORE INSERT OR UPDATE ON evaluations "
        "FOR EACH ROW EXECUTE FUNCTION guard_evaluation_write()"
    )
    op.execute(_APPROVAL_GUARD_SQL)
    op.execute(
        "CREATE TRIGGER guard_approval_write BEFORE INSERT OR UPDATE ON approvals "
        "FOR EACH ROW EXECUTE FUNCTION guard_approval_write()"
    )
    op.execute(_DEPLOYMENT_GUARD_SQL)
    op.execute(
        "CREATE TRIGGER guard_skill_deployment_write "
        "BEFORE INSERT OR UPDATE ON skill_deployments "
        "FOR EACH ROW EXECUTE FUNCTION guard_skill_deployment_write()"
    )
    op.execute(_NO_DELETE_SQL)
    for table in (
        "memories",
        "skills",
        "skill_versions",
        "growth_sources",
        "evaluations",
        "approvals",
        "skill_deployments",
    ):
        op.execute(
            f"CREATE TRIGGER guard_{table}_delete BEFORE DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_growth_delete()"
        )


_MEMORY_GUARD_SQL = r"""
CREATE FUNCTION guard_memory_write()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
DECLARE
    previous memories%ROWTYPE;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'candidate' THEN
            RAISE EXCEPTION 'memory must start as candidate'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.version = 1 AND NEW.supersedes_id IS NOT NULL THEN
            RAISE EXCEPTION 'first memory version cannot supersede another version'
                USING ERRCODE = 'integrity_constraint_violation';
        ELSIF NEW.version > 1 THEN
            IF NEW.supersedes_id IS NULL THEN
                RAISE EXCEPTION 'later memory version requires supersedes_id'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            SELECT * INTO previous FROM public.memories
             WHERE tenant_id = NEW.tenant_id AND id = NEW.supersedes_id;
            IF NOT FOUND OR previous.memory_key <> NEW.memory_key
               OR previous.version + 1 <> NEW.version
               OR previous.memory_type <> NEW.memory_type
               OR previous.scope_type <> NEW.scope_type
               OR previous.project_id IS DISTINCT FROM NEW.project_id
               OR previous.agent_id IS DISTINCT FROM NEW.agent_id THEN
                RAISE EXCEPTION 'superseded memory must be the prior version in the same scope'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.memory_key IS DISTINCT FROM OLD.memory_key
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.memory_type IS DISTINCT FROM OLD.memory_type
       OR NEW.scope_type IS DISTINCT FROM OLD.scope_type
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.agent_id IS DISTINCT FROM OLD.agent_id
       OR NEW.content IS DISTINCT FROM OLD.content
       OR NEW.confidence IS DISTINCT FROM OLD.confidence
       OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
       OR NEW.supersedes_id IS DISTINCT FROM OLD.supersedes_id
       OR NEW.created_by IS DISTINCT FROM OLD.created_by
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
        RAISE EXCEPTION 'memory content, scope, version, source identity and expiry are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'candidate' AND NEW.status IN ('active', 'deleted')) OR
        (OLD.status = 'active' AND NEW.status IN ('invalidated', 'expired', 'deleted')) OR
        (OLD.status IN ('invalidated', 'expired') AND NEW.status = 'deleted')
    ) THEN
        RAISE EXCEPTION 'invalid memory state transition'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status = 'deleted' THEN
        RAISE EXCEPTION 'deleted memory is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status = 'active' AND OLD.status <> 'active' THEN
        IF NOT EXISTS (
            SELECT 1 FROM public.growth_sources g
             WHERE g.tenant_id = NEW.tenant_id AND g.memory_id = NEW.id
        ) OR NOT EXISTS (
            SELECT 1 FROM public.evaluations e
             WHERE e.tenant_id = NEW.tenant_id AND e.memory_id = NEW.id
               AND e.content_hash = NEW.content_hash
               AND e.status = 'completed' AND e.verdict = 'pass'
        ) OR NOT EXISTS (
            SELECT 1 FROM public.approvals a
             WHERE a.tenant_id = NEW.tenant_id AND a.memory_id = NEW.id
               AND a.content_hash = NEW.content_hash AND a.action = 'publish'
               AND a.status = 'approved'
               AND (a.expires_at IS NULL OR a.expires_at > clock_timestamp())
        ) THEN
            RAISE EXCEPTION 'memory publication requires source, passing evaluation and approval'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    IF NEW.status = 'expired'
       AND (NEW.expires_at IS NULL OR NEW.expires_at > clock_timestamp()) THEN
        RAISE EXCEPTION 'memory cannot expire before expires_at'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status = 'invalidated' AND NEW.invalidated_at IS NULL THEN
        RAISE EXCEPTION 'invalidated memory requires invalidated_at'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status = 'deleted' AND NEW.deleted_at IS NULL THEN
        RAISE EXCEPTION 'deleted memory requires deleted_at'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$
"""


_SKILL_VERSION_GUARD_SQL = r"""
CREATE FUNCTION guard_skill_version_write()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' THEN
            RAISE EXCEPTION 'skill version must start as draft'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.skill_id IS DISTINCT FROM OLD.skill_id
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.conditions IS DISTINCT FROM OLD.conditions
       OR NEW.preconditions IS DISTINCT FROM OLD.preconditions
       OR NEW.input_schema IS DISTINCT FROM OLD.input_schema
       OR NEW.steps IS DISTINCT FROM OLD.steps
       OR NEW.tools IS DISTINCT FROM OLD.tools
       OR NEW.output_schema IS DISTINCT FROM OLD.output_schema
       OR NEW.validation IS DISTINCT FROM OLD.validation
       OR NEW.failure_modes IS DISTINCT FROM OLD.failure_modes
       OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
       OR NEW.created_by IS DISTINCT FROM OLD.created_by THEN
        RAISE EXCEPTION 'skill version content and identity are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'draft' AND NEW.status IN ('testing', 'rejected')) OR
        (OLD.status = 'testing' AND NEW.status IN ('published', 'rejected'))
    ) THEN
        RAISE EXCEPTION 'invalid skill version state transition'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status IN ('published', 'rejected') THEN
        RAISE EXCEPTION 'terminal skill version is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status = 'published' AND OLD.status <> 'published' THEN
        IF NOT EXISTS (
            SELECT 1 FROM public.growth_sources g
             WHERE g.tenant_id = NEW.tenant_id AND g.skill_version_id = NEW.id
        ) OR NOT EXISTS (
            SELECT 1 FROM public.evaluations e
             WHERE e.tenant_id = NEW.tenant_id AND e.skill_version_id = NEW.id
               AND e.content_hash = NEW.content_hash
               AND e.status = 'completed' AND e.verdict = 'pass'
        ) OR NOT EXISTS (
            SELECT 1 FROM public.approvals a
             WHERE a.tenant_id = NEW.tenant_id AND a.skill_version_id = NEW.id
               AND a.content_hash = NEW.content_hash AND a.action = 'publish'
               AND a.status = 'approved'
               AND (a.expires_at IS NULL OR a.expires_at > clock_timestamp())
        ) THEN
            RAISE EXCEPTION 'skill publication requires source, passing evaluation and approval'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$function$
"""


_SKILL_GUARD_SQL = r"""
CREATE FUNCTION guard_skill_write()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
DECLARE
    pointed_status text;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'candidate' OR NEW.current_version_id IS NOT NULL THEN
            RAISE EXCEPTION 'skill must start as candidate without active version'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.name IS DISTINCT FROM OLD.name
       OR NEW.description IS DISTINCT FROM OLD.description
       OR NEW.scope_type IS DISTINCT FROM OLD.scope_type
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.agent_id IS DISTINCT FROM OLD.agent_id
       OR NEW.created_by IS DISTINCT FROM OLD.created_by THEN
        RAISE EXCEPTION 'skill identity and scope are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
        (OLD.status = 'candidate' AND NEW.status IN ('testing', 'disabled')) OR
        (OLD.status = 'testing' AND NEW.status IN ('candidate', 'approved', 'disabled')) OR
        (OLD.status = 'approved' AND NEW.status IN ('published', 'disabled')) OR
        (OLD.status = 'published' AND NEW.status IN ('deprecated', 'disabled')) OR
        (OLD.status = 'deprecated' AND NEW.status IN ('published', 'disabled'))
    ) THEN
        RAISE EXCEPTION 'invalid skill state transition'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status = 'disabled' THEN
        RAISE EXCEPTION 'disabled skill is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status = 'published' THEN
        IF NEW.current_version_id IS NULL THEN
            RAISE EXCEPTION 'published skill requires current version'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        SELECT status INTO pointed_status FROM public.skill_versions
         WHERE tenant_id = NEW.tenant_id AND skill_id = NEW.id AND id = NEW.current_version_id;
        IF pointed_status IS DISTINCT FROM 'published' THEN
            RAISE EXCEPTION 'skill current version must be published'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    ELSIF NEW.status IN ('candidate', 'testing', 'approved')
          AND NEW.current_version_id IS NOT NULL THEN
        RAISE EXCEPTION 'unpublished skill cannot have current version'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$
"""


_GROWTH_SOURCE_GUARD_SQL = r"""
CREATE FUNCTION guard_growth_source_insert()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
DECLARE
    source_run public.runs%ROWTYPE;
    step_status text;
    call_status text;
    session_status text;
BEGIN
    SELECT * INTO source_run FROM public.runs
     WHERE tenant_id = NEW.tenant_id AND id = NEW.run_id;
    IF NOT FOUND OR source_run.status NOT IN ('completed', 'failed', 'cancelled', 'timed_out') THEN
        RAISE EXCEPTION 'growth source run must be terminal'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF source_run.agent_version_id <> NEW.agent_version_id THEN
        RAISE EXCEPTION 'growth source agent version must match run snapshot'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT status INTO step_status FROM public.run_steps
     WHERE tenant_id = NEW.tenant_id AND run_id = NEW.run_id AND id = NEW.run_step_id;
    IF step_status IS NULL OR step_status NOT IN ('completed', 'failed', 'cancelled') THEN
        RAISE EXCEPTION 'growth source step must be terminal'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.tool_call_id IS NOT NULL THEN
        SELECT status INTO call_status FROM public.tool_calls
         WHERE tenant_id = NEW.tenant_id AND run_id = NEW.run_id
           AND run_step_id = NEW.run_step_id AND id = NEW.tool_call_id;
        IF call_status IS NULL OR call_status NOT IN
            ('succeeded', 'failed', 'timed_out', 'cancelled') THEN
            RAISE EXCEPTION 'growth source tool call must be terminal'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    IF NEW.runtime_session_id IS NOT NULL THEN
        SELECT status INTO session_status FROM public.runtime_sessions
         WHERE tenant_id = NEW.tenant_id AND run_id = NEW.run_id
           AND id = NEW.runtime_session_id;
        IF session_status IS NULL OR session_status NOT IN
            ('completed', 'failed', 'cancelled') THEN
            RAISE EXCEPTION 'growth source runtime session must be terminal'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$function$
"""


_EVALUATION_GUARD_SQL = r"""
CREATE FUNCTION guard_evaluation_write()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
DECLARE
    subject_hash text;
BEGIN
    IF NEW.subject_type = 'memory' THEN
        SELECT content_hash INTO subject_hash FROM public.memories
         WHERE tenant_id = NEW.tenant_id AND id = NEW.memory_id;
    ELSE
        SELECT content_hash INTO subject_hash FROM public.skill_versions
         WHERE tenant_id = NEW.tenant_id AND id = NEW.skill_version_id;
    END IF;
    IF subject_hash IS NULL OR subject_hash <> NEW.content_hash THEN
        RAISE EXCEPTION 'evaluation content hash must match subject'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'pending' THEN
            RAISE EXCEPTION 'evaluation must start pending'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.subject_type IS DISTINCT FROM OLD.subject_type
       OR NEW.memory_id IS DISTINCT FROM OLD.memory_id
       OR NEW.skill_version_id IS DISTINCT FROM OLD.skill_version_id
       OR NEW.evaluator_name IS DISTINCT FROM OLD.evaluator_name
       OR NEW.evaluator_version IS DISTINCT FROM OLD.evaluator_version
       OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
       OR NEW.created_by IS DISTINCT FROM OLD.created_by THEN
        RAISE EXCEPTION 'evaluation identity is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status <> 'pending' OR NEW.status NOT IN ('completed', 'failed') THEN
        RAISE EXCEPTION 'terminal evaluation is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$
"""


_APPROVAL_GUARD_SQL = r"""
CREATE FUNCTION guard_approval_write()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
DECLARE
    subject_hash text;
BEGIN
    IF NEW.subject_type = 'memory' THEN
        SELECT content_hash INTO subject_hash FROM public.memories
         WHERE tenant_id = NEW.tenant_id AND id = NEW.memory_id;
    ELSE
        SELECT content_hash INTO subject_hash FROM public.skill_versions
         WHERE tenant_id = NEW.tenant_id AND id = NEW.skill_version_id;
    END IF;
    IF subject_hash IS NULL OR subject_hash <> NEW.content_hash THEN
        RAISE EXCEPTION 'approval content hash must match subject'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'requested' THEN
            RAISE EXCEPTION 'approval must start requested'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.subject_type IS DISTINCT FROM OLD.subject_type
       OR NEW.memory_id IS DISTINCT FROM OLD.memory_id
       OR NEW.skill_version_id IS DISTINCT FROM OLD.skill_version_id
       OR NEW.action IS DISTINCT FROM OLD.action
       OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
       OR NEW.requester IS DISTINCT FROM OLD.requester
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
        RAISE EXCEPTION 'approval request identity is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status <> 'requested' OR NEW.status NOT IN
        ('approved', 'rejected', 'cancelled', 'expired') THEN
        RAISE EXCEPTION 'terminal approval is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$
"""


_DEPLOYMENT_GUARD_SQL = r"""
CREATE FUNCTION guard_skill_deployment_write()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
DECLARE
    version_status text;
BEGIN
    SELECT status INTO version_status FROM public.skill_versions
     WHERE tenant_id = NEW.tenant_id AND skill_id = NEW.skill_id
       AND id = NEW.skill_version_id;
    IF version_status IS DISTINCT FROM 'published' THEN
        RAISE EXCEPTION 'skill deployment requires published version'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'active' THEN
            RAISE EXCEPTION 'skill deployment must start active'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.skill_id IS DISTINCT FROM OLD.skill_id
       OR NEW.skill_version_id IS DISTINCT FROM OLD.skill_version_id
       OR NEW.scope_type IS DISTINCT FROM OLD.scope_type
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.agent_id IS DISTINCT FROM OLD.agent_id
       OR NEW.rollout_percentage IS DISTINCT FROM OLD.rollout_percentage
       OR NEW.created_by IS DISTINCT FROM OLD.created_by THEN
        RAISE EXCEPTION 'skill deployment definition is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status <> 'active' OR NEW.status <> 'retired' THEN
        RAISE EXCEPTION 'retired skill deployment is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$
"""


_NO_DELETE_SQL = r"""
CREATE FUNCTION reject_growth_delete()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
BEGIN
    RAISE EXCEPTION 'controlled growth records cannot be physically deleted'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$function$
"""


def downgrade() -> None:
    for table in (
        "skill_deployments",
        "approvals",
        "evaluations",
        "growth_sources",
    ):
        op.drop_table(table)
    op.drop_constraint("fk_skills_current_version", "skills", type_="foreignkey")
    op.drop_table("skill_versions")
    op.drop_table("skills")
    op.drop_table("memories")

    for function in (
        "reject_growth_delete",
        "guard_skill_deployment_write",
        "guard_approval_write",
        "guard_evaluation_write",
        "guard_growth_source_insert",
        "guard_skill_write",
        "guard_skill_version_write",
        "guard_memory_write",
    ):
        op.execute(f"DROP FUNCTION {function}()")

    op.drop_constraint("uq_tool_calls_tenant_run_step_id", "tool_calls", type_="unique")
    op.drop_constraint("uq_runtime_sessions_tenant_run_id", "runtime_sessions", type_="unique")
    op.drop_constraint("uq_agent_versions_tenant_id_id", "agent_versions", type_="unique")
