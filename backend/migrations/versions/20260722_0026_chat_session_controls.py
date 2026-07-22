"""Add durable Conversation queue and approval policy state.

Revision ID: 20260722_0026
Revises: 20260722_0025
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260722_0026"
down_revision: str | None = "20260722_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("approval_mode", sa.String(32), nullable=False, server_default="ask"),
    )
    op.add_column(
        "conversations",
        sa.Column("queue_state", sa.String(32), nullable=False, server_default="active"),
    )
    op.add_column("conversations", sa.Column("queue_pause_reason", sa.String(64)))
    op.add_column("conversations", sa.Column("queue_pause_turn_id", sa.Uuid()))
    op.add_column(
        "conversations",
        sa.Column("queue_paused_at", sa.DateTime(timezone=True)),
    )
    op.create_check_constraint(
        "ck_conversations_approval_mode",
        "conversations",
        "approval_mode IN ('ask', 'auto-medium', 'auto-all')",
    )
    op.create_check_constraint(
        "ck_conversations_queue_state",
        "conversations",
        "queue_state IN ('active', 'paused')",
    )
    op.create_check_constraint(
        "ck_conversations_queue_pause_reason",
        "conversations",
        "queue_pause_reason IS NULL OR queue_pause_reason IN "
        "('run_failed', 'run_timed_out', 'turn_cancelled', 'tool_rejected', "
        "'approval_expired', 'approval_policy_changed')",
    )
    op.create_check_constraint(
        "ck_conversations_queue_pause_fields",
        "conversations",
        "(queue_state = 'active' AND queue_pause_reason IS NULL "
        "AND queue_pause_turn_id IS NULL AND queue_paused_at IS NULL) OR "
        "(queue_state = 'paused' AND queue_pause_reason IS NOT NULL "
        "AND queue_pause_turn_id IS NOT NULL AND queue_paused_at IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_conversations_queue_pause_turn",
        "conversations",
        "conversation_turns",
        ["tenant_id", "id", "queue_pause_turn_id"],
        ["tenant_id", "conversation_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_conversations_queue",
        "conversations",
        ["tenant_id", "queue_state", "updated_at"],
    )
    op.create_index(
        "ix_conversation_turns_queue",
        "conversation_turns",
        ["tenant_id", "conversation_id", "status", "sequence"],
    )

    op.execute(_queue_aware_turn_projection())
    op.execute(_approval_queue_projection())
    op.execute(
        "CREATE TRIGGER project_conversation_queue_after_tool_approval "
        "AFTER UPDATE OF status ON tool_approval_requests FOR EACH ROW "
        "EXECUTE FUNCTION project_conversation_queue_from_tool_approval()"
    )
    op.execute(_claim_next_run(queue_aware=True))
    op.execute("REVOKE ALL ON FUNCTION claim_next_run(text, integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION claim_next_run(text, integer) TO nico_worker_claimer")


def downgrade() -> None:
    op.execute(_claim_next_run(queue_aware=False))
    op.execute("REVOKE ALL ON FUNCTION claim_next_run(text, integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION claim_next_run(text, integer) TO nico_worker_claimer")
    op.execute(
        "DROP TRIGGER project_conversation_queue_after_tool_approval "
        "ON tool_approval_requests"
    )
    op.execute("DROP FUNCTION project_conversation_queue_from_tool_approval()")
    op.execute(_legacy_turn_projection())

    op.drop_index("ix_conversation_turns_queue", table_name="conversation_turns")
    op.drop_index("ix_conversations_queue", table_name="conversations")
    op.drop_constraint(
        "fk_conversations_queue_pause_turn", "conversations", type_="foreignkey"
    )
    op.drop_constraint(
        "ck_conversations_queue_pause_fields", "conversations", type_="check"
    )
    op.drop_constraint(
        "ck_conversations_queue_pause_reason", "conversations", type_="check"
    )
    op.drop_constraint("ck_conversations_queue_state", "conversations", type_="check")
    op.drop_constraint("ck_conversations_approval_mode", "conversations", type_="check")
    op.drop_column("conversations", "queue_paused_at")
    op.drop_column("conversations", "queue_pause_turn_id")
    op.drop_column("conversations", "queue_pause_reason")
    op.drop_column("conversations", "queue_state")
    op.drop_column("conversations", "approval_mode")


def _queue_aware_turn_projection() -> str:
    return r"""
    CREATE OR REPLACE FUNCTION project_conversation_turn_from_run()
    RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
    DECLARE
        projected_status text;
        current_turn public.conversation_turns%ROWTYPE;
        is_head boolean := false;
        pause_reason text;
    BEGIN
        SELECT * INTO current_turn
        FROM public.conversation_turns
        WHERE tenant_id = NEW.tenant_id AND run_id = NEW.id
        FOR UPDATE;

        IF FOUND THEN
            is_head := NOT EXISTS (
                SELECT 1
                FROM public.conversation_turns AS earlier
                WHERE earlier.tenant_id = current_turn.tenant_id
                  AND earlier.conversation_id = current_turn.conversation_id
                  AND earlier.sequence < current_turn.sequence
                  AND earlier.status IN (
                      'accepted', 'queued', 'running', 'waiting_for_approval'
                  )
            );
        END IF;

        projected_status := CASE NEW.status
            WHEN 'pending' THEN 'queued'
            WHEN 'waiting_for_approval' THEN 'waiting_for_approval'
            WHEN 'completed' THEN 'completed'
            WHEN 'failed' THEN 'failed'
            WHEN 'timed_out' THEN 'failed'
            WHEN 'cancelled' THEN 'cancelled'
            ELSE 'running'
        END;
        UPDATE public.conversation_turns
           SET status = projected_status,
               assistant_output = NEW.result,
               usage = COALESCE(NEW.cost, '{}'::jsonb),
               error = NEW.error,
               revision = revision + 1,
               updated_at = clock_timestamp()
         WHERE tenant_id = NEW.tenant_id
           AND run_id = NEW.id
           AND (status, assistant_output, usage, error)
               IS DISTINCT FROM
               (projected_status, NEW.result, COALESCE(NEW.cost, '{}'::jsonb), NEW.error);

        pause_reason := CASE NEW.status
            WHEN 'failed' THEN 'run_failed'
            WHEN 'timed_out' THEN 'run_timed_out'
            WHEN 'cancelled' THEN 'turn_cancelled'
            ELSE NULL
        END;
        IF current_turn.id IS NOT NULL AND is_head AND pause_reason IS NOT NULL THEN
            UPDATE public.conversations
               SET queue_state = 'paused',
                   queue_pause_reason = pause_reason,
                   queue_pause_turn_id = current_turn.id,
                   queue_paused_at = clock_timestamp(),
                   revision = revision + 1,
                   updated_at = clock_timestamp()
             WHERE tenant_id = current_turn.tenant_id
               AND id = current_turn.conversation_id
               AND queue_state = 'active';
        END IF;
        RETURN NEW;
    END;
    $function$;
    """


def _approval_queue_projection() -> str:
    return r"""
    CREATE FUNCTION project_conversation_queue_from_tool_approval()
    RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
    DECLARE
        current_turn public.conversation_turns%ROWTYPE;
        is_head boolean := false;
        pause_reason text;
    BEGIN
        IF OLD.status = 'requested' AND NEW.status IN ('rejected', 'expired') THEN
            SELECT * INTO current_turn
            FROM public.conversation_turns
            WHERE tenant_id = NEW.tenant_id AND run_id = NEW.run_id
            FOR UPDATE;
            IF FOUND THEN
                is_head := NOT EXISTS (
                    SELECT 1
                    FROM public.conversation_turns AS earlier
                    WHERE earlier.tenant_id = current_turn.tenant_id
                      AND earlier.conversation_id = current_turn.conversation_id
                      AND earlier.sequence < current_turn.sequence
                      AND earlier.status IN (
                          'accepted', 'queued', 'running', 'waiting_for_approval'
                      )
                );
            END IF;
            pause_reason := CASE NEW.status
                WHEN 'rejected' THEN 'tool_rejected'
                ELSE 'approval_expired'
            END;
            IF current_turn.id IS NOT NULL AND is_head THEN
                UPDATE public.conversations
                   SET queue_state = 'paused',
                       queue_pause_reason = pause_reason,
                       queue_pause_turn_id = current_turn.id,
                       queue_paused_at = clock_timestamp(),
                       revision = revision + 1,
                       updated_at = clock_timestamp()
                 WHERE tenant_id = current_turn.tenant_id
                   AND id = current_turn.conversation_id
                   AND queue_state = 'active';
            END IF;
        END IF;
        RETURN NEW;
    END;
    $function$;
    """


def _legacy_turn_projection() -> str:
    return r"""
    CREATE OR REPLACE FUNCTION project_conversation_turn_from_run()
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
        UPDATE public.conversation_turns
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


