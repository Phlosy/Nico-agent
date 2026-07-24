"""Add durable Runtime user-input requests and wake reconciliation.

Revision ID: 20260723_0031
Revises: 20260723_0030
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260723_0031"
down_revision: str | None = "20260723_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")
UUID_DEFAULT = sa.text("gen_random_uuid()")
JSON_OBJECT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "user_input_requests",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("runtime_session_id", sa.Uuid(), nullable=False),
        sa.Column("action_batch_id", sa.Uuid(), nullable=False),
        sa.Column("agent_action_id", sa.Uuid(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("input_schema", postgresql.JSONB(), nullable=False),
        sa.Column(
            "redacted_projection",
            postgresql.JSONB(),
            nullable=False,
            server_default=JSON_OBJECT,
        ),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("wake_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="requested"),
        sa.Column("answer_payload", postgresql.JSONB()),
        sa.Column("answer_hash", sa.String(64)),
        sa.Column("answer_ref", sa.String(500)),
        sa.Column("answer_idempotency_key", sa.String(200)),
        sa.Column("answered_by", sa.String(200)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "status IN ('requested', 'answered', 'expired', 'cancelled')",
            name="ck_user_input_requests_status",
        ),
        sa.CheckConstraint("length(request_hash) = 64", name="ck_user_input_requests_hash"),
        sa.CheckConstraint("length(wake_key) = 64", name="ck_user_input_requests_wake_key"),
        sa.CheckConstraint(
            "answer_hash IS NULL OR length(answer_hash) = 64",
            name="ck_user_input_requests_answer_hash",
        ),
        sa.CheckConstraint(
            "octet_length(input_schema::text) <= 32768",
            name="ck_user_input_requests_schema_bound",
        ),
        sa.CheckConstraint(
            "answer_payload IS NULL OR octet_length(answer_payload::text) <= 65536",
            name="ck_user_input_requests_answer_bound",
        ),
        sa.CheckConstraint(
            "(status = 'requested' AND answer_payload IS NULL AND answer_hash IS NULL "
            "AND answer_ref IS NULL AND answer_idempotency_key IS NULL "
            "AND answered_by IS NULL AND answered_at IS NULL AND resolved_at IS NULL) OR "
            "(status = 'answered' AND answer_payload IS NOT NULL AND answer_hash IS NOT NULL "
            "AND answer_ref IS NOT NULL AND answer_idempotency_key IS NOT NULL "
            "AND answered_by IS NOT NULL AND answered_at IS NOT NULL "
            "AND resolved_at IS NOT NULL) OR "
            "(status IN ('expired', 'cancelled') AND answer_payload IS NULL "
            "AND answer_hash IS NULL AND answer_ref IS NULL "
            "AND answer_idempotency_key IS NOT NULL AND answered_by IS NOT NULL "
            "AND answered_at IS NULL AND resolved_at IS NOT NULL)",
            name="ck_user_input_requests_resolution_shape",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_user_input_requests_run",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "runtime_session_id"],
            ["runtime_sessions.tenant_id", "runtime_sessions.run_id", "runtime_sessions.id"],
            ondelete="RESTRICT",
            name="fk_user_input_requests_session",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "action_batch_id"],
            [
                "agent_action_batches.tenant_id",
                "agent_action_batches.run_id",
                "agent_action_batches.id",
            ],
            ondelete="RESTRICT",
            name="fk_user_input_requests_batch",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "agent_action_id"],
            ["agent_actions.tenant_id", "agent_actions.run_id", "agent_actions.id"],
            ondelete="RESTRICT",
            name="fk_user_input_requests_action",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_user_input_requests_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "agent_action_id",
            name="uq_user_input_requests_action",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "wake_key",
            name="uq_user_input_requests_wake_key",
        ),
    )
    op.create_index(
        "uq_user_input_requests_active_run",
        "user_input_requests",
        ["tenant_id", "run_id"],
        unique=True,
        postgresql_where=sa.text("status = 'requested'"),
    )
    op.create_index(
        "ix_user_input_requests_reconcile",
        "user_input_requests",
        ["status", "expires_at", "created_at"],
    )
    op.execute(_GUARD_REQUEST)
    op.execute(
        "CREATE TRIGGER guard_user_input_request BEFORE UPDATE OR DELETE "
        "ON user_input_requests FOR EACH ROW EXECUTE FUNCTION guard_user_input_request()"
    )
    op.execute(_CANCEL_ON_TERMINAL_RUN)
    op.execute(
        "CREATE TRIGGER cancel_user_input_on_terminal_run AFTER UPDATE OF status ON runs "
        "FOR EACH ROW EXECUTE FUNCTION cancel_user_input_on_terminal_run()"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON user_input_requests TO nico_runtime")
    _enable_rls("user_input_requests")

    op.execute(_transition_allowed_function(enabled=True))
    op.execute(_transition_function(preserve_user_input=True))
    op.execute(_RECONCILER)
    op.execute("REVOKE ALL ON FUNCTION reconcile_user_input_requests() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION reconcile_user_input_requests() TO nico_worker_claimer")


def downgrade() -> None:
    # Isolated rehearsal only. A production code rollback keeps this additive
    # table and leaves old binaries to ignore it.
    op.execute(
        "UPDATE runs SET status = 'paused', lease_owner = NULL, lease_token = NULL, "
        "lease_expires_at = NULL, heartbeat_at = NULL, revision = revision + 1 "
        "WHERE status = 'waiting_for_user_input'"
    )
    op.execute("DROP FUNCTION reconcile_user_input_requests()")
    op.execute(_transition_function(preserve_user_input=False))
    op.execute(_transition_allowed_function(enabled=False))
    op.execute("DROP TRIGGER cancel_user_input_on_terminal_run ON runs")
    op.execute("DROP FUNCTION cancel_user_input_on_terminal_run()")
    op.execute("DROP TRIGGER guard_user_input_request ON user_input_requests")
    op.execute("DROP FUNCTION guard_user_input_request()")
    op.drop_table("user_input_requests")


def _enable_rls(table: str) -> None:
    predicate = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(
        f'CREATE POLICY tenant_isolation ON "{table}" USING ({predicate}) WITH CHECK ({predicate})'
    )
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')


def _transition_allowed_function(*, enabled: bool) -> str:
    user_input_running = ", 'waiting_for_user_input'" if enabled else ""
    user_input_targets = "p_target IN ('running', 'failed', 'cancelled')" if enabled else "false"
    return f"""
    CREATE OR REPLACE FUNCTION runtime_lifecycle_transition_allowed(
        p_source text, p_target text
    )
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
                'waiting_for_tool', 'waiting_for_approval'{user_input_running},
                'waiting_for_subagent', 'paused', 'completed', 'failed',
                'cancelled', 'timed_out'
            )
            WHEN 'waiting_for_tool' THEN p_target IN ('running', 'cancelled')
            WHEN 'waiting_for_approval' THEN p_target IN ('running', 'failed', 'cancelled')
            WHEN 'waiting_for_user_input' THEN {user_input_targets}
            WHEN 'waiting_for_subagent' THEN p_target IN ('running', 'failed', 'cancelled')
            WHEN 'paused' THEN p_target IN ('running', 'cancelled')
            ELSE false
        END
    $function$;
    """


def _transition_function(*, preserve_user_input: bool) -> str:
    preserve_source = (
        "current_run.status IN ('waiting_for_approval', 'waiting_for_user_input')"
        if preserve_user_input
        else "current_run.status = 'waiting_for_approval'"
    )
    return rf"""
    CREATE OR REPLACE FUNCTION transition_run_lifecycle(
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
            {preserve_source}
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


_GUARD_REQUEST = r"""
CREATE FUNCTION guard_user_input_request()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'UserInputRequest facts are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF ROW(
        OLD.tenant_id, OLD.run_id, OLD.runtime_session_id, OLD.action_batch_id,
        OLD.agent_action_id, OLD.question, OLD.reason, OLD.input_schema,
        OLD.redacted_projection, OLD.request_hash, OLD.wake_key, OLD.expires_at
    ) IS DISTINCT FROM ROW(
        NEW.tenant_id, NEW.run_id, NEW.runtime_session_id, NEW.action_batch_id,
        NEW.agent_action_id, NEW.question, NEW.reason, NEW.input_schema,
        NEW.redacted_projection, NEW.request_hash, NEW.wake_key, NEW.expires_at
    ) THEN
        RAISE EXCEPTION 'UserInputRequest identity is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status <> 'requested' THEN
        RAISE EXCEPTION 'terminal UserInputRequest resolution is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status NOT IN ('answered', 'expired', 'cancelled')
       OR NEW.revision <> OLD.revision + 1 THEN
        RAISE EXCEPTION 'UserInputRequest may resolve exactly once'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$;
"""


_CANCEL_ON_TERMINAL_RUN = r"""
CREATE FUNCTION cancel_user_input_on_terminal_run()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
BEGIN
    IF NEW.status IN ('completed', 'failed', 'cancelled', 'timed_out')
       AND OLD.status IS DISTINCT FROM NEW.status THEN
        WITH cancelled AS (
            UPDATE public.user_input_requests
            SET status = 'cancelled',
                answer_idempotency_key = 'run-terminal:' || NEW.id::text,
                answered_by = 'system:run-terminal',
                resolved_at = clock_timestamp(),
                revision = revision + 1,
                updated_at = clock_timestamp()
            WHERE tenant_id = NEW.tenant_id
              AND run_id = NEW.id
              AND status = 'requested'
            RETURNING agent_action_id, action_batch_id
        ), blocked AS (
            UPDATE public.agent_actions AS action
            SET status = 'blocked',
                outcome_ref = 'user_input:cancelled',
                observation_ref = 'user_input:run-terminal',
                completed_at = clock_timestamp(),
                revision = revision + 1,
                updated_at = clock_timestamp()
            FROM cancelled
            WHERE action.tenant_id = NEW.tenant_id
              AND action.run_id = NEW.id
              AND action.id = cancelled.agent_action_id
              AND action.status = 'dispatched'
            RETURNING action.batch_id
        )
        UPDATE public.agent_action_batches AS batch
        SET status = 'failed',
            completed_at = clock_timestamp(),
            revision = revision + 1,
            updated_at = clock_timestamp()
        WHERE batch.tenant_id = NEW.tenant_id
          AND batch.run_id = NEW.id
          AND batch.id IN (SELECT batch_id FROM blocked)
          AND batch.status NOT IN ('completed', 'failed');
    END IF;
    RETURN NEW;
END;
$function$;
"""


_RECONCILER = r"""
CREATE OR REPLACE FUNCTION reconcile_user_input_requests()
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    candidate record;
    request public.user_input_requests%ROWTYPE;
    correlation uuid;
    affected integer := 0;
    payload jsonb;
BEGIN
    FOR candidate IN
        SELECT run.tenant_id, run.id AS run_id, input.id AS request_id
        FROM public.runs AS run
        JOIN public.user_input_requests AS input
          ON input.tenant_id = run.tenant_id AND input.run_id = run.id
        WHERE (
            input.status = 'requested' AND input.expires_at <= clock_timestamp()
        ) OR (
            input.status = 'answered' AND run.status = 'waiting_for_user_input'
        )
        ORDER BY input.expires_at, input.id
        FOR UPDATE OF run SKIP LOCKED
    LOOP
        SELECT * INTO request
        FROM public.user_input_requests
        WHERE tenant_id = candidate.tenant_id AND id = candidate.request_id
        FOR UPDATE;
        correlation := public.gen_random_uuid();

        IF request.status = 'requested' AND request.expires_at <= clock_timestamp() THEN
            UPDATE public.user_input_requests
            SET status = 'expired',
                answer_idempotency_key = 'expiry:' || request.id::text,
                answered_by = 'system:user-input-reconciler',
                resolved_at = clock_timestamp(),
                revision = revision + 1,
                updated_at = clock_timestamp()
            WHERE tenant_id = request.tenant_id AND id = request.id;
            UPDATE public.agent_actions
            SET status = 'blocked',
                outcome_ref = 'user_input:' || request.id::text,
                observation_ref = 'user_input:' || request.id::text || chr(58) || 'expired',
                completed_at = clock_timestamp(),
                revision = revision + 1,
                updated_at = clock_timestamp()
            WHERE tenant_id = request.tenant_id
              AND run_id = request.run_id
              AND id = request.agent_action_id
              AND status = 'dispatched';
            UPDATE public.agent_action_batches
            SET status = 'failed',
                completed_at = clock_timestamp(),
                revision = revision + 1,
                updated_at = clock_timestamp()
            WHERE tenant_id = request.tenant_id
              AND run_id = request.run_id
              AND id = request.action_batch_id
              AND status NOT IN ('completed', 'failed');
            IF EXISTS (
                SELECT 1 FROM public.runs
                WHERE tenant_id = request.tenant_id AND id = request.run_id
                  AND status = 'waiting_for_user_input'
            ) THEN
                PERFORM public.transition_run_lifecycle(
                    request.tenant_id, request.run_id,
                    'waiting_for_user_input', 'running',
                    'user_input_expired',
                    jsonb_build_object('request_id', request.id, 'status', 'expired'),
                    'system:user-input-reconciler', correlation,
                    'RunWoken', 'user_inputs.reconcile.expire'
                );
            END IF;
            payload := jsonb_build_object(
                'request_id', request.id,
                'agent_action_id', request.agent_action_id,
                'status', 'expired',
                'request_hash', request.request_hash
            );
            INSERT INTO public.events (
                tenant_id, event_type, aggregate_type, aggregate_id, run_id,
                actor_id, payload, correlation_id
            ) VALUES (
                request.tenant_id, 'UserInputExpired', 'user_input_request',
                request.id, request.run_id, 'system:user-input-reconciler',
                payload, correlation
            );
            INSERT INTO public.audit_records (
                tenant_id, action, resource_type, resource_id, actor_id,
                details, correlation_id
            ) VALUES (
                request.tenant_id, 'user_input.expired', 'user_input_request',
                request.id, 'system:user-input-reconciler', payload, correlation
            );
            affected := affected + 1;
        ELSIF request.status = 'answered' THEN
            IF public.transition_run_lifecycle(
                request.tenant_id, request.run_id,
                'waiting_for_user_input', 'running',
                'user_input_answer_reconciled',
                jsonb_build_object(
                    'request_id', request.id,
                    'answer_hash', request.answer_hash
                ),
                'system:user-input-reconciler', correlation,
                'RunWoken', 'user_inputs.reconcile.wake'
            ) THEN
                affected := affected + 1;
            END IF;
        END IF;

        UPDATE public.runtime_sessions
        SET status = 'paused',
            provider_state = provider_state || jsonb_build_object(
                'user_input_reconciled_at', clock_timestamp(),
                'user_input_request_id', request.id
            ),
            revision = revision + 1,
            updated_at = clock_timestamp()
        WHERE tenant_id = request.tenant_id
          AND run_id = request.run_id
          AND status = 'suspended';
    END LOOP;
    RETURN affected;
END;
$function$;
"""
