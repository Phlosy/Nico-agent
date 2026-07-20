"""Create the tenant-scoped core control-plane schema.

Revision ID: 20260717_0002
Revises: 20260716_0001
Create Date: 2026-07-17 00:00:00 UTC
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260717_0002"
down_revision: str | None = "20260716_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_OBJECT = sa.text("'{}'::jsonb")
JSON_ARRAY = sa.text("'[]'::jsonb")
NOW = sa.text("now()")
UUID_DEFAULT = sa.text("gen_random_uuid()")


def _identity() -> sa.Identity:
    return sa.Identity(start=1)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    ]


def _enable_rls(table: str, tenant_column: str = "tenant_id") -> None:
    predicate = f"{tenant_column} = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "tenant_isolation" ON "{table}" '
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("settings", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("limits", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'archived')", name="ck_tenants_status"
        ),
    )

    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_projects_status"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_projects_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_projects_tenant_name"),
    )

    op.create_table(
        "agents",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("current_version_id", sa.Uuid()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('draft', 'ready', 'running', 'paused', 'archived', 'error')",
            name="ck_agents_status",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_agents_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "name", name="uq_agents_tenant_name"),
    )

    op.create_table(
        "agent_versions",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("role", sa.String(120), nullable=False),
        sa.Column("mandate", sa.Text(), nullable=False),
        sa.Column("boundaries", postgresql.JSONB(), nullable=False, server_default=JSON_ARRAY),
        sa.Column("long_term_goal", sa.Text()),
        sa.Column("current_goal", sa.Text()),
        sa.Column("model_config", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("tool_policy", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("memory_policy", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("skill_policy", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("plugin_refs", postgresql.JSONB(), nullable=False, server_default=JSON_ARRAY),
        sa.Column("budgets", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("run_config", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("content_hash", sa.String(64), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'superseded')",
            name="ck_agent_versions_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="RESTRICT",
            name="fk_agent_versions_tenant_agent",
        ),
        sa.UniqueConstraint("tenant_id", "agent_id", "id", name="uq_agent_versions_scope_id"),
        sa.UniqueConstraint(
            "tenant_id", "agent_id", "version", name="uq_agent_versions_scope_version"
        ),
    )
    op.create_foreign_key(
        "fk_agents_current_version",
        "agents",
        "agent_versions",
        ["tenant_id", "id", "current_version_id"],
        ["tenant_id", "agent_id", "id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("assignee_agent_id", sa.Uuid()),
        sa.Column("parent_task_id", sa.Uuid()),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("input", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("acceptance", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("status", sa.String(32), nullable=False, server_default="created"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('created', 'assigned', 'running', 'waiting_for_review', "
            "'revision_required', 'completed', 'failed', 'cancelled')",
            name="ck_tasks_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_tasks_tenant_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "assignee_agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="RESTRICT",
            name="fk_tasks_tenant_assignee",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "parent_task_id"],
            ["tasks.tenant_id", "tasks.id"],
            ondelete="RESTRICT",
            name="fk_tasks_tenant_parent",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tasks_tenant_id_id"),
    )
    op.create_index(
        "ix_tasks_tenant_status_created", "tasks", ["tenant_id", "status", "created_at"]
    )

    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("retry_of_run_id", sa.Uuid()),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("max_steps", sa.Integer(), nullable=False, server_default="64"),
        sa.Column("token_budget", sa.BigInteger()),
        sa.Column("timeout_seconds", sa.Integer()),
        sa.Column("budgets", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("lease_owner", sa.String(200)),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("checkpoint", postgresql.JSONB()),
        sa.Column("cost", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("error", postgresql.JSONB()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'planning', 'running', 'waiting_for_tool', "
            "'waiting_for_approval', 'paused', 'completed', 'failed', 'cancelled', "
            "'timed_out')",
            name="ck_runs_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.id"],
            ondelete="RESTRICT",
            name="fk_runs_tenant_task",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="RESTRICT",
            name="fk_runs_tenant_agent",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_id", "agent_version_id"],
            ["agent_versions.tenant_id", "agent_versions.agent_id", "agent_versions.id"],
            ondelete="RESTRICT",
            name="fk_runs_tenant_agent_version",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "retry_of_run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_runs_tenant_retry",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_runs_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "task_id", "attempt", name="uq_runs_task_attempt"),
    )
    op.create_index("ix_runs_claimable", "runs", ["status", "lease_expires_at", "created_at"])
    op.create_index("ix_runs_tenant_task_created", "runs", ["tenant_id", "task_id", "created_at"])

    op.create_table(
        "run_steps",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(100), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("input", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("output", postgresql.JSONB()),
        sa.Column("error", postgresql.JSONB()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'waiting', 'completed', 'failed', 'cancelled')",
            name="ck_run_steps_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_run_steps_tenant_run",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_run_steps_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "run_id", "sequence", name="uq_run_steps_sequence"),
    )

    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), _identity(), nullable=False),
        sa.Column("event_type", sa.String(150), nullable=False),
        sa.Column("aggregate_type", sa.String(100), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid()),
        sa.Column("actor_id", sa.String(200), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("causation_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_events_tenant_run",
        ),
        sa.UniqueConstraint("tenant_id", "sequence", name="uq_events_tenant_sequence"),
    )
    op.create_index(
        "ix_events_tenant_aggregate",
        "events",
        ["tenant_id", "aggregate_type", "aggregate_id"],
    )

    op.create_table(
        "audit_records",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), _identity(), nullable=False),
        sa.Column("action", sa.String(150), nullable=False),
        sa.Column("resource_type", sa.String(100), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.String(200), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("tenant_id", "sequence", name="uq_audit_tenant_sequence"),
    )
    op.create_index(
        "ix_audit_tenant_resource",
        "audit_records",
        ["tenant_id", "resource_type", "resource_id"],
    )

    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nico_runtime') THEN "
        "CREATE ROLE nico_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
        "NOREPLICATION NOBYPASSRLS; END IF; END $$"
    )
    op.execute("GRANT nico_runtime TO CURRENT_USER")
    op.execute("GRANT USAGE ON SCHEMA public TO nico_runtime")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON "
        "tenants, projects, agents, agent_versions, tasks, runs, run_steps, events, "
        "audit_records TO nico_runtime"
    )
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO nico_runtime")

    _enable_rls("tenants", "id")
    for table in (
        "projects",
        "agents",
        "agent_versions",
        "tasks",
        "runs",
        "run_steps",
        "events",
        "audit_records",
    ):
        _enable_rls(table)


def downgrade() -> None:
    op.execute("REVOKE USAGE ON SCHEMA public FROM nico_runtime")
    op.drop_constraint("fk_agents_current_version", "agents", type_="foreignkey")
    for table in (
        "audit_records",
        "events",
        "run_steps",
        "runs",
        "tasks",
        "agent_versions",
        "agents",
        "projects",
        "tenants",
    ):
        op.drop_table(table)
    op.execute("REVOKE nico_runtime FROM CURRENT_USER")
    op.execute("DROP ROLE IF EXISTS nico_runtime")
