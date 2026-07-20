"""Add tenant-scoped dynamic delegation, messages, closure, and budget ledgers.

Revision ID: 20260718_0013
Revises: 20260718_0012
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0013"
down_revision: str | None = "20260718_0012"
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
    op.add_column(
        "agent_versions",
        sa.Column(
            "coordination_policy", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT
        ),
    )
    op.add_column(
        "runtime_sessions",
        sa.Column(
            "coordination_policy_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=JSON_OBJECT,
        ),
    )
    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.create_check_constraint(
        "ck_runs_status",
        "runs",
        "status IN ('pending', 'planning', 'running', 'waiting_for_tool', "
        "'waiting_for_approval', 'waiting_for_subagent', 'paused', 'completed', "
        "'failed', 'cancelled', 'timed_out')",
    )

    op.create_table(
        "delegations",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("parent_run_id", sa.Uuid(), nullable=False),
        sa.Column("parent_run_step_id", sa.Uuid()),
        sa.Column("child_task_id", sa.Uuid(), nullable=False),
        sa.Column("child_run_id", sa.Uuid(), nullable=False),
        sa.Column("target_agent_id", sa.Uuid(), nullable=False),
        sa.Column("target_agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("acceptance", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("context_refs", postgresql.JSONB(), nullable=False, server_default=JSON_ARRAY),
        sa.Column("execution_mode", sa.String(16), nullable=False),
        sa.Column("budget_grant", postgresql.JSONB(), nullable=False),
        sa.Column("policy_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("permission_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("task_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="proposed"),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("error", postgresql.JSONB()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('proposed', 'accepted', 'running', 'completed', 'failed', "
            "'cancelled', 'rejected')",
            name="ck_delegations_status",
        ),
        sa.CheckConstraint(
            "execution_mode IN ('serial', 'parallel')", name="ck_delegations_execution_mode"
        ),
        sa.CheckConstraint("length(task_fingerprint) = 64", name="ck_delegations_fingerprint"),
        sa.CheckConstraint("revision > 0", name="ck_delegations_revision"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "parent_run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_delegations_parent_run",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "parent_run_id", "parent_run_step_id"],
            ["run_steps.tenant_id", "run_steps.run_id", "run_steps.id"],
            ondelete="RESTRICT",
            name="fk_delegations_parent_step",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "child_task_id"],
            ["tasks.tenant_id", "tasks.id"],
            ondelete="RESTRICT",
            name="fk_delegations_child_task",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "child_run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_delegations_child_run",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "target_agent_id", "target_agent_version_id"],
            ["agent_versions.tenant_id", "agent_versions.agent_id", "agent_versions.id"],
            ondelete="RESTRICT",
            name="fk_delegations_target_version",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_delegations_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "parent_run_id", "id", name="uq_delegations_parent_id"),
        sa.UniqueConstraint("tenant_id", "child_run_id", name="uq_delegations_child_run"),
        sa.UniqueConstraint(
            "tenant_id",
            "parent_run_id",
            "idempotency_key",
            name="uq_delegations_parent_idempotency",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "parent_run_id",
            "task_fingerprint",
            name="uq_delegations_parent_fingerprint",
        ),
    )
    op.create_index(
        "ix_delegations_parent_status",
        "delegations",
        ["tenant_id", "parent_run_id", "status", "created_at"],
    )

    op.create_table(
        "agent_run_relations",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("ancestor_run_id", sa.Uuid(), nullable=False),
        sa.Column("descendant_run_id", sa.Uuid(), nullable=False),
        sa.Column("delegation_id", sa.Uuid(), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("is_direct", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        *_timestamps(),
        sa.CheckConstraint("ancestor_run_id <> descendant_run_id", name="ck_run_relations_no_self"),
        sa.CheckConstraint("depth > 0", name="ck_run_relations_depth"),
        sa.CheckConstraint("NOT is_direct OR depth = 1", name="ck_run_relations_direct_depth"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "ancestor_run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_run_relations_ancestor",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "descendant_run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_run_relations_descendant",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "delegation_id"],
            ["delegations.tenant_id", "delegations.id"],
            ondelete="RESTRICT",
            name="fk_run_relations_delegation",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_run_relations_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "ancestor_run_id",
            "descendant_run_id",
            name="uq_run_relations_closure",
        ),
    )
    op.create_index(
        "ix_run_relations_descendant_depth",
        "agent_run_relations",
        ["tenant_id", "descendant_run_id", "depth"],
    )
    op.create_index(
        "uq_run_relations_direct_child",
        "agent_run_relations",
        ["tenant_id", "descendant_run_id"],
        unique=True,
        postgresql_where=sa.text("is_direct"),
    )

    op.create_table(
        "agent_messages",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("delegation_id", sa.Uuid(), nullable=False),
        sa.Column("sender_run_id", sa.Uuid(), nullable=False),
        sa.Column("receiver_run_id", sa.Uuid(), nullable=False),
        sa.Column("message_type", sa.String(32), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("refs", postgresql.JSONB(), nullable=False, server_default=JSON_ARRAY),
        sa.Column("visibility", sa.String(32), nullable=False, server_default="sender_receiver"),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.CheckConstraint(
            "message_type IN ('task_assignment', 'progress', 'question', 'answer', 'result', "
            "'critique', 'retry_request', 'cancel', 'system_notice')",
            name="ck_agent_messages_type",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'delivered', 'acknowledged')",
            name="ck_agent_messages_status",
        ),
        sa.CheckConstraint(
            "visibility IN ('sender_receiver', 'delegation_tree', 'parent_only')",
            name="ck_agent_messages_visibility",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "sender_run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_agent_messages_sender",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "receiver_run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_agent_messages_receiver",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "delegation_id"],
            ["delegations.tenant_id", "delegations.id"],
            ondelete="RESTRICT",
            name="fk_agent_messages_delegation",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_agent_messages_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "sender_run_id",
            "idempotency_key",
            name="uq_agent_messages_sender_idempotency",
        ),
    )
    op.create_index(
        "ix_agent_messages_receiver_status",
        "agent_messages",
        ["tenant_id", "receiver_run_id", "status", "created_at"],
    )

    op.create_table(
        "run_budget_ledgers",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("token_limit", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("token_direct_consumed", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("token_child_consumed", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("token_child_reserved", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_limit_microunits", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "cost_direct_consumed_microunits", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "cost_child_consumed_microunits", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "cost_child_reserved_microunits", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("tool_call_limit", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_calls_direct_consumed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_calls_child_consumed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tool_calls_child_reserved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("child_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wall_deadline", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "token_limit >= 0 AND token_direct_consumed >= 0 AND token_child_consumed >= 0 "
            "AND token_child_reserved >= 0",
            name="ck_budget_ledgers_token_nonnegative",
        ),
        sa.CheckConstraint(
            "token_direct_consumed + token_child_consumed + token_child_reserved <= token_limit",
            name="ck_budget_ledgers_token_bound",
        ),
        sa.CheckConstraint(
            "cost_limit_microunits >= 0 AND cost_direct_consumed_microunits >= 0 "
            "AND cost_child_consumed_microunits >= 0 AND cost_child_reserved_microunits >= 0",
            name="ck_budget_ledgers_cost_nonnegative",
        ),
        sa.CheckConstraint(
            "cost_direct_consumed_microunits + cost_child_consumed_microunits + "
            "cost_child_reserved_microunits <= cost_limit_microunits",
            name="ck_budget_ledgers_cost_bound",
        ),
        sa.CheckConstraint(
            "tool_call_limit >= 0 AND tool_calls_direct_consumed >= 0 "
            "AND tool_calls_child_consumed >= 0 AND tool_calls_child_reserved >= 0",
            name="ck_budget_ledgers_tool_nonnegative",
        ),
        sa.CheckConstraint(
            "tool_calls_direct_consumed + tool_calls_child_consumed + "
            "tool_calls_child_reserved <= tool_call_limit",
            name="ck_budget_ledgers_tool_bound",
        ),
        sa.CheckConstraint("child_count >= 0", name="ck_budget_ledgers_child_count"),
        sa.CheckConstraint("revision > 0", name="ck_budget_ledgers_revision"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_budget_ledgers_run",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_budget_ledgers_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "run_id", name="uq_budget_ledgers_tenant_run"),
    )

    op.execute(_GUARD_DELEGATION_FUNCTION)
    op.execute(
        "CREATE TRIGGER guard_delegation_semantics BEFORE UPDATE OR DELETE ON delegations "
        "FOR EACH ROW EXECUTE FUNCTION guard_delegation_semantics()"
    )
    op.execute(_RECONCILE_WAITERS_FUNCTION)
    op.execute("REVOKE ALL ON FUNCTION reconcile_coordination_waiters() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION reconcile_coordination_waiters() TO nico_worker_claimer")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON delegations, agent_messages, run_budget_ledgers "
        "TO nico_runtime"
    )
    op.execute("GRANT SELECT, INSERT ON agent_run_relations TO nico_runtime")
    for table in (
        "delegations",
        "agent_run_relations",
        "agent_messages",
        "run_budget_ledgers",
    ):
        _enable_rls(table)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS reconcile_coordination_waiters()")
    op.execute("DROP TRIGGER guard_delegation_semantics ON delegations")
    op.execute("DROP FUNCTION guard_delegation_semantics()")
    op.drop_table("run_budget_ledgers")
    op.drop_table("agent_messages")
    op.drop_table("agent_run_relations")
    op.drop_table("delegations")
    op.drop_constraint("ck_runs_status", "runs", type_="check")
    op.execute(
        "UPDATE runs SET status = 'paused', lease_owner = NULL, lease_token = NULL, "
        "lease_expires_at = NULL, revision = revision + 1 "
        "WHERE status = 'waiting_for_subagent'"
    )
    op.create_check_constraint(
        "ck_runs_status",
        "runs",
        "status IN ('pending', 'planning', 'running', 'waiting_for_tool', "
        "'waiting_for_approval', 'paused', 'completed', 'failed', 'cancelled', 'timed_out')",
    )
    op.drop_column("runtime_sessions", "coordination_policy_snapshot")
    op.drop_column("agent_versions", "coordination_policy")


_GUARD_DELEGATION_FUNCTION = r"""
CREATE FUNCTION guard_delegation_semantics()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Delegations cannot be deleted'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.parent_run_id IS DISTINCT FROM NEW.parent_run_id
       OR OLD.parent_run_step_id IS DISTINCT FROM NEW.parent_run_step_id
       OR OLD.child_task_id IS DISTINCT FROM NEW.child_task_id
       OR OLD.child_run_id IS DISTINCT FROM NEW.child_run_id
       OR OLD.target_agent_id IS DISTINCT FROM NEW.target_agent_id
       OR OLD.target_agent_version_id IS DISTINCT FROM NEW.target_agent_version_id
       OR OLD.objective IS DISTINCT FROM NEW.objective
       OR OLD.acceptance IS DISTINCT FROM NEW.acceptance
       OR OLD.context_refs IS DISTINCT FROM NEW.context_refs
       OR OLD.execution_mode IS DISTINCT FROM NEW.execution_mode
       OR OLD.budget_grant IS DISTINCT FROM NEW.budget_grant
       OR OLD.policy_snapshot IS DISTINCT FROM NEW.policy_snapshot
       OR OLD.permission_snapshot IS DISTINCT FROM NEW.permission_snapshot
       OR OLD.idempotency_key IS DISTINCT FROM NEW.idempotency_key
       OR OLD.task_fingerprint IS DISTINCT FROM NEW.task_fingerprint
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'Delegation definition is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status IN ('completed', 'failed', 'cancelled', 'rejected') THEN
        RAISE EXCEPTION 'terminal Delegation is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT (
        (OLD.status = 'proposed' AND NEW.status IN ('accepted', 'rejected', 'cancelled'))
        OR (OLD.status = 'accepted'
            AND NEW.status IN ('running', 'completed', 'failed', 'cancelled'))
        OR (OLD.status = 'running' AND NEW.status IN ('completed', 'failed', 'cancelled'))
        OR OLD.status = NEW.status
    ) THEN
        RAISE EXCEPTION 'invalid Delegation lifecycle transition'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$;
"""


_RECONCILE_WAITERS_FUNCTION = r"""
CREATE FUNCTION reconcile_coordination_waiters()
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $function$
DECLARE
    ready_ids uuid[];
    affected integer := 0;
BEGIN
    SELECT array_agg(candidate.id) INTO ready_ids
    FROM (
        SELECT parent.id
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
        FOR UPDATE OF parent SKIP LOCKED
    ) AS candidate;

    IF ready_ids IS NULL THEN
        RETURN 0;
    END IF;
    UPDATE public.runs
    SET status = 'running',
        lease_owner = NULL,
        lease_token = NULL,
        lease_expires_at = NULL,
        heartbeat_at = NULL,
        revision = revision + 1,
        updated_at = clock_timestamp()
    WHERE id = ANY(ready_ids);
    GET DIAGNOSTICS affected = ROW_COUNT;

    UPDATE public.runtime_sessions
    SET status = 'paused',
        provider_state = provider_state || jsonb_build_object(
            'coordination_reconciled_at', clock_timestamp()
        ),
        revision = revision + 1,
        updated_at = clock_timestamp()
    WHERE run_id = ANY(ready_ids)
      AND status = 'suspended';
    RETURN affected;
END;
$function$;
"""
