"""Add run-scoped external tool provider bindings.

Revision ID: 20260727_0033
Revises: 20260724_0032
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260727_0033"
down_revision: str | None = "20260724_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")
UUID_DEFAULT = sa.text("gen_random_uuid()")
JSON_OBJECT = sa.text("'{}'::jsonb")
EMPTY_BINDING_SNAPSHOT = sa.text("""'{"schema_version":"1","bindings":[]}'::jsonb""")


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "tool_binding_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=EMPTY_BINDING_SNAPSHOT,
        ),
    )
    op.create_check_constraint(
        "ck_runs_tool_binding_snapshot_shape",
        "runs",
        "jsonb_typeof(tool_binding_snapshot) = 'object' "
        "AND tool_binding_snapshot ->> 'schema_version' = '1' "
        "AND jsonb_typeof(tool_binding_snapshot -> 'bindings') = 'array'",
    )
    op.create_check_constraint(
        "ck_runs_tool_binding_snapshot_size",
        "runs",
        "octet_length(tool_binding_snapshot::text) <= 1048576",
    )

    op.create_table(
        "external_tool_providers",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid()),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column(
            "protocol",
            sa.String(50),
            nullable=False,
            server_default="nico-tool-provider-v1",
        ),
        sa.Column("endpoint_url", sa.String(2000), nullable=False),
        sa.Column("endpoint_identity", sa.String(71), nullable=False),
        sa.Column("credential_ref", sa.String(300), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="registered"),
        sa.Column(
            "capability_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=JSON_OBJECT,
        ),
        sa.Column("capability_digest", sa.String(71)),
        sa.Column(
            "endpoint_policy",
            postgresql.JSONB(),
            nullable=False,
            server_default=JSON_OBJECT,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=JSON_OBJECT,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("disabled_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "status IN ('registered', 'verified', 'active', 'disabled', 'expired', 'revoked')",
            name="ck_external_tool_providers_status",
        ),
        sa.CheckConstraint(
            "protocol = 'nico-tool-provider-v1'",
            name="ck_external_tool_providers_protocol",
        ),
        sa.CheckConstraint(
            "name ~ '^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$'",
            name="ck_external_tool_providers_name",
        ),
        sa.CheckConstraint(
            "endpoint_identity ~ '^sha256:[0-9a-f]{64}$' "
            "AND (capability_digest IS NULL "
            "OR capability_digest ~ '^sha256:[0-9a-f]{64}$')",
            name="ck_external_tool_providers_digests",
        ),
        sa.CheckConstraint(
            "credential_ref ~ '^env:NICO_TOOL_SECRET_[A-Z0-9_]{1,100}$'",
            name="ck_external_tool_providers_credential_ref",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(capability_snapshot) = 'object' "
            "AND jsonb_typeof(endpoint_policy) = 'object' "
            "AND jsonb_typeof(metadata) = 'object'",
            name="ck_external_tool_providers_json_shapes",
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at",
            name="ck_external_tool_providers_expiry",
        ),
        sa.CheckConstraint(
            "(status NOT IN ('verified', 'active') "
            "OR (verified_at IS NOT NULL AND capability_digest IS NOT NULL)) "
            "AND (status <> 'active' OR activated_at IS NOT NULL)",
            name="ck_external_tool_providers_verification",
        ),
        sa.CheckConstraint("revision > 0", name="ck_external_tool_providers_revision"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_external_tool_providers_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_external_tool_providers_project",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_external_tool_providers_tenant_id_id",
        ),
    )
    op.create_index(
        "uq_external_tool_providers_tenant_name",
        "external_tool_providers",
        ["tenant_id", "name"],
        unique=True,
        postgresql_where=sa.text("project_id IS NULL"),
    )
    op.create_index(
        "uq_external_tool_providers_project_name",
        "external_tool_providers",
        ["tenant_id", "project_id", "name"],
        unique=True,
        postgresql_where=sa.text("project_id IS NOT NULL"),
    )
    op.create_index(
        "ix_external_tool_providers_tenant_status",
        "external_tool_providers",
        ["tenant_id", "status", "expires_at"],
    )

    op.create_table(
        "run_tool_bindings",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("tool_definition_id", sa.Uuid(), nullable=False),
        sa.Column("tool_name", sa.String(120), nullable=False),
        sa.Column("tool_version", sa.String(50), nullable=False),
        sa.Column("binding_digest", sa.String(71), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="created"),
        sa.Column("policy", postgresql.JSONB(), nullable=False),
        sa.Column("max_calls", sa.Integer(), nullable=False),
        sa.Column("max_total_duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("max_single_call_duration_ms", sa.Integer(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("call_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_duration_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("active_provider_request_id", sa.String(200)),
        sa.Column("cancel_status", sa.String(32), nullable=False, server_default="none"),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "status IN ('created', 'frozen', 'active', 'expiring', "
            "'expired', 'revoked', 'completed')",
            name="ck_run_tool_bindings_status",
        ),
        sa.CheckConstraint(
            "binding_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_run_tool_bindings_digest",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(policy) = 'object'",
            name="ck_run_tool_bindings_policy",
        ),
        sa.CheckConstraint(
            "max_calls BETWEEN 1 AND 100000 "
            "AND max_total_duration_ms BETWEEN 1 AND 86400000 "
            "AND max_single_call_duration_ms BETWEEN 1 AND 300000 "
            "AND max_single_call_duration_ms <= max_total_duration_ms "
            "AND max_retries BETWEEN 0 AND 4",
            name="ck_run_tool_bindings_budgets",
        ),
        sa.CheckConstraint(
            "call_count >= 0 AND call_count <= max_calls AND total_duration_ms >= 0",
            name="ck_run_tool_bindings_usage",
        ),
        sa.CheckConstraint(
            "cancel_status IN ('none', 'requested', 'cancelled', 'failed', 'unknown')",
            name="ck_run_tool_bindings_cancel_status",
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at",
            name="ck_run_tool_bindings_expiry",
        ),
        sa.CheckConstraint("revision > 0", name="ck_run_tool_bindings_revision"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_run_tool_bindings_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_run_tool_bindings_run",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "provider_id"],
            ["external_tool_providers.tenant_id", "external_tool_providers.id"],
            ondelete="RESTRICT",
            name="fk_run_tool_bindings_provider",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "tool_definition_id"],
            ["tool_definitions.tenant_id", "tool_definitions.id"],
            ondelete="RESTRICT",
            name="fk_run_tool_bindings_definition",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_run_tool_bindings_tenant_id_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "id",
            "provider_id",
            "tool_definition_id",
            name="uq_run_tool_bindings_call_scope",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "tool_definition_id",
            name="uq_run_tool_bindings_run_tool",
        ),
    )
    op.create_index(
        "ix_run_tool_bindings_tenant_run_status",
        "run_tool_bindings",
        ["tenant_id", "run_id", "status"],
    )
    op.create_index(
        "ix_run_tool_bindings_provider_status",
        "run_tool_bindings",
        ["tenant_id", "provider_id", "status"],
    )
    op.create_index(
        "ix_run_tool_bindings_active_request",
        "run_tool_bindings",
        ["tenant_id", "run_id", "active_provider_request_id"],
        postgresql_where=sa.text("active_provider_request_id IS NOT NULL"),
    )

    for column in (
        sa.Column("run_tool_binding_id", sa.Uuid()),
        sa.Column("provider_id", sa.Uuid()),
        sa.Column("provider_request_id", sa.String(200)),
        sa.Column("provider_execution_id", sa.String(300)),
        sa.Column("provider_request_digest", sa.String(71)),
        sa.Column("provider_deadline_at", sa.DateTime(timezone=True)),
        sa.Column("provider_trace_id", sa.String(128)),
        sa.Column("provider_status", sa.String(32)),
        sa.Column(
            "external_execution_may_continue",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    ):
        op.add_column("tool_calls", column)
    op.create_check_constraint(
        "ck_tool_calls_provider_binding_shape",
        "tool_calls",
        "(run_tool_binding_id IS NULL AND provider_id IS NULL) "
        "OR (run_tool_binding_id IS NOT NULL AND provider_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_tool_calls_provider_request_digest",
        "tool_calls",
        "provider_request_digest IS NULL OR provider_request_digest ~ '^sha256:[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "ck_tool_calls_provider_status",
        "tool_calls",
        "provider_status IS NULL OR provider_status IN "
        "('pending', 'accepted', 'running', 'succeeded', 'failed', "
        "'cancelled', 'timed_out', 'unknown')",
    )
    op.create_foreign_key(
        "fk_tool_calls_run_provider_binding",
        "tool_calls",
        "run_tool_bindings",
        [
            "tenant_id",
            "run_id",
            "run_tool_binding_id",
            "provider_id",
            "tool_definition_id",
        ],
        ["tenant_id", "run_id", "id", "provider_id", "tool_definition_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_tool_calls_provider_request",
        "tool_calls",
        ["tenant_id", "provider_id", "provider_request_id"],
        unique=True,
        postgresql_where=sa.text("provider_request_id IS NOT NULL"),
    )
    op.create_index(
        "ix_tool_calls_active_provider_request",
        "tool_calls",
        ["tenant_id", "run_id", "provider_status"],
        postgresql_where=sa.text("provider_status IN ('pending', 'accepted', 'running')"),
    )
    op.execute(_tool_call_provider_accounting_guard())

    op.execute(_run_snapshot_guard())
    op.execute(
        "CREATE TRIGGER guard_run_tool_binding_snapshot "
        "BEFORE UPDATE OF tool_binding_snapshot ON runs "
        "FOR EACH ROW EXECUTE FUNCTION guard_run_tool_binding_snapshot()"
    )
    op.execute(_provider_guard())
    op.execute(
        "CREATE TRIGGER guard_external_tool_provider_write "
        "BEFORE UPDATE OR DELETE ON external_tool_providers "
        "FOR EACH ROW EXECUTE FUNCTION guard_external_tool_provider_write()"
    )
    op.execute(_binding_guard())
    op.execute(
        "CREATE TRIGGER guard_run_tool_binding_write "
        "BEFORE INSERT OR UPDATE OR DELETE ON run_tool_bindings "
        "FOR EACH ROW EXECUTE FUNCTION guard_run_tool_binding_write()"
    )

    predicate = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    for table in ("external_tool_providers", "run_tool_bindings"):
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO nico_runtime")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS guard_run_tool_binding_write ON run_tool_bindings")
    op.execute("DROP FUNCTION IF EXISTS guard_run_tool_binding_write()")
    op.execute(
        "DROP TRIGGER IF EXISTS guard_external_tool_provider_write ON external_tool_providers"
    )
    op.execute("DROP FUNCTION IF EXISTS guard_external_tool_provider_write()")
    op.execute("DROP TRIGGER IF EXISTS guard_run_tool_binding_snapshot ON runs")
    op.execute("DROP FUNCTION IF EXISTS guard_run_tool_binding_snapshot()")
    op.execute(_legacy_tool_call_guard())

    op.drop_index("ix_tool_calls_active_provider_request", table_name="tool_calls")
    op.drop_index("uq_tool_calls_provider_request", table_name="tool_calls")
    op.drop_constraint(
        "fk_tool_calls_run_provider_binding",
        "tool_calls",
        type_="foreignkey",
    )
    op.drop_constraint("ck_tool_calls_provider_status", "tool_calls", type_="check")
    op.drop_constraint(
        "ck_tool_calls_provider_request_digest",
        "tool_calls",
        type_="check",
    )
    op.drop_constraint(
        "ck_tool_calls_provider_binding_shape",
        "tool_calls",
        type_="check",
    )
    for column in (
        "external_execution_may_continue",
        "provider_status",
        "provider_trace_id",
        "provider_deadline_at",
        "provider_request_digest",
        "provider_execution_id",
        "provider_request_id",
        "provider_id",
        "run_tool_binding_id",
    ):
        op.drop_column("tool_calls", column)

    op.drop_table("run_tool_bindings")
    op.drop_table("external_tool_providers")
    op.drop_constraint(
        "ck_runs_tool_binding_snapshot_size",
        "runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_runs_tool_binding_snapshot_shape",
        "runs",
        type_="check",
    )
    op.drop_column("runs", "tool_binding_snapshot")


def _run_snapshot_guard() -> str:
    return """
    CREATE FUNCTION guard_run_tool_binding_snapshot()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public
    AS $function$
    BEGIN
        IF NEW.tool_binding_snapshot IS DISTINCT FROM OLD.tool_binding_snapshot THEN
            RAISE EXCEPTION 'run tool binding snapshot is immutable';
        END IF;
        RETURN NEW;
    END;
    $function$
    """


def _tool_call_provider_accounting_guard() -> str:
    return """
    CREATE OR REPLACE FUNCTION guard_terminal_tool_call_update()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public
    AS $function$
    BEGIN
        IF OLD.status IN ('succeeded', 'failed', 'timed_out', 'cancelled') THEN
            IF OLD.status <> 'cancelled'
               OR OLD.provider_id IS NULL
               OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
               OR NEW.run_id IS DISTINCT FROM OLD.run_id
               OR NEW.run_step_id IS DISTINCT FROM OLD.run_step_id
               OR NEW.tool_definition_id IS DISTINCT FROM OLD.tool_definition_id
               OR NEW.run_tool_binding_id IS DISTINCT FROM OLD.run_tool_binding_id
               OR NEW.provider_id IS DISTINCT FROM OLD.provider_id
               OR NEW.tool_name IS DISTINCT FROM OLD.tool_name
               OR NEW.tool_version IS DISTINCT FROM OLD.tool_version
               OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
               OR NEW.arguments_hash IS DISTINCT FROM OLD.arguments_hash
               OR NEW.caller IS DISTINCT FROM OLD.caller
               OR NEW.execution_owner IS DISTINCT FROM OLD.execution_owner
               OR NEW.execution_lease_token IS DISTINCT FROM OLD.execution_lease_token
               OR NEW.provider_request_id IS DISTINCT FROM OLD.provider_request_id
               OR NEW.provider_request_digest IS DISTINCT FROM OLD.provider_request_digest
               OR NEW.provider_deadline_at IS DISTINCT FROM OLD.provider_deadline_at
               OR NEW.provider_trace_id IS DISTINCT FROM OLD.provider_trace_id
               OR (
                   OLD.provider_execution_id IS NOT NULL
                   AND NEW.provider_execution_id IS DISTINCT FROM OLD.provider_execution_id
               )
               OR NEW.arguments IS DISTINCT FROM OLD.arguments
               OR NEW.status IS DISTINCT FROM OLD.status
               OR NEW.attempts IS DISTINCT FROM OLD.attempts
               OR NEW.result IS DISTINCT FROM OLD.result
               OR NEW.error IS DISTINCT FROM OLD.error
               OR NEW.usage IS DISTINCT FROM OLD.usage
               OR NEW.started_at IS DISTINCT FROM OLD.started_at
               OR NEW.ended_at IS DISTINCT FROM OLD.ended_at
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR NEW.revision <> OLD.revision + 1 THEN
                RAISE EXCEPTION 'terminal tool calls are immutable'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END IF;
        RETURN NEW;
    END;
    $function$
    """


def _legacy_tool_call_guard() -> str:
    return """
    CREATE OR REPLACE FUNCTION guard_terminal_tool_call_update()
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


