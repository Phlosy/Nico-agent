"""Add project membership, stable sessions, supervision, and interventions.

Revision ID: 20260721_0022
Revises: 20260720_0021
Create Date: 2026-07-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260721_0022"
down_revision: str | None = "20260720_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")
UUID_DEFAULT = sa.text("gen_random_uuid()")
JSON_OBJECT = sa.text("'{}'::jsonb")


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    ]


def _enable_rls(table: str) -> None:
    predicate = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(
        f'CREATE POLICY tenant_isolation ON "{table}" USING ({predicate}) WITH CHECK ({predicate})'
    )
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')


def upgrade() -> None:
    op.add_column(
        "projects", sa.Column("kind", sa.String(32), nullable=False, server_default="shared")
    )
    op.add_column("projects", sa.Column("owner_actor_id", sa.String(200)))
    op.add_column("projects", sa.Column("supervision_cadence_seconds", sa.Integer()))
    op.add_column(
        "projects", sa.Column("next_supervision_at", sa.DateTime(timezone=True))
    )
    op.create_check_constraint("ck_projects_kind", "projects", "kind IN ('shared', 'personal')")
    op.create_check_constraint(
        "ck_projects_kind_owner",
        "projects",
        "(kind = 'personal' AND owner_actor_id IS NOT NULL "
        "AND supervision_cadence_seconds IS NULL) OR "
        "(kind = 'shared' AND owner_actor_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_projects_supervision_cadence",
        "projects",
        "supervision_cadence_seconds IS NULL OR "
        "supervision_cadence_seconds BETWEEN 300 AND 604800",
    )
    op.create_index(
        "uq_projects_personal_owner",
        "projects",
        ["tenant_id", "owner_actor_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'personal'"),
    )

    op.add_column("conversations", sa.Column("project_session_id", sa.Uuid()))
    op.create_unique_constraint(
        "uq_conversations_project_agent_id",
        "conversations",
        ["tenant_id", "project_id", "agent_id", "id"],
    )
    op.add_column("tasks", sa.Column("project_session_id", sa.Uuid()))
    op.create_unique_constraint(
        "uq_tasks_project_session_id",
        "tasks",
        ["tenant_id", "project_id", "project_session_id", "id"],
    )

    op.create_table(
        "project_members",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False, server_default="member"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("removal_reason", sa.Text()),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint("role IN ('lead', 'member')", name="ck_project_members_role"),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'removed')",
            name="ck_project_members_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_project_members_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="RESTRICT",
            name="fk_project_members_agent",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_project_members_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "id", name="uq_project_members_project_id"
        ),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "agent_id", name="uq_project_members_project_agent"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "agent_id",
            "id",
            name="uq_project_members_scope_id",
        ),
    )
    op.create_index(
        "uq_project_members_active_lead",
        "project_members",
        ["tenant_id", "project_id"],
        unique=True,
        postgresql_where=sa.text("role = 'lead' AND status = 'active'"),
    )
    op.create_index(
        "ix_project_members_active",
        "project_members",
        ["tenant_id", "project_id", "status", "created_at"],
    )

    op.create_table(
        "project_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("project_member_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("current_conversation_id", sa.Uuid()),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'archived')",
            name="ck_project_sessions_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_project_sessions_project",
        ),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "agent_id", "current_conversation_id"],
            [
                "conversations.tenant_id",
                "conversations.project_id",
                "conversations.agent_id",
                "conversations.id",
            ],
            ondelete="RESTRICT",
            name="fk_project_sessions_current_conversation",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_project_sessions_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "id", name="uq_project_sessions_project_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "project_member_id",
            "id",
            name="uq_project_sessions_member_id",
        ),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "agent_id", name="uq_project_sessions_project_agent"
        ),
        sa.UniqueConstraint(
            "tenant_id", "project_member_id", name="uq_project_sessions_member"
        ),
    )
    op.create_index(
        "ix_project_sessions_status",
        "project_sessions",
        ["tenant_id", "project_id", "status", "updated_at"],
    )
    op.create_foreign_key(
        "fk_conversations_project_session",
        "conversations",
        "project_sessions",
        ["tenant_id", "project_id", "project_session_id"],
        ["tenant_id", "project_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_tasks_project_session",
        "tasks",
        "project_sessions",
        ["tenant_id", "project_id", "project_session_id"],
        ["tenant_id", "project_id", "id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "project_supervision_cycles",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("lead_project_member_id", sa.Uuid(), nullable=False),
        sa.Column("lead_project_session_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid()),
        sa.Column("run_id", sa.Uuid()),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("cadence_slot", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("lease_owner", sa.String(200)),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("metrics", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("narrative_summary", sa.Text()),
        sa.Column("error", postgresql.JSONB()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint("trigger IN ('manual', 'scheduled')", name="ck_supervision_trigger"),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_supervision_status",
        ),
        sa.CheckConstraint(
            "run_id IS NULL OR task_id IS NOT NULL", name="ck_supervision_run_requires_task"
        ),
        sa.CheckConstraint("revision > 0", name="ck_supervision_revision"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_supervision_project",
        ),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id", "run_id"],
            ["runs.tenant_id", "runs.task_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_supervision_run",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_supervision_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "trigger",
            "cadence_slot",
            name="uq_supervision_project_slot",
        ),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "idempotency_key", name="uq_supervision_idempotency"
        ),
    )
    op.create_index(
        "ix_supervision_due",
        "project_supervision_cycles",
        ["status", "scheduled_for", "lease_expires_at"],
    )

    op.create_table(
        "run_interventions",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("project_session_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("expected_run_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("consumed_by", sa.String(200)),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("rejection_reason", sa.Text()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "kind IN ('local_guidance', 'project_change')",
            name="ck_run_interventions_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'consumed', 'rejected', 'withdrawn')",
            name="ck_run_interventions_status",
        ),
        sa.CheckConstraint(
            "length(content) BETWEEN 1 AND 16000", name="ck_run_interventions_content"
        ),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_run_interventions_hash"),
        sa.CheckConstraint(
            "expected_run_revision > 0", name="ck_run_interventions_run_revision"
        ),
        sa.CheckConstraint("revision > 0", name="ck_run_interventions_revision"),
        sa.CheckConstraint(
            "status <> 'consumed' OR consumed_at IS NOT NULL",
            name="ck_run_interventions_consumed",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "project_session_id"],
            [
                "project_sessions.tenant_id",
                "project_sessions.project_id",
                "project_sessions.id",
            ],
            ondelete="RESTRICT",
            name="fk_run_interventions_session",
        ),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id", "run_id"],
            ["runs.tenant_id", "runs.task_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_run_interventions_run",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_run_interventions_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "run_id", "idempotency_key", name="uq_run_interventions_idempotency"
        ),
    )
    op.create_index(
        "ix_run_interventions_pending",
        "run_interventions",
        ["tenant_id", "run_id", "status", "created_at"],
    )

    op.execute(_GUARD_SUPERVISION_FUNCTION)
    op.execute(
        "CREATE TRIGGER guard_supervision_terminal BEFORE UPDATE ON project_supervision_cycles "
        "FOR EACH ROW EXECUTE FUNCTION guard_supervision_terminal()"
    )
    op.execute(_GUARD_INTERVENTION_FUNCTION)
    op.execute(
        "CREATE TRIGGER guard_intervention_terminal BEFORE UPDATE ON run_interventions "
        "FOR EACH ROW EXECUTE FUNCTION guard_intervention_terminal()"
    )

    for table in (
        "project_members",
        "project_sessions",
        "project_supervision_cycles",
        "run_interventions",
    ):
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO nico_runtime")
        _enable_rls(table)


def downgrade() -> None:
    op.execute("DROP TRIGGER guard_intervention_terminal ON run_interventions")
    op.execute("DROP FUNCTION guard_intervention_terminal()")
    op.execute("DROP TRIGGER guard_supervision_terminal ON project_supervision_cycles")
    op.execute("DROP FUNCTION guard_supervision_terminal()")
    op.drop_constraint("fk_tasks_project_session", "tasks", type_="foreignkey")
    op.drop_constraint(
        "fk_conversations_project_session", "conversations", type_="foreignkey"
    )
    op.drop_table("run_interventions")
    op.drop_table("project_supervision_cycles")
    op.drop_table("project_sessions")
    op.drop_table("project_members")
    op.drop_constraint("uq_tasks_project_session_id", "tasks", type_="unique")
    op.drop_column("tasks", "project_session_id")
    op.drop_constraint(
        "uq_conversations_project_agent_id", "conversations", type_="unique"
    )
    op.drop_column("conversations", "project_session_id")
    op.drop_index("uq_projects_personal_owner", table_name="projects")
    op.drop_constraint("ck_projects_supervision_cadence", "projects", type_="check")
    op.drop_constraint("ck_projects_kind_owner", "projects", type_="check")
    op.drop_constraint("ck_projects_kind", "projects", type_="check")
    op.drop_column("projects", "next_supervision_at")
    op.drop_column("projects", "supervision_cadence_seconds")
    op.drop_column("projects", "owner_actor_id")
    op.drop_column("projects", "kind")


_GUARD_SUPERVISION_FUNCTION = r"""
CREATE FUNCTION guard_supervision_terminal() RETURNS trigger AS $$
BEGIN
    IF OLD.status IN ('completed', 'failed', 'cancelled') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal project supervision cycle is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


_GUARD_INTERVENTION_FUNCTION = r"""
CREATE FUNCTION guard_intervention_terminal() RETURNS trigger AS $$
BEGIN
    IF OLD.status IN ('consumed', 'rejected', 'withdrawn') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'terminal run intervention is immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""
