"""Add RuntimeSession persistence and the least-privilege Run claimer.

Revision ID: 20260717_0003
Revises: 20260717_0002
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260717_0003"
down_revision: str | None = "20260717_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")
UUID_DEFAULT = sa.text("gen_random_uuid()")
JSON_OBJECT = sa.text("'{}'::jsonb")
JSON_ARRAY = sa.text("'[]'::jsonb")


def upgrade() -> None:
    op.create_table(
        "runtime_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("provider_name", sa.String(100), nullable=False),
        sa.Column("provider_version", sa.String(100), nullable=False),
        sa.Column("protocol_version", sa.String(32), nullable=False),
        sa.Column("external_session_id", sa.String(500)),
        sa.Column("status", sa.String(32), nullable=False, server_default="created"),
        sa.Column("capabilities", postgresql.JSONB(), nullable=False, server_default=JSON_ARRAY),
        sa.Column("provider_state", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("checkpoint", postgresql.JSONB()),
        sa.Column("usage", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("trajectory", postgresql.JSONB()),
        sa.Column("last_event_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "status IN ('created', 'running', 'paused', 'completed', 'failed', 'cancelled')",
            name="ck_runtime_sessions_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_runtime_sessions_tenant_run",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_runtime_sessions_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "run_id", name="uq_runtime_sessions_tenant_run"),
    )
    op.create_index(
        "ix_runtime_sessions_tenant_status",
        "runtime_sessions",
        ["tenant_id", "status", "updated_at"],
    )

    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nico_worker_claimer') THEN "
        "CREATE ROLE nico_worker_claimer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOINHERIT NOREPLICATION NOBYPASSRLS; END IF; END $$"
    )
    op.execute("GRANT nico_worker_claimer TO CURRENT_USER")
    op.execute("GRANT USAGE ON SCHEMA public TO nico_worker_claimer")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON runtime_sessions TO nico_runtime")
    op.execute(
        "CREATE POLICY tenant_isolation ON runtime_sessions "
        "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
        "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute("ALTER TABLE runtime_sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE runtime_sessions FORCE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE FUNCTION claim_next_run(p_worker_id text, p_lease_seconds integer)
        RETURNS TABLE(run_id uuid, tenant_id uuid, lease_token uuid, previous_status text)
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
        DECLARE
            claimed_at timestamptz := clock_timestamp();
        BEGIN
            IF p_worker_id IS NULL OR length(p_worker_id) < 1 OR length(p_worker_id) > 200 THEN
                RAISE EXCEPTION 'worker_id must contain between 1 and 200 characters';
            END IF;
            IF p_lease_seconds < 5 OR p_lease_seconds > 3600 THEN
                RAISE EXCEPTION 'lease_seconds must be between 5 and 3600';
            END IF;

            RETURN QUERY
            WITH candidate AS (
                SELECT r.id
                FROM public.runs AS r
                JOIN public.tasks AS t
                  ON t.tenant_id = r.tenant_id AND t.id = r.task_id
                WHERE r.status IN ('pending', 'planning', 'running')
                  AND (r.lease_expires_at IS NULL OR r.lease_expires_at <= claimed_at)
                ORDER BY t.priority DESC, r.created_at, r.id
                FOR UPDATE OF r SKIP LOCKED
                LIMIT 1
            ), claimed AS (
                UPDATE public.runs AS r
                SET lease_owner = p_worker_id,
                    lease_token = gen_random_uuid(),
                    lease_expires_at = claimed_at + make_interval(secs => p_lease_seconds),
                    heartbeat_at = claimed_at,
                    updated_at = claimed_at
                FROM candidate AS c
                WHERE r.id = c.id
                RETURNING r.id, r.tenant_id, r.lease_token, r.status
            )
            SELECT c.id, c.tenant_id, c.lease_token, c.status::text
            FROM claimed AS c;
        END;
        $function$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION claim_next_run(text, integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION claim_next_run(text, integer) TO nico_worker_claimer")


def downgrade() -> None:
    op.execute("REVOKE EXECUTE ON FUNCTION claim_next_run(text, integer) FROM nico_worker_claimer")
    op.execute("DROP FUNCTION claim_next_run(text, integer)")
    op.execute("REVOKE USAGE ON SCHEMA public FROM nico_worker_claimer")
    op.execute("REVOKE nico_worker_claimer FROM CURRENT_USER")
    op.execute("DROP ROLE IF EXISTS nico_worker_claimer")
    op.drop_table("runtime_sessions")
