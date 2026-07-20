"""Add the native model runtime persistence baseline.

Revision ID: 20260718_0010
Revises: 20260717_0009
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0010"
down_revision: str | None = "20260717_0009"
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
    op.create_table(
        "model_endpoints",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("stable_key", sa.String(120), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("protocol", sa.String(50), nullable=False, server_default="openai_compatible"),
        sa.Column("base_url", sa.String(2000), nullable=False),
        sa.Column("credential_ref", sa.String(300), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("allowed_models", postgresql.JSONB(), nullable=False, server_default=JSON_ARRAY),
        sa.Column("capabilities", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("rate_limit", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("tls_policy", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_model_endpoints_status"),
        sa.CheckConstraint("protocol IN ('openai_compatible')", name="ck_model_endpoints_protocol"),
        sa.CheckConstraint("revision > 0", name="ck_model_endpoints_revision"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="RESTRICT", name="fk_model_endpoints_tenant"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_model_endpoints_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "stable_key", "revision", name="uq_model_endpoints_key_revision"
        ),
    )
    op.create_index(
        "ix_model_endpoints_tenant_status",
        "model_endpoints",
        ["tenant_id", "status", "stable_key"],
    )

    for column in (
        sa.Column("runtime_provider", sa.String(100)),
        sa.Column("execution_mode", sa.String(32)),
        sa.Column("model_endpoint_id", sa.Uuid()),
        sa.Column("model_name", sa.String(200)),
    ):
        op.add_column("agent_versions", column)
    op.create_check_constraint(
        "ck_agent_versions_execution_mode",
        "agent_versions",
        "execution_mode IS NULL OR execution_mode IN ('direct', 'react', 'plan_and_execute')",
    )
    op.create_foreign_key(
        "fk_agent_versions_model_endpoint",
        "agent_versions",
        "model_endpoints",
        ["tenant_id", "model_endpoint_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )

    op.drop_constraint("ck_runtime_sessions_status", "runtime_sessions", type_="check")
    op.create_check_constraint(
        "ck_runtime_sessions_status",
        "runtime_sessions",
        "status IN ('created', 'running', 'paused', 'suspended', "
        "'completed', 'failed', 'cancelled')",
    )
    op.add_column(
        "runtime_sessions",
        sa.Column("execution_mode", sa.String(32), nullable=False, server_default="direct"),
    )
    op.add_column(
        "runtime_sessions",
        sa.Column("loop_state", sa.String(32), nullable=False, server_default="initializing"),
    )
    op.add_column(
        "runtime_sessions",
        sa.Column(
            "execution_manifest", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT
        ),
    )
    op.add_column("runtime_sessions", sa.Column("model_endpoint_snapshot", postgresql.JSONB()))
    op.add_column("runtime_sessions", sa.Column("current_context_snapshot_id", sa.Uuid()))
    op.add_column("runtime_sessions", sa.Column("last_model_call_id", sa.Uuid()))
    op.create_check_constraint(
        "ck_runtime_sessions_execution_mode",
        "runtime_sessions",
        "execution_mode IN ('direct', 'react', 'plan_and_execute')",
    )

    op.create_table(
        "context_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("runtime_session_id", sa.Uuid(), nullable=False),
        sa.Column("parent_snapshot_id", sa.Uuid()),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(100), nullable=False),
        sa.Column("source_refs", postgresql.JSONB(), nullable=False, server_default=JSON_ARRAY),
        sa.Column(
            "rendered_messages", postgresql.JSONB(), nullable=False, server_default=JSON_ARRAY
        ),
        sa.Column("token_estimate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truncation", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint("schema_version > 0", name="ck_context_snapshots_schema_version"),
        sa.CheckConstraint("version > 0", name="ck_context_snapshots_version"),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_context_snapshots_hash"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_context_snapshots_tenant_run",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "runtime_session_id"],
            ["runtime_sessions.tenant_id", "runtime_sessions.run_id", "runtime_sessions.id"],
            ondelete="RESTRICT",
            name="fk_context_snapshots_runtime_session",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "parent_snapshot_id"],
            ["context_snapshots.tenant_id", "context_snapshots.run_id", "context_snapshots.id"],
            ondelete="RESTRICT",
            name="fk_context_snapshots_parent",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_context_snapshots_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "run_id", "id", name="uq_context_snapshots_tenant_run_id"),
        sa.UniqueConstraint(
            "tenant_id", "run_id", "version", name="uq_context_snapshots_run_version"
        ),
    )
    op.create_index(
        "ix_context_snapshots_run_created",
        "context_snapshots",
        ["tenant_id", "run_id", "created_at"],
    )

    op.create_table(
        "model_calls",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("runtime_session_id", sa.Uuid(), nullable=False),
        sa.Column("context_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("model_endpoint_id", sa.Uuid()),
        sa.Column("call_key", sa.String(200), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column(
            "request_redacted", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT
        ),
        sa.Column("response_redacted", postgresql.JSONB()),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_hash", sa.String(64)),
        sa.Column("provider_request_id", sa.String(300)),
        sa.Column("usage", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("usage_status", sa.String(32), nullable=False, server_default="missing"),
        sa.Column("cost", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("cost_status", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("pricing_revision", sa.String(100)),
        sa.Column("error", postgresql.JSONB()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'streaming', 'completed', 'failed', 'interrupted', 'cancelled')",
            name="ck_model_calls_status",
        ),
        sa.CheckConstraint(
            "usage_status IN ('missing', 'partial', 'exact')",
            name="ck_model_calls_usage_status",
        ),
        sa.CheckConstraint(
            "cost_status IN ('unknown', 'estimated', 'exact')",
            name="ck_model_calls_cost_status",
        ),
        sa.CheckConstraint("length(request_hash) = 64", name="ck_model_calls_request_hash"),
        sa.CheckConstraint(
            "response_hash IS NULL OR length(response_hash) = 64",
            name="ck_model_calls_response_hash",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_model_calls_tenant_run",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "runtime_session_id"],
            ["runtime_sessions.tenant_id", "runtime_sessions.run_id", "runtime_sessions.id"],
            ondelete="RESTRICT",
            name="fk_model_calls_runtime_session",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "context_snapshot_id"],
            ["context_snapshots.tenant_id", "context_snapshots.run_id", "context_snapshots.id"],
            ondelete="RESTRICT",
            name="fk_model_calls_context_snapshot",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "model_endpoint_id"],
            ["model_endpoints.tenant_id", "model_endpoints.id"],
            ondelete="RESTRICT",
            name="fk_model_calls_endpoint",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_model_calls_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "run_id", "id", name="uq_model_calls_tenant_run_id"),
        sa.UniqueConstraint("tenant_id", "run_id", "call_key", name="uq_model_calls_run_key"),
    )
    op.create_index(
        "ix_model_calls_run_created", "model_calls", ["tenant_id", "run_id", "created_at"]
    )

    op.create_foreign_key(
        "fk_runtime_sessions_current_context",
        "runtime_sessions",
        "context_snapshots",
        ["tenant_id", "run_id", "current_context_snapshot_id"],
        ["tenant_id", "run_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_runtime_sessions_last_model_call",
        "runtime_sessions",
        "model_calls",
        ["tenant_id", "run_id", "last_model_call_id"],
        ["tenant_id", "run_id", "id"],
        ondelete="RESTRICT",
    )

    for statement in (
        _REJECT_FACT_CHANGE_FUNCTION,
        _GUARD_TERMINAL_MODEL_CALL_FUNCTION,
        _GUARD_MODEL_ENDPOINT_FUNCTION,
    ):
        op.execute(statement)
    op.execute(
        "CREATE TRIGGER guard_context_snapshot_immutable BEFORE UPDATE OR DELETE "
        "ON context_snapshots FOR EACH ROW EXECUTE FUNCTION reject_native_runtime_fact_change()"
    )
    op.execute(
        "CREATE TRIGGER guard_model_call_terminal BEFORE UPDATE OR DELETE ON model_calls "
        "FOR EACH ROW EXECUTE FUNCTION guard_terminal_model_call()"
    )
    op.execute(
        "CREATE TRIGGER guard_model_endpoint_semantics BEFORE UPDATE ON model_endpoints "
        "FOR EACH ROW EXECUTE FUNCTION guard_model_endpoint_semantics()"
    )

    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON model_endpoints, context_snapshots, "
        "model_calls TO nico_runtime"
    )
    for table in ("model_endpoints", "context_snapshots", "model_calls"):
        _enable_rls(table)


def downgrade() -> None:
    op.execute("DROP TRIGGER guard_model_endpoint_semantics ON model_endpoints")
    op.execute("DROP FUNCTION guard_model_endpoint_semantics()")
    op.execute("DROP TRIGGER guard_model_call_terminal ON model_calls")
    op.execute("DROP FUNCTION guard_terminal_model_call()")
    op.execute("DROP TRIGGER guard_context_snapshot_immutable ON context_snapshots")
    op.execute("DROP FUNCTION reject_native_runtime_fact_change()")
    op.drop_constraint(
        "fk_runtime_sessions_last_model_call", "runtime_sessions", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_runtime_sessions_current_context", "runtime_sessions", type_="foreignkey"
    )
    op.drop_table("model_calls")
    op.drop_table("context_snapshots")
    op.drop_constraint("ck_runtime_sessions_execution_mode", "runtime_sessions", type_="check")
    for column in (
        "last_model_call_id",
        "current_context_snapshot_id",
        "model_endpoint_snapshot",
        "execution_manifest",
        "loop_state",
        "execution_mode",
    ):
        op.drop_column("runtime_sessions", column)
    op.drop_constraint("ck_runtime_sessions_status", "runtime_sessions", type_="check")
    op.execute(
        "UPDATE runtime_sessions SET status = 'paused', revision = revision + 1 "
        "WHERE status = 'suspended'"
    )
    op.create_check_constraint(
        "ck_runtime_sessions_status",
        "runtime_sessions",
        "status IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')",
    )
    op.drop_constraint("fk_agent_versions_model_endpoint", "agent_versions", type_="foreignkey")
    op.drop_constraint("ck_agent_versions_execution_mode", "agent_versions", type_="check")
    for column in ("model_name", "model_endpoint_id", "execution_mode", "runtime_provider"):
        op.drop_column("agent_versions", column)
    op.drop_table("model_endpoints")


_REJECT_FACT_CHANGE_FUNCTION = r"""
CREATE FUNCTION reject_native_runtime_fact_change()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
BEGIN
    RAISE EXCEPTION 'native runtime fact is immutable'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$function$;
"""

_GUARD_TERMINAL_MODEL_CALL_FUNCTION = r"""
CREATE FUNCTION guard_terminal_model_call()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
BEGIN
    IF OLD.status IN ('completed', 'failed', 'interrupted', 'cancelled') THEN
        RAISE EXCEPTION 'terminal model call is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$;
"""

_GUARD_MODEL_ENDPOINT_FUNCTION = r"""
CREATE FUNCTION guard_model_endpoint_semantics()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.agent_versions v
        WHERE v.tenant_id = OLD.tenant_id AND v.model_endpoint_id = OLD.id
    ) AND (
        OLD.stable_key IS DISTINCT FROM NEW.stable_key
        OR OLD.revision IS DISTINCT FROM NEW.revision
        OR OLD.protocol IS DISTINCT FROM NEW.protocol
        OR OLD.base_url IS DISTINCT FROM NEW.base_url
        OR OLD.allowed_models IS DISTINCT FROM NEW.allowed_models
        OR OLD.tls_policy IS DISTINCT FROM NEW.tls_policy
    ) THEN
        RAISE EXCEPTION 'referenced model endpoint semantics are immutable; create a revision'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$;
"""