def _provider_guard() -> str:
    return """
    CREATE FUNCTION guard_external_tool_provider_write()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public
    AS $function$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'external tool providers cannot be deleted';
        END IF;

        IF NEW.tenant_id <> OLD.tenant_id
           OR NEW.project_id IS DISTINCT FROM OLD.project_id
           OR NEW.name <> OLD.name
           OR NEW.protocol <> OLD.protocol
           OR NEW.endpoint_url <> OLD.endpoint_url
           OR NEW.endpoint_identity <> OLD.endpoint_identity
           OR NEW.credential_ref <> OLD.credential_ref
           OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
           OR NEW.created_at <> OLD.created_at THEN
            RAISE EXCEPTION 'external tool provider identity is immutable';
        END IF;
        IF OLD.status <> 'registered'
           AND NEW.endpoint_policy IS DISTINCT FROM OLD.endpoint_policy THEN
            RAISE EXCEPTION 'verified external tool provider endpoint policy is immutable';
        END IF;
        IF OLD.status NOT IN ('registered', 'disabled')
           AND (
               NEW.capability_snapshot IS DISTINCT FROM OLD.capability_snapshot
               OR NEW.capability_digest IS DISTINCT FROM OLD.capability_digest
           ) THEN
            RAISE EXCEPTION 'verified external tool provider capability is immutable';
        END IF;
        IF OLD.status IN ('expired', 'revoked') THEN
            RAISE EXCEPTION 'terminal external tool provider is immutable';
        END IF;
        IF NEW.status <> OLD.status
           AND NOT (
               (
                   OLD.status = 'registered'
                   AND NEW.status IN ('verified', 'disabled', 'expired', 'revoked')
               )
               OR (
                   OLD.status = 'verified'
                   AND NEW.status IN ('active', 'disabled', 'expired', 'revoked')
               )
               OR (OLD.status = 'active' AND NEW.status IN ('disabled', 'expired', 'revoked'))
               OR (
                   OLD.status = 'disabled'
                   AND NEW.status IN ('verified', 'active', 'expired', 'revoked')
               )
           ) THEN
            RAISE EXCEPTION 'invalid external tool provider lifecycle transition';
        END IF;
        IF NEW.revision <> OLD.revision + 1 THEN
            RAISE EXCEPTION 'external tool provider revision must increment by one';
        END IF;
        RETURN NEW;
    END;
    $function$
    """