def _claim_next_run(*, queue_aware: bool) -> str:
    conversation_joins = ""
    conversation_guard = ""
    if queue_aware:
        conversation_joins = r"""
            LEFT JOIN public.conversation_turns AS ct
              ON ct.tenant_id = r.tenant_id AND ct.run_id = r.id
            LEFT JOIN public.conversations AS conversation
              ON conversation.tenant_id = ct.tenant_id
             AND conversation.id = ct.conversation_id
        """
        conversation_guard = r"""
              AND (
                  ct.id IS NULL
                  OR (
                      NOT EXISTS (
                          SELECT 1
                          FROM public.conversation_turns AS earlier
                          WHERE earlier.tenant_id = ct.tenant_id
                            AND earlier.conversation_id = ct.conversation_id
                            AND earlier.sequence < ct.sequence
                            AND earlier.status IN (
                                'accepted', 'queued', 'running', 'waiting_for_approval'
                            )
                      )
                      AND (
                          r.status IN ('planning', 'running', 'waiting_for_tool')
                          OR (
                              r.status = 'pending'
                              AND (
                                  conversation.queue_state = 'active'
                                  OR conversation.queue_pause_turn_id = ct.id
                              )
                          )
                      )
                  )
              )
        """
    return f"""
    CREATE OR REPLACE FUNCTION claim_next_run(p_worker_id text, p_lease_seconds integer)
    RETURNS TABLE(run_id uuid, tenant_id uuid, lease_token uuid, previous_status text)
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
    AS $function$
    DECLARE claimed_at timestamptz := clock_timestamp();
    BEGIN
        IF p_worker_id IS NULL OR length(p_worker_id) < 1 OR length(p_worker_id) > 200 THEN
            RAISE EXCEPTION 'worker_id must contain between 1 and 200 characters';
        END IF;
        IF p_lease_seconds < 5 OR p_lease_seconds > 3600 THEN
            RAISE EXCEPTION 'lease_seconds must be between 5 and 3600';
        END IF;
        PERFORM pg_advisory_xact_lock(hashtextextended('nico:runtime-maintenance', 0));
        IF EXISTS (
            SELECT 1 FROM public.deployment_maintenance
            WHERE singleton AND lease_expires_at > claimed_at
        ) THEN
            RETURN;
        END IF;
        RETURN QUERY
        WITH candidate AS (
            SELECT r.id FROM public.runs AS r
            JOIN public.tasks AS t ON t.tenant_id = r.tenant_id AND t.id = r.task_id
            {conversation_joins}
            WHERE r.status IN ('pending', 'planning', 'running', 'waiting_for_tool')
              AND (r.lease_expires_at IS NULL OR r.lease_expires_at <= claimed_at)
              {conversation_guard}
            ORDER BY t.priority DESC, r.created_at, r.id
            FOR UPDATE OF r SKIP LOCKED LIMIT 1
        ), claimed AS (
            UPDATE public.runs AS r
            SET lease_owner = p_worker_id, lease_token = gen_random_uuid(),
                lease_expires_at = claimed_at + make_interval(secs => p_lease_seconds),
                heartbeat_at = claimed_at, updated_at = claimed_at
            FROM candidate AS c WHERE r.id = c.id
            RETURNING r.id, r.tenant_id, r.lease_token, r.status
        )
        SELECT c.id, c.tenant_id, c.lease_token, c.status::text FROM claimed AS c;
    END;
    $function$;
    """
