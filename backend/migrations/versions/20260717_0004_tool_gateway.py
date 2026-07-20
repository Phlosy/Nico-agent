"""Add versioned tools and immutable ToolCall execution records.

Revision ID: 20260717_0004
Revises: 20260717_0003
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260717_0004"
down_revision: str | None = "20260717_0003"
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
    op.create_unique_constraint(
        "uq_run_steps_tenant_run_id", "run_steps", ["tenant_id", "run_id", "id"]
    )
    op.create_table(
        "tool_definitions",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("input_schema", postgresql.JSONB(), nullable=False),
        sa.Column("output_schema", postgresql.JSONB(), nullable=False),
        sa.Column("permission", sa.String(150), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("retry_policy", postgresql.JSONB(), nullable=False),
        sa.Column("isolation_policy", postgresql.JSONB(), nullable=False),
        sa.Column("risk", sa.String(20), nullable=False, server_default="low"),
        sa.Column("max_output_bytes", sa.Integer(), nullable=False),
        sa.Column("implementation_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(200), nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True)),
        sa.Column("disabled_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('draft', 'enabled', 'disabled')",
            name="ck_tool_definitions_status",
        ),
        sa.CheckConstraint("risk IN ('low', 'medium', 'high')", name="ck_tool_definitions_risk"),
        sa.CheckConstraint("timeout_seconds > 0", name="ck_tool_definitions_timeout"),
        sa.CheckConstraint("max_output_bytes > 0", name="ck_tool_definitions_output_limit"),
        sa.CheckConstraint(
            "length(implementation_hash) = 64 AND length(content_hash) = 64",
            name="ck_tool_definitions_hashes",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tool_definitions_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "name",
            "version",
            name="uq_tool_definitions_tenant_name_version",
        ),
    )
    op.create_index(
        "ix_tool_definitions_tenant_status_name",
        "tool_definitions",
        ["tenant_id", "status", "name"],
    )

    op.create_table(
        "tool_calls",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("run_step_id", sa.Uuid(), nullable=False),
        sa.Column("tool_definition_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(120), nullable=False),
        sa.Column("tool_version", sa.String(50), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("caller", sa.String(200), nullable=False),
        sa.Column("arguments", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("attempts", postgresql.JSONB(), nullable=False, server_default=JSON_ARRAY),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("error", postgresql.JSONB()),
        sa.Column("usage", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'timed_out', 'cancelled')",
            name="ck_tool_calls_status",
        ),
        sa.CheckConstraint("length(arguments_hash) = 64", name="ck_tool_calls_arguments_hash"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_tool_calls_tenant_run",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "run_step_id"],
            ["run_steps.tenant_id", "run_steps.run_id", "run_steps.id"],
            ondelete="RESTRICT",
            name="fk_tool_calls_tenant_run_step",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "tool_definition_id"],
            ["tool_definitions.tenant_id", "tool_definitions.id"],
            ondelete="RESTRICT",
            name="fk_tool_calls_tenant_definition",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tool_calls_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "tool_definition_id",
            "idempotency_key",
            name="uq_tool_calls_run_tool_idempotency",
        ),
    )
    op.create_index(
        "ix_tool_calls_tenant_run_created",
        "tool_calls",
        ["tenant_id", "run_id", "created_at"],
    )
    op.create_index(
        "ix_tool_calls_tenant_status",
        "tool_calls",
        ["tenant_id", "status", "created_at"],
    )

    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON tool_definitions, tool_calls TO nico_runtime"
    )
    _enable_rls("tool_definitions")
    _enable_rls("tool_calls")
    op.execute(
        """
        CREATE FUNCTION guard_tool_definition_update()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $function$
        BEGIN
            IF OLD.status <> 'draft' AND (
                NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR
                NEW.name IS DISTINCT FROM OLD.name OR
                NEW.version IS DISTINCT FROM OLD.version OR
                NEW.description IS DISTINCT FROM OLD.description OR
                NEW.input_schema IS DISTINCT FROM OLD.input_schema OR
                NEW.output_schema IS DISTINCT FROM OLD.output_schema OR
                NEW.permission IS DISTINCT FROM OLD.permission OR
                NEW.timeout_seconds IS DISTINCT FROM OLD.timeout_seconds OR
                NEW.retry_policy IS DISTINCT FROM OLD.retry_policy OR
                NEW.isolation_policy IS DISTINCT FROM OLD.isolation_policy OR
                NEW.risk IS DISTINCT FROM OLD.risk OR
                NEW.max_output_bytes IS DISTINCT FROM OLD.max_output_bytes OR
                NEW.implementation_hash IS DISTINCT FROM OLD.implementation_hash OR
                NEW.content_hash IS DISTINCT FROM OLD.content_hash OR
                NEW.created_by IS DISTINCT FROM OLD.created_by
            ) THEN
                RAISE EXCEPTION 'enabled tool definitions are immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        "CREATE TRIGGER guard_tool_definition_update "
        "BEFORE UPDATE ON tool_definitions FOR EACH ROW "
        "EXECUTE FUNCTION guard_tool_definition_update()"
    )
    op.execute(
        """
        CREATE FUNCTION guard_terminal_tool_call_update()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $function$
        BEGIN
            IF OLD.status IN ('succeeded', 'failed', 'timed_out', 'cancelled') THEN
                RAISE EXCEPTION 'terminal tool calls are immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        "CREATE TRIGGER guard_terminal_tool_call_update "
        "BEFORE UPDATE ON tool_calls FOR EACH ROW "
        "EXECUTE FUNCTION guard_terminal_tool_call_update()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER guard_terminal_tool_call_update ON tool_calls")
    op.execute("DROP FUNCTION guard_terminal_tool_call_update()")
    op.execute("DROP TRIGGER guard_tool_definition_update ON tool_definitions")
    op.execute("DROP FUNCTION guard_tool_definition_update()")
    op.drop_table("tool_calls")
    op.drop_table("tool_definitions")
    op.drop_constraint("uq_run_steps_tenant_run_id", "run_steps", type_="unique")
