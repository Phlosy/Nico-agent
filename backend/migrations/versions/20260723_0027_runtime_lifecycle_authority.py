"""Add the guarded Runtime Run lifecycle authority.

Revision ID: 20260723_0027
Revises: 20260722_0026
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260723_0027"
down_revision: str | None = "20260722_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_OBJECT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    # Expand first. Old application binaries ignore these nullable/defaulted
    # columns and continue to use the unchanged queue/public response shape.
    op.add_column("runs", sa.Column("lifecycle_reason", sa.String(150)))
    op.add_column(
        "runs",
        sa.Column(
            "lifecycle_metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=JSON_OBJECT,
        ),
    )
    op.add_column(
        "runs",
        sa.Column("lifecycle_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_runs_lifecycle_revision",
        "runs",
        "lifecycle_revision >= 0 AND lifecycle_revision <= revision",
    )
    op.create_check_constraint(
        "ck_runs_lifecycle_metadata_size",
        "runs",
        "octet_length(lifecycle_metadata::text) <= 16384",
    )
    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.create_check_constraint(
        "ck_runs_status",
        "runs",
        "status IN ('pending', 'planning', 'running', 'waiting_for_tool', "
        "'waiting_for_approval', 'waiting_for_user_input', "
        "'waiting_for_subagent', 'paused', 'completed', 'failed', "
        "'cancelled', 'timed_out')",
    )

    op.execute(_transition_allowed_function())
    op.execute(_claimable_function())
    op.execute(
        "REVOKE ALL ON FUNCTION runtime_lifecycle_transition_allowed(text, text) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION runtime_lifecycle_transition_allowed(text, text) "
        "TO nico_runtime, nico_worker_claimer"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION runtime_run_claimable(text, timestamptz, timestamptz) FROM PUBLIC"
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION runtime_run_claimable(text, timestamptz, timestamptz) "
        "TO nico_runtime, nico_worker_claimer"
    )
    op.execute(_transition_function())
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "transition_run_lifecycle("
        "uuid, uuid, text, text, text, jsonb, text, uuid, text, text"
        ") FROM PUBLIC, nico_worker_claimer"
    )
    op.execute(_claim_next_run())
    op.execute("REVOKE ALL ON FUNCTION claim_next_run(text, integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION claim_next_run(text, integer) TO nico_worker_claimer")
    op.execute(_coordination_reconciler())
    op.execute("REVOKE ALL ON FUNCTION reconcile_coordination_waiters() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION reconcile_coordination_waiters() TO nico_worker_claimer")
    op.execute(_approval_reconciler())
    op.execute("REVOKE ALL ON FUNCTION reconcile_expired_tool_approvals() FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION reconcile_expired_tool_approvals() TO nico_worker_claimer"
    )


def downgrade() -> None:
    # A production rollback is code-only: retain expanded columns and lifecycle
    # evidence. This reversible path exists for isolated migration rehearsal.
    op.execute(_legacy_coordination_reconciler())
    op.execute("REVOKE ALL ON FUNCTION reconcile_coordination_waiters() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION reconcile_coordination_waiters() TO nico_worker_claimer")
    op.execute(_legacy_approval_reconciler())
    op.execute("REVOKE ALL ON FUNCTION reconcile_expired_tool_approvals() FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION reconcile_expired_tool_approvals() TO nico_worker_claimer"
    )
    op.execute(_legacy_claim_next_run())
    op.execute("REVOKE ALL ON FUNCTION claim_next_run(text, integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION claim_next_run(text, integer) TO nico_worker_claimer")
    op.execute(
        "UPDATE runs SET status = 'paused', lease_owner = NULL, lease_token = NULL, "
        "lease_expires_at = NULL, heartbeat_at = NULL, revision = revision + 1 "
        "WHERE status = 'waiting_for_user_input'"
    )
    op.execute(
        "DROP FUNCTION transition_run_lifecycle("
        "uuid, uuid, text, text, text, jsonb, text, uuid, text, text)"
    )
    op.execute("DROP FUNCTION runtime_run_claimable(text, timestamptz, timestamptz)")
    op.execute("DROP FUNCTION runtime_lifecycle_transition_allowed(text, text)")
    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.create_check_constraint(
        "ck_runs_status",
        "runs",
        "status IN ('pending', 'planning', 'running', 'waiting_for_tool', "
        "'waiting_for_approval', 'waiting_for_subagent', 'paused', "
        "'completed', 'failed', 'cancelled', 'timed_out')",
    )
    op.drop_constraint("ck_runs_lifecycle_metadata_size", "runs", type_="check")
    op.drop_constraint("ck_runs_lifecycle_revision", "runs", type_="check")
    op.drop_column("runs", "lifecycle_revision")
    op.drop_column("runs", "lifecycle_metadata")
    op.drop_column("runs", "lifecycle_reason")


def _transition_function() -> str:
    return r"""
    CREATE FUNCTION transition_run_lifecycle(
        p_tenant_id uuid,
        p_run_id uuid,
        p_expected_source text,
        p_target text,
        p_reason text,
        p_metadata jsonb,
        p_actor_id text,
        p_correlation_id uuid,
        p_event_type text,
        p_action text
    )
    RETURNS boolean
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $function$
    DECLARE
        current_run public.runs%ROWTYPE;
        transitioned public.runs%ROWTYPE;
        allowed boolean := false;
        clear_lease boolean := false;
        claimable boolean := false;
        payload jsonb;
    BEGIN
        IF p_reason IS NULL OR length(btrim(p_reason)) < 1 OR length(p_reason) > 150 THEN
            RAISE EXCEPTION 'lifecycle reason must contain between 1 and 150 characters'
                USING ERRCODE = 'check_violation';
        END IF;
        IF p_actor_id IS NULL OR length(p_actor_id) < 1 OR length(p_actor_id) > 200 THEN
            RAISE EXCEPTION 'lifecycle actor must contain between 1 and 200 characters'
                USING ERRCODE = 'check_violation';
        END IF;
        IF p_correlation_id IS NULL THEN
            RAISE EXCEPTION 'lifecycle correlation id is required'
                USING ERRCODE = 'check_violation';
        END IF;
        IF p_metadata IS NULL OR jsonb_typeof(p_metadata) <> 'object'
           OR octet_length(p_metadata::text) > 16384 THEN
            RAISE EXCEPTION 'lifecycle metadata must be a bounded object'
                USING ERRCODE = 'check_violation';
        END IF;

        SELECT * INTO current_run
        FROM public.runs
        WHERE tenant_id = p_tenant_id AND id = p_run_id
        FOR UPDATE;
        IF NOT FOUND OR current_run.status <> p_expected_source THEN
            RETURN false;
        END IF;

        allowed := public.runtime_lifecycle_transition_allowed(
            current_run.status, p_target
        );
        IF NOT allowed THEN
            RAISE EXCEPTION 'invalid Run lifecycle transition from % to %',
                current_run.status, p_target
                USING ERRCODE = 'check_violation';
        END IF;

        clear_lease := (
            p_target IN (
            'waiting_for_approval', 'waiting_for_user_input', 'waiting_for_subagent',
            'paused', 'completed', 'failed', 'cancelled', 'timed_out'
            ) OR current_run.status IN (
                'waiting_for_approval', 'waiting_for_user_input', 'waiting_for_subagent'
            )
        ) AND NOT (
            current_run.status = 'waiting_for_approval'
            AND p_target = 'running'
            AND current_run.lease_owner IS NOT NULL
            AND current_run.lease_token IS NOT NULL
            AND current_run.lease_expires_at IS NOT NULL
            AND current_run.lease_expires_at > clock_timestamp()
        );

        UPDATE public.runs
        SET status = p_target,
            lifecycle_reason = btrim(p_reason),
            lifecycle_metadata = p_metadata,
            lifecycle_revision = lifecycle_revision + 1,
            revision = revision + 1,
            started_at = CASE
                WHEN current_run.status = 'pending' AND p_target = 'planning'
                    THEN COALESCE(started_at, clock_timestamp())
                ELSE started_at
            END,
            ended_at = CASE
                WHEN p_target IN ('completed', 'failed', 'cancelled', 'timed_out')
                    THEN COALESCE(ended_at, clock_timestamp())
                ELSE ended_at
            END,
            lease_owner = CASE WHEN clear_lease THEN NULL ELSE lease_owner END,
            lease_token = CASE WHEN clear_lease THEN NULL ELSE lease_token END,
            lease_expires_at = CASE WHEN clear_lease THEN NULL ELSE lease_expires_at END,
            heartbeat_at = CASE WHEN clear_lease THEN NULL ELSE heartbeat_at END,
            updated_at = clock_timestamp()
        WHERE tenant_id = p_tenant_id AND id = p_run_id
          AND status = p_expected_source
        RETURNING * INTO transitioned;
        IF NOT FOUND THEN
            RETURN false;
        END IF;

        claimable := public.runtime_run_claimable(
            p_target, transitioned.lease_expires_at, clock_timestamp()
        );
        payload := jsonb_build_object(
            'contract_version', 1,
            'source', current_run.status,
            'target', p_target,
            'status', p_target,
            'reason', btrim(p_reason),
            'metadata', p_metadata,
            'revision', transitioned.revision,
            'lifecycle_revision', transitioned.lifecycle_revision,
            'claimable', claimable
        );
        INSERT INTO public.events (
            tenant_id, event_type, aggregate_type, aggregate_id, run_id,
            actor_id, payload, correlation_id
        ) VALUES (
            p_tenant_id, p_event_type, 'run', p_run_id, p_run_id,
            p_actor_id, payload, p_correlation_id
        );
        INSERT INTO public.audit_records (
            tenant_id, action, resource_type, resource_id, actor_id,
            details, correlation_id
        ) VALUES (
            p_tenant_id, p_action, 'run', p_run_id, p_actor_id,
            payload, p_correlation_id
        );
        RETURN true;
    END;
    $function$;
    """


def _transition_allowed_function() -> str:
    return r"""
    CREATE FUNCTION runtime_lifecycle_transition_allowed(p_source text, p_target text)
    RETURNS boolean
    LANGUAGE sql
    IMMUTABLE
    PARALLEL SAFE
    SET search_path = pg_catalog, public
    AS $function$
        SELECT CASE p_source
            WHEN 'pending' THEN p_target IN ('planning', 'cancelled', 'timed_out')
            WHEN 'planning' THEN p_target IN (
                'running', 'paused', 'completed', 'failed', 'cancelled', 'timed_out'
            )
            WHEN 'running' THEN p_target IN (
                'waiting_for_tool', 'waiting_for_approval', 'waiting_for_subagent',
                'paused', 'completed', 'failed',
                'cancelled', 'timed_out'
            )
            WHEN 'waiting_for_tool' THEN p_target IN ('running', 'cancelled')
            WHEN 'waiting_for_approval' THEN p_target IN ('running', 'failed', 'cancelled')
            -- Reserved until the durable U3 request/answer/wake protocol exists.
            WHEN 'waiting_for_user_input' THEN false
            WHEN 'waiting_for_subagent' THEN p_target IN ('running', 'failed', 'cancelled')
            WHEN 'paused' THEN p_target IN ('running', 'cancelled')
            ELSE false
        END
    $function$;
    """


def _claimable_function() -> str:
    return r"""
    CREATE FUNCTION runtime_run_claimable(
        p_status text,
        p_lease_expires_at timestamptz,
        p_at timestamptz
    )
    RETURNS boolean
    LANGUAGE sql
    IMMUTABLE
    PARALLEL SAFE
    SET search_path = pg_catalog, public
    AS $function$
        SELECT (
            p_status IN ('pending', 'planning', 'running')
            AND (p_lease_expires_at IS NULL OR p_lease_expires_at <= p_at)
        ) OR (
            p_status = 'waiting_for_tool'
            AND p_lease_expires_at IS NOT NULL
            AND p_lease_expires_at <= p_at
        )
    $function$;
    """


def _claim_next_run() -> str:
    # Keep the 0026 conversation queue guard and ordering byte-for-byte in
    # semantics. waiting_for_tool remains reclaimable only after lease expiry.
    return r"""
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
            LEFT JOIN public.conversation_turns AS ct
              ON ct.tenant_id = r.tenant_id AND ct.run_id = r.id
            LEFT JOIN public.conversations AS conversation
              ON conversation.tenant_id = ct.tenant_id
             AND conversation.id = ct.conversation_id
            WHERE public.runtime_run_claimable(
                      r.status, r.lease_expires_at, claimed_at
                  )
              AND (
                  ct.id IS NULL
                  OR (
                      NOT EXISTS (
                          SELECT 1 FROM public.conversation_turns AS earlier
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


def _legacy_claim_next_run() -> str:
    return _claim_next_run().replace(
        """public.runtime_run_claimable(
                      r.status, r.lease_expires_at, claimed_at
                  )""",
        """r.status IN ('pending', 'planning', 'running', 'waiting_for_tool')
              AND (r.lease_expires_at IS NULL OR r.lease_expires_at <= claimed_at)""",
    )


def _coordination_reconciler() -> str:
    return r"""
    CREATE OR REPLACE FUNCTION reconcile_coordination_waiters()
    RETURNS integer
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $function$
    DECLARE
        candidate record;
        correlation uuid;
        affected integer := 0;
    BEGIN
        FOR candidate IN
            SELECT parent.tenant_id, parent.id
            FROM public.runs AS parent
            WHERE parent.status = 'waiting_for_subagent'
              AND EXISTS (
                  SELECT 1 FROM public.delegations AS child
                  WHERE child.tenant_id = parent.tenant_id
                    AND child.parent_run_id = parent.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM public.delegations AS child
                  WHERE child.tenant_id = parent.tenant_id
                    AND child.parent_run_id = parent.id
                    AND child.status IN ('proposed', 'accepted', 'running')
              )
            ORDER BY parent.created_at, parent.id
            FOR UPDATE OF parent SKIP LOCKED
        LOOP
            correlation := public.gen_random_uuid();
            IF public.transition_run_lifecycle(
                candidate.tenant_id, candidate.id, 'waiting_for_subagent', 'running',
                'coordination_children_terminal', '{}'::jsonb,
                'system:coordination-reconciler', correlation,
                'RunWoken', 'coordination.reconcile.wake'
            ) THEN
                UPDATE public.runtime_sessions
                SET status = 'paused',
                    provider_state = provider_state || jsonb_build_object(
                        'coordination_reconciled_at', clock_timestamp()
                    ),
                    revision = revision + 1,
                    updated_at = clock_timestamp()
                WHERE tenant_id = candidate.tenant_id
                  AND run_id = candidate.id
                  AND status = 'suspended';
                affected := affected + 1;
            END IF;
        END LOOP;
        RETURN affected;
    END;
    $function$;
    """


def _approval_reconciler() -> str:
    return r"""
    CREATE OR REPLACE FUNCTION reconcile_expired_tool_approvals()
    RETURNS integer
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $function$
    DECLARE
        approval public.tool_approval_requests%ROWTYPE;
        correlation uuid;
        affected integer := 0;
        payload jsonb;
    BEGIN
        FOR approval IN
            SELECT *
            FROM public.tool_approval_requests
            WHERE status = 'requested' AND expires_at <= clock_timestamp()
            ORDER BY expires_at, id
            FOR UPDATE SKIP LOCKED
        LOOP
            correlation := public.gen_random_uuid();
            UPDATE public.tool_approval_requests
            SET status = 'expired',
                allowed_scope = 'none',
                decided_by = 'system:approval-expiry',
                decision = jsonb_build_object(
                    'status', 'expired',
                    'scope', 'none',
                    'reason', 'approval request expired before a decision'
                ),
                decision_idempotency_key = 'expiry:' || approval.id::text,
                decided_at = clock_timestamp(),
                revision = revision + 1,
                updated_at = clock_timestamp()
            WHERE tenant_id = approval.tenant_id AND id = approval.id;

            UPDATE public.tool_calls
            SET status = 'failed',
                error = jsonb_build_object(
                    'code', 'TOOL_APPROVAL_EXPIRED',
                    'message', 'approval request expired before a decision'
                ),
                ended_at = clock_timestamp(),
                revision = revision + 1,
                updated_at = clock_timestamp()
            WHERE tenant_id = approval.tenant_id
              AND id = approval.tool_call_id
              AND status IN ('pending', 'running');

            UPDATE public.run_steps
            SET status = 'failed',
                error = jsonb_build_object(
                    'code', 'TOOL_APPROVAL_EXPIRED',
                    'message', 'approval request expired before a decision'
                ),
                ended_at = clock_timestamp(),
                revision = revision + 1,
                updated_at = clock_timestamp()
            WHERE tenant_id = approval.tenant_id
              AND id = approval.run_step_id
              AND status IN ('pending', 'running', 'waiting');

            PERFORM public.transition_run_lifecycle(
                approval.tenant_id, approval.run_id,
                'waiting_for_approval', 'running',
                'tool_approval_expired',
                jsonb_build_object(
                    'approval_id', approval.id::text,
                    'tool_call_id', approval.tool_call_id::text
                ),
                'system:approval-expiry', correlation,
                'RunWoken', 'tool.approval.reconcile.wake'
            );

            UPDATE public.runtime_sessions
            SET provider_state = provider_state || jsonb_build_object(
                    'tool_approval_decision', jsonb_build_object(
                        'approval_id', approval.id::text,
                        'status', 'expired',
                        'scope', 'none'
                    )
                ),
                revision = revision + 1,
                updated_at = clock_timestamp()
            WHERE tenant_id = approval.tenant_id AND run_id = approval.run_id;

            payload := jsonb_build_object(
                'approval_id', approval.id::text,
                'tool_call_id', approval.tool_call_id::text,
                'risk_level', approval.risk_level,
                'status', 'expired',
                'allowed_scope', 'none',
                'decided_by', 'system:approval-expiry',
                'revision', approval.revision + 1
            );
            INSERT INTO public.events (
                tenant_id, event_type, aggregate_type, aggregate_id, run_id,
                actor_id, payload, correlation_id
            ) VALUES (
                approval.tenant_id, 'ToolApprovalExpired', 'tool_approval_request',
                approval.id, approval.run_id, 'system:approval-expiry', payload, correlation
            );
            INSERT INTO public.audit_records (
                tenant_id, action, resource_type, resource_id, actor_id, details, correlation_id
            ) VALUES (
                approval.tenant_id, 'tool.approval.expired', 'tool_approval_request',
                approval.id, 'system:approval-expiry', payload, correlation
            );
            affected := affected + 1;
        END LOOP;
        RETURN affected;
    END;
    $function$;
    """


def _legacy_coordination_reconciler() -> str:
    return r"""
    CREATE OR REPLACE FUNCTION reconcile_coordination_waiters()
    RETURNS integer
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $function$
    DECLARE ready_ids uuid[]; affected integer := 0;
    BEGIN
        SELECT array_agg(candidate.id) INTO ready_ids
        FROM (
            SELECT parent.id FROM public.runs AS parent
            WHERE parent.status = 'waiting_for_subagent'
              AND EXISTS (
                  SELECT 1 FROM public.delegations AS child
                  WHERE child.tenant_id = parent.tenant_id
                    AND child.parent_run_id = parent.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM public.delegations AS child
                  WHERE child.tenant_id = parent.tenant_id
                    AND child.parent_run_id = parent.id
                    AND child.status IN ('proposed', 'accepted', 'running')
              )
            FOR UPDATE OF parent SKIP LOCKED
        ) AS candidate;
        IF ready_ids IS NULL THEN RETURN 0; END IF;
        UPDATE public.runs
        SET status = 'running', lease_owner = NULL, lease_token = NULL,
            lease_expires_at = NULL, heartbeat_at = NULL,
            revision = revision + 1, updated_at = clock_timestamp()
        WHERE id = ANY(ready_ids);
        GET DIAGNOSTICS affected = ROW_COUNT;
        UPDATE public.runtime_sessions
        SET status = 'paused',
            provider_state = provider_state || jsonb_build_object(
                'coordination_reconciled_at', clock_timestamp()
            ),
            revision = revision + 1, updated_at = clock_timestamp()
        WHERE run_id = ANY(ready_ids) AND status = 'suspended';
        RETURN affected;
    END;
    $function$;
    """


def _legacy_approval_reconciler() -> str:
    # 0026 uses the 0019 function body.
    return _approval_reconciler().replace(
        """
            PERFORM public.transition_run_lifecycle(
                approval.tenant_id, approval.run_id,
                'waiting_for_approval', 'running',
                'tool_approval_expired',
                jsonb_build_object(
                    'approval_id', approval.id::text,
                    'tool_call_id', approval.tool_call_id::text
                ),
                'system:approval-expiry', correlation,
                'RunWoken', 'tool.approval.reconcile.wake'
            );
""",
        """
            UPDATE public.runs
            SET status = 'running',
                revision = revision + 1,
                updated_at = clock_timestamp()
            WHERE tenant_id = approval.tenant_id
              AND id = approval.run_id
              AND status = 'waiting_for_approval';
""",
    )
