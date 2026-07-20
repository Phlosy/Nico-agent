"""Add durable conversations, turns, and conversation context references.

Revision ID: 20260719_0017
Revises: 20260718_0016
Create Date: 2026-07-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260719_0017"
down_revision: str | None = "20260718_0016"
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
    op.execute(
        f'CREATE POLICY tenant_isolation ON "{table}" USING ({predicate}) WITH CHECK ({predicate})'
    )
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')


def upgrade() -> None:
    op.create_unique_constraint("uq_runs_tenant_task_id", "runs", ["tenant_id", "task_id", "id"])
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("summary", sa.Text()),
        sa.Column("summary_through_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary_input_hash", sa.String(64)),
        sa.Column("summary_model_call_id", sa.Uuid()),
        sa.Column("last_turn_id", sa.Uuid()),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint("status IN ('active', 'archived')", name="ck_conversations_status"),
        sa.CheckConstraint(
            "summary_through_sequence >= 0", name="ck_conversations_summary_sequence"
        ),
        sa.CheckConstraint(
            "summary_input_hash IS NULL OR length(summary_input_hash) = 64",
            name="ck_conversations_summary_hash",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_conversations_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="RESTRICT",
            name="fk_conversations_agent",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_id", "agent_version_id"],
            ["agent_versions.tenant_id", "agent_versions.agent_id", "agent_versions.id"],
            ondelete="RESTRICT",
            name="fk_conversations_agent_version",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "summary_model_call_id"],
            ["model_calls.tenant_id", "model_calls.id"],
            ondelete="RESTRICT",
            name="fk_conversations_summary_model_call",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_conversations_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_conversations_idempotency"),
    )
    op.create_index(
        "ix_conversations_recent",
        "conversations",
        ["tenant_id", "status", "updated_at"],
    )

    op.create_table(
        "conversation_turns",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("user_input", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("assistant_output", postgresql.JSONB()),
        sa.Column("artifact_refs", postgresql.JSONB(), nullable=False, server_default=JSON_ARRAY),
        sa.Column("usage", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("error", postgresql.JSONB()),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint("sequence > 0", name="ck_conversation_turns_sequence"),
        sa.CheckConstraint(
            "status IN ('accepted', 'queued', 'running', 'waiting_for_approval', "
            "'completed', 'failed', 'cancelled')",
            name="ck_conversation_turns_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            ondelete="RESTRICT",
            name="fk_conversation_turns_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.id"],
            ondelete="RESTRICT",
            name="fk_conversation_turns_task",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id", "run_id"],
            ["runs.tenant_id", "runs.task_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_conversation_turns_current_run",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_conversation_turns_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "task_id", name="uq_conversation_turns_task"),
        sa.UniqueConstraint("tenant_id", "run_id", name="uq_conversation_turns_current_run"),
        sa.UniqueConstraint(
            "tenant_id", "conversation_id", "id", name="uq_conversation_turns_scope_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "conversation_id",
            "sequence",
            name="uq_conversation_turns_sequence",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "conversation_id",
            "idempotency_key",
            name="uq_conversation_turns_idempotency",
        ),
    )
    op.create_index(
        "ix_conversation_turns_history",
        "conversation_turns",
        ["tenant_id", "conversation_id", "sequence"],
    )
    op.create_foreign_key(
        "fk_conversations_last_turn",
        "conversations",
        "conversation_turns",
        ["tenant_id", "id", "last_turn_id"],
        ["tenant_id", "conversation_id", "id"],
        ondelete="RESTRICT",
    )

    op.add_column("context_snapshots", sa.Column("conversation_id", sa.Uuid()))
    op.add_column("context_snapshots", sa.Column("conversation_turn_id", sa.Uuid()))
    op.add_column(
        "context_snapshots",
        sa.Column(
            "selected_turn_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=JSON_ARRAY,
        ),
    )
    op.add_column("context_snapshots", sa.Column("conversation_summary_hash", sa.String(64)))
    op.add_column(
        "context_snapshots",
        sa.Column("artifact_refs", postgresql.JSONB(), nullable=False, server_default=JSON_ARRAY),
    )
    op.add_column("context_snapshots", sa.Column("token_budget", sa.Integer()))
    op.create_foreign_key(
        "fk_context_snapshots_conversation",
        "context_snapshots",
        "conversations",
        ["tenant_id", "conversation_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_context_snapshots_conversation_turn",
        "context_snapshots",
        "conversation_turns",
        ["tenant_id", "conversation_id", "conversation_turn_id"],
        ["tenant_id", "conversation_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_context_snapshots_conversation_ref",
        "context_snapshots",
        "conversation_turn_id IS NULL OR conversation_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_context_snapshots_conversation_summary_hash",
        "context_snapshots",
        "conversation_summary_hash IS NULL OR length(conversation_summary_hash) = 64",
    )
    op.create_check_constraint(
        "ck_context_snapshots_token_budget",
        "context_snapshots",
        "token_budget IS NULL OR token_budget > 0",
    )

    op.execute(_GUARD_CONVERSATION_FUNCTION)
    op.execute(
        "CREATE TRIGGER guard_conversation_identity BEFORE UPDATE ON conversations "
        "FOR EACH ROW EXECUTE FUNCTION guard_conversation_identity()"
    )
    op.execute(_GUARD_TURN_FUNCTION)
    op.execute(
        "CREATE TRIGGER guard_conversation_turn_identity BEFORE UPDATE ON conversation_turns "
        "FOR EACH ROW EXECUTE FUNCTION guard_conversation_turn_identity()"
    )
    op.execute(_PROJECT_TURN_FUNCTION)
    op.execute(
        "CREATE TRIGGER project_conversation_turn_after_run "
        "AFTER UPDATE OF status, result, error, cost ON runs "
        "FOR EACH ROW EXECUTE FUNCTION project_conversation_turn_from_run()"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON conversations, conversation_turns TO nico_runtime")
    for table in ("conversations", "conversation_turns"):
        _enable_rls(table)


def downgrade() -> None:
    op.execute("DROP TRIGGER project_conversation_turn_after_run ON runs")
    op.execute("DROP FUNCTION project_conversation_turn_from_run()")
    op.execute("DROP TRIGGER guard_conversation_turn_identity ON conversation_turns")
    op.execute("DROP FUNCTION guard_conversation_turn_identity()")
    op.execute("DROP TRIGGER guard_conversation_identity ON conversations")
    op.execute("DROP FUNCTION guard_conversation_identity()")
    op.drop_constraint("ck_context_snapshots_token_budget", "context_snapshots", type_="check")
    op.drop_constraint(
        "ck_context_snapshots_conversation_summary_hash",
        "context_snapshots",
        type_="check",
    )
    op.drop_constraint("ck_context_snapshots_conversation_ref", "context_snapshots", type_="check")
    op.drop_constraint(
        "fk_context_snapshots_conversation_turn", "context_snapshots", type_="foreignkey"
    )
    op.drop_constraint("fk_context_snapshots_conversation", "context_snapshots", type_="foreignkey")
    for column in (
        "token_budget",
        "artifact_refs",
        "conversation_summary_hash",
        "selected_turn_ids",
        "conversation_turn_id",
        "conversation_id",
    ):
        op.drop_column("context_snapshots", column)
    op.drop_constraint("fk_conversations_last_turn", "conversations", type_="foreignkey")
    op.drop_table("conversation_turns")
    op.drop_table("conversations")
    op.drop_constraint("uq_runs_tenant_task_id", "runs", type_="unique")


_GUARD_CONVERSATION_FUNCTION = r"""
CREATE FUNCTION guard_conversation_identity()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
BEGIN
    IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.project_id IS DISTINCT FROM NEW.project_id
       OR OLD.agent_id IS DISTINCT FROM NEW.agent_id
       OR OLD.agent_version_id IS DISTINCT FROM NEW.agent_version_id
       OR OLD.created_by IS DISTINCT FROM NEW.created_by
       OR OLD.idempotency_key IS DISTINCT FROM NEW.idempotency_key THEN
        RAISE EXCEPTION 'conversation identity is immutable';
    END IF;
    RETURN NEW;
