"""Freeze per-Run tool policy and bind ToolCall execution to a Run lease.

Revision ID: 20260717_0005
Revises: 20260717_0004
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260717_0005"
down_revision: str | None = "20260717_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_OBJECT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.add_column(
        "runtime_sessions",
        sa.Column(
            "tool_policy_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=JSON_OBJECT,
        ),
    )
    op.add_column("tool_calls", sa.Column("execution_owner", sa.String(200)))
    op.add_column("tool_calls", sa.Column("execution_lease_token", sa.Uuid()))
    _replace_claim_function("'pending', 'planning', 'running', 'waiting_for_tool'")


def downgrade() -> None:
    _replace_claim_function("'pending', 'planning', 'running'")
    op.drop_column("tool_calls", "execution_lease_token")
    op.drop_column("tool_calls", "execution_owner")
    op.drop_column("runtime_sessions", "tool_policy_snapshot")


def _replace_claim_function(statuses: str) -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION claim_next_run(p_worker_id text, p_lease_seconds integer)
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
                WHERE r.status IN ({statuses})
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