def _binding_guard() -> str:
    return """
    CREATE FUNCTION guard_run_tool_binding_write()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public
    AS $function$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'run tool bindings cannot be deleted';
        END IF;
        IF TG_OP = 'INSERT' THEN
            IF NEW.status NOT IN ('created', 'frozen')
               OR NEW.revision <> 1
               OR NEW.call_count <> 0
               OR NEW.total_duration_ms <> 0
               OR NEW.active_provider_request_id IS NOT NULL
               OR NEW.cancel_status <> 'none' THEN
                RAISE EXCEPTION 'invalid initial run tool binding state';
            END IF;
            RETURN NEW;
        END IF;

        IF NEW.tenant_id <> OLD.tenant_id
           OR NEW.project_id <> OLD.project_id
           OR NEW.run_id <> OLD.run_id
           OR NEW.provider_id <> OLD.provider_id
           OR NEW.tool_definition_id <> OLD.tool_definition_id
           OR NEW.tool_name <> OLD.tool_name
           OR NEW.tool_version <> OLD.tool_version
           OR NEW.binding_digest <> OLD.binding_digest
           OR NEW.policy IS DISTINCT FROM OLD.policy
           OR NEW.max_calls <> OLD.max_calls
           OR NEW.max_total_duration_ms <> OLD.max_total_duration_ms
           OR NEW.max_single_call_duration_ms <> OLD.max_single_call_duration_ms
           OR NEW.max_retries <> OLD.max_retries
           OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
           OR NEW.created_at <> OLD.created_at THEN
            RAISE EXCEPTION 'run tool binding policy is immutable';
        END IF;
        IF OLD.status IN ('expired', 'revoked', 'completed') THEN
            IF NEW.status <> OLD.status
               OR NEW.call_count <> OLD.call_count
               OR NEW.total_duration_ms <> OLD.total_duration_ms
               OR NEW.completed_at IS DISTINCT FROM OLD.completed_at THEN
                RAISE EXCEPTION 'terminal run tool binding is immutable';
            END IF;
        END IF;
        IF NEW.call_count < OLD.call_count
           OR NEW.total_duration_ms < OLD.total_duration_ms THEN
            RAISE EXCEPTION 'run tool binding counters cannot decrease';
        END IF;
        IF NEW.status <> OLD.status
           AND NOT (
               (OLD.status = 'created' AND NEW.status IN ('frozen', 'revoked'))
               OR (
                   OLD.status = 'frozen'
                   AND NEW.status IN ('active', 'expired', 'revoked', 'completed')
               )
               OR (
                   OLD.status = 'active'
                   AND NEW.status IN ('expiring', 'expired', 'revoked', 'completed')
               )
               OR (OLD.status = 'expiring' AND NEW.status IN ('expired', 'revoked', 'completed'))
           ) THEN
            RAISE EXCEPTION 'invalid run tool binding lifecycle transition';
        END IF;
        IF OLD.cancel_status = 'cancelled'
           AND NEW.cancel_status <> OLD.cancel_status THEN
            RAISE EXCEPTION 'confirmed provider cancellation is immutable';
        END IF;
        IF NEW.revision <> OLD.revision + 1 THEN
            RAISE EXCEPTION 'run tool binding revision must increment by one';
        END IF;
        RETURN NEW;
    END;
    $function$
    """