END;
$function$;
"""


_GUARD_TURN_FUNCTION = r"""
CREATE FUNCTION guard_conversation_turn_identity()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
BEGIN
    IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.conversation_id IS DISTINCT FROM NEW.conversation_id
       OR OLD.sequence IS DISTINCT FROM NEW.sequence
       OR OLD.user_input IS DISTINCT FROM NEW.user_input
       OR OLD.task_id IS DISTINCT FROM NEW.task_id
       OR OLD.idempotency_key IS DISTINCT FROM NEW.idempotency_key THEN
        RAISE EXCEPTION 'conversation turn identity is immutable';
    END IF;
    RETURN NEW;
END;
$function$;
"""


_PROJECT_TURN_FUNCTION = r"""
CREATE FUNCTION project_conversation_turn_from_run()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
DECLARE
    projected_status text;
BEGIN
    projected_status := CASE NEW.status
        WHEN 'pending' THEN 'queued'
        WHEN 'waiting_for_approval' THEN 'waiting_for_approval'
        WHEN 'completed' THEN 'completed'
        WHEN 'failed' THEN 'failed'
        WHEN 'timed_out' THEN 'failed'
        WHEN 'cancelled' THEN 'cancelled'
        ELSE 'running'
    END;
    UPDATE conversation_turns
       SET status = projected_status,
           assistant_output = NEW.result,
           usage = COALESCE(NEW.cost, '{}'::jsonb),
           error = NEW.error,
           revision = revision + 1,
           updated_at = now()
     WHERE tenant_id = NEW.tenant_id
       AND run_id = NEW.id
       AND (status, assistant_output, usage, error)
           IS DISTINCT FROM
           (projected_status, NEW.result, COALESCE(NEW.cost, '{}'::jsonb), NEW.error);
    RETURN NEW;
END;
$function$;
"""
