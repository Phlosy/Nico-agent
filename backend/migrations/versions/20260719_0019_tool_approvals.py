"""Add durable ToolApprovalRequest facts and decision guards.

Revision ID: 20260719_0019
Revises: 20260719_0018
Create Date: 2026-07-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260719_0019"
down_revision: str | None = "20260719_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")
UUID_DEFAULT = sa.text("gen_random_uuid()")
JSON_OBJECT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "tool_approval_requests",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("run_step_id", sa.Uuid(), nullable=False),
        sa.Column("tool_call_id", sa.Uuid(), nullable=False),
        sa.Column("tool_definition_id", sa.Uuid(), nullable=False),
        sa.Column("risk_level", sa.String(20), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="requested"),
        sa.Column("allowed_scope", sa.String(20)),
        sa.Column("requester", sa.String(200), nullable=False),
        sa.Column(
            "arguments_redacted",
            postgresql.JSONB(),
            nullable=False,
            server_default=JSON_OBJECT,
        ),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("decided_by", sa.String(200)),
        sa.Column("decision", postgresql.JSONB()),
        sa.Column("decision_idempotency_key", sa.String(200)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "status IN ('requested', 'approved', 'rejected', 'expired', 'cancelled')",
            name="ck_tool_approval_requests_status",
        ),
        sa.CheckConstraint(
            "risk_level IN ('medium', 'high')",
            name="ck_tool_approval_requests_risk",
        ),
        sa.CheckConstraint(
            "allowed_scope IS NULL OR allowed_scope IN ('none', 'once', 'run')",
            name="ck_tool_approval_requests_scope",
        ),
        sa.CheckConstraint(
            "length(arguments_hash) = 64",
            name="ck_tool_approval_requests_arguments_hash",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_tool_approval_requests_expiry",
        ),
        sa.CheckConstraint(
            "(status = 'requested' AND allowed_scope IS NULL AND decided_by IS NULL "
            "AND decided_at IS NULL AND decision_idempotency_key IS NULL) OR "
            "(status = 'approved' AND allowed_scope IN ('once', 'run') "
            "AND decided_by IS NOT NULL AND decided_at IS NOT NULL "
            "AND decision_idempotency_key IS NOT NULL) OR "
            "(status IN ('rejected', 'expired', 'cancelled') AND allowed_scope = 'none' "
            "AND decided_by IS NOT NULL AND decided_at IS NOT NULL "
            "AND decision_idempotency_key IS NOT NULL)",
            name="ck_tool_approval_requests_decision_shape",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_tool_approval_requests_tenant_run",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "run_step_id"],
            ["run_steps.tenant_id", "run_steps.run_id", "run_steps.id"],
            ondelete="RESTRICT",
            name="fk_tool_approval_requests_tenant_run_step",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "run_step_id", "tool_call_id"],
            [
                "tool_calls.tenant_id",
                "tool_calls.run_id",
                "tool_calls.run_step_id",
                "tool_calls.id",
            ],
            ondelete="RESTRICT",
            name="fk_tool_approval_requests_tenant_tool_call",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "tool_definition_id"],
            ["tool_definitions.tenant_id", "tool_definitions.id"],
            ondelete="RESTRICT",
            name="fk_tool_approval_requests_tenant_definition",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tool_approval_requests_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "tool_call_id",
            name="uq_tool_approval_requests_tool_call",
        ),
    )
    op.create_index(
        "ix_tool_approval_requests_tenant_status",
        "tool_approval_requests",
        ["tenant_id", "status", "created_at"],
    )
    op.create_index(
        "ix_tool_approval_requests_run_scope",
        "tool_approval_requests",
        ["tenant_id", "run_id", "tool_definition_id", "status", "allowed_scope"],
    )
    op.execute(_guard_function())
    op.execute(
        "CREATE TRIGGER guard_tool_approval_request_write "
        "BEFORE INSERT OR UPDATE ON tool_approval_requests "
        "FOR EACH ROW EXECUTE FUNCTION guard_tool_approval_request_write()"
    )
    predicate = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(
        "CREATE POLICY tenant_isolation ON tool_approval_requests "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute("ALTER TABLE tool_approval_requests ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tool_approval_requests FORCE ROW LEVEL SECURITY")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON tool_approval_requests TO nico_runtime")
    op.execute(_expiry_reconciler_function())
    op.execute("REVOKE ALL ON FUNCTION reconcile_expired_tool_approvals() FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION reconcile_expired_tool_approvals() TO nico_worker_claimer"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS reconcile_expired_tool_approvals()")
    op.execute("DROP TRIGGER IF EXISTS guard_tool_approval_request_write ON tool_approval_requests")
    op.execute("DROP FUNCTION IF EXISTS guard_tool_approval_request_write()")
    op.drop_table("tool_approval_requests")


def _guard_function() -> str:
    return """
    CREATE FUNCTION guard_tool_approval_request_write()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public
    AS $function$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.status <> 'requested' OR NEW.revision <> 1 THEN
                RAISE EXCEPTION 'tool approval must start requested at revision 1';
            END IF;
            RETURN NEW;
        END IF;

        IF OLD.status <> 'requested' THEN
            RAISE EXCEPTION 'terminal tool approval is immutable';
        END IF;
        IF NEW.tenant_id <> OLD.tenant_id
           OR NEW.run_id <> OLD.run_id
           OR NEW.run_step_id <> OLD.run_step_id
           OR NEW.tool_call_id <> OLD.tool_call_id
           OR NEW.tool_definition_id <> OLD.tool_definition_id
           OR NEW.risk_level <> OLD.risk_level
           OR NEW.requester <> OLD.requester
           OR NEW.arguments_hash <> OLD.arguments_hash
           OR NEW.arguments_redacted <> OLD.arguments_redacted
           OR NEW.expires_at <> OLD.expires_at
           OR NEW.created_at <> OLD.created_at THEN
            RAISE EXCEPTION 'tool approval request identity is immutable';
        END IF;
        IF NEW.status = 'requested' OR NEW.revision <> OLD.revision + 1 THEN
            RAISE EXCEPTION 'tool approval decision must be one terminal revision';
        END IF;
        RETURN NEW;
    END;
    $function$
    """


def _expiry_reconciler_function() -> str:
    return r"""
    CREATE FUNCTION reconcile_expired_tool_approvals()
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

            UPDATE public.runs
            SET status = 'running',
                revision = revision + 1,
                updated_at = clock_timestamp()
            WHERE tenant_id = approval.tenant_id
              AND id = approval.run_id
              AND status = 'waiting_for_approval';

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
    $function$
    """
