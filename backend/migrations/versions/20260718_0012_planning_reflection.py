"""Add versioned Plans, PlanSteps, and runtime evaluations.

Revision ID: 20260718_0012
Revises: 20260718_0011
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0012"
down_revision: str | None = "20260718_0011"
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
        "plans",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("runtime_session_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("supersedes_plan_id", sa.Uuid()),
        sa.Column("created_by_model_call_id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("revision > 0", name="ck_plans_revision"),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'completed', 'failed')", name="ck_plans_status"
        ),
        sa.CheckConstraint("reason IN ('initial', 'replan')", name="ck_plans_reason"),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_plans_content_hash"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_plans_tenant_run",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "runtime_session_id"],
            ["runtime_sessions.tenant_id", "runtime_sessions.run_id", "runtime_sessions.id"],
            ondelete="RESTRICT",
            name="fk_plans_runtime_session",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "supersedes_plan_id"],
            ["plans.tenant_id", "plans.run_id", "plans.id"],
            ondelete="RESTRICT",
            name="fk_plans_supersedes",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "created_by_model_call_id"],
            ["model_calls.tenant_id", "model_calls.run_id", "model_calls.id"],
            ondelete="RESTRICT",
            name="fk_plans_created_by_model_call",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_plans_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "run_id", "id", name="uq_plans_tenant_run_id"),
        sa.UniqueConstraint("tenant_id", "run_id", "revision", name="uq_plans_run_revision"),
    )
    op.create_index("ix_plans_run_revision", "plans", ["tenant_id", "run_id", "revision"])

    op.create_table(
        "plan_steps",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("step_key", sa.String(64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("instruction", sa.Text(), nullable=False),
        sa.Column("acceptance", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("dependencies", postgresql.JSONB(), nullable=False, server_default=JSON_ARRAY),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("run_step_id", sa.Uuid()),
        sa.Column("output", postgresql.JSONB()),
        sa.Column("output_hash", sa.String(64)),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=False, server_default=JSON_ARRAY),
        sa.Column("error", postgresql.JSONB()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.CheckConstraint("position > 0", name="ck_plan_steps_position"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'skipped')",
            name="ck_plan_steps_status",
        ),
        sa.CheckConstraint(
            "output_hash IS NULL OR length(output_hash) = 64", name="ck_plan_steps_output_hash"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "plan_id"],
            ["plans.tenant_id", "plans.run_id", "plans.id"],
            ondelete="RESTRICT",
            name="fk_plan_steps_plan",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "run_step_id"],
            ["run_steps.tenant_id", "run_steps.run_id", "run_steps.id"],
            ondelete="RESTRICT",
            name="fk_plan_steps_run_step",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_plan_steps_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "run_id", "id", name="uq_plan_steps_tenant_run_id"),
        sa.UniqueConstraint(
            "tenant_id", "run_id", "plan_id", "step_key", name="uq_plan_steps_plan_key"
        ),
        sa.UniqueConstraint(
            "tenant_id", "run_id", "plan_id", "position", name="uq_plan_steps_plan_position"
        ),
    )
    op.create_index(
        "ix_plan_steps_plan_position",
        "plan_steps",
        ["tenant_id", "run_id", "plan_id", "position"],
    )

    op.create_table(
        "runtime_evaluations",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid()),
        sa.Column("plan_step_id", sa.Uuid()),
        sa.Column("model_call_id", sa.Uuid()),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("evaluation_type", sa.String(32), nullable=False),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="completed"),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("output_hash", sa.String(64)),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=False, server_default=JSON_ARRAY),
        sa.Column("result", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("error", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "evaluation_type IN ('step_validation', 'reflection', 'completion')",
            name="ck_runtime_evaluations_type",
        ),
        sa.CheckConstraint(
            "method IN ('deterministic', 'model')", name="ck_runtime_evaluations_method"
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'failed')", name="ck_runtime_evaluations_status"
        ),
        sa.CheckConstraint(
            "verdict IN ('passed', 'failed', 'retry', 'replan', 'complete', 'continue')",
            name="ck_runtime_evaluations_verdict",
        ),
        sa.CheckConstraint("sequence > 0", name="ck_runtime_evaluations_sequence"),
        sa.CheckConstraint("length(input_hash) = 64", name="ck_runtime_evaluations_input_hash"),
        sa.CheckConstraint(
            "output_hash IS NULL OR length(output_hash) = 64",
            name="ck_runtime_evaluations_output_hash",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_runtime_evaluations_run",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "plan_id"],
            ["plans.tenant_id", "plans.run_id", "plans.id"],
            ondelete="RESTRICT",
            name="fk_runtime_evaluations_plan",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "plan_step_id"],
            ["plan_steps.tenant_id", "plan_steps.run_id", "plan_steps.id"],
            ondelete="RESTRICT",
            name="fk_runtime_evaluations_plan_step",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "model_call_id"],
            ["model_calls.tenant_id", "model_calls.run_id", "model_calls.id"],
            ondelete="RESTRICT",
            name="fk_runtime_evaluations_model_call",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_runtime_evaluations_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "run_id", "id", name="uq_runtime_evaluations_tenant_run_id"
        ),
        sa.UniqueConstraint(
            "tenant_id", "run_id", "sequence", name="uq_runtime_evaluations_run_sequence"
        ),
    )
    op.create_index(
        "ix_runtime_evaluations_run_sequence",
        "runtime_evaluations",
        ["tenant_id", "run_id", "sequence"],
    )

    op.execute(_GUARD_PLAN_FUNCTION)
    op.execute(_GUARD_PLAN_STEP_FUNCTION)
    op.execute(_REJECT_EVALUATION_CHANGE_FUNCTION)
    op.execute(
        "CREATE TRIGGER guard_plan_semantics BEFORE UPDATE OR DELETE ON plans "
        "FOR EACH ROW EXECUTE FUNCTION guard_plan_semantics()"
    )
    op.execute(
        "CREATE TRIGGER guard_plan_step_semantics BEFORE UPDATE OR DELETE ON plan_steps "
        "FOR EACH ROW EXECUTE FUNCTION guard_plan_step_semantics()"
    )
    op.execute(
        "CREATE TRIGGER guard_runtime_evaluation_immutable BEFORE UPDATE OR DELETE "
        "ON runtime_evaluations FOR EACH ROW EXECUTE FUNCTION reject_runtime_evaluation_change()"
    )

    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON plans, plan_steps TO nico_runtime")
    op.execute("GRANT SELECT, INSERT ON runtime_evaluations TO nico_runtime")
    for table in ("plans", "plan_steps", "runtime_evaluations"):
        _enable_rls(table)


def downgrade() -> None:
    op.execute("DROP TRIGGER guard_runtime_evaluation_immutable ON runtime_evaluations")
    op.execute("DROP FUNCTION reject_runtime_evaluation_change()")
    op.execute("DROP TRIGGER guard_plan_step_semantics ON plan_steps")
    op.execute("DROP FUNCTION guard_plan_step_semantics()")
    op.execute("DROP TRIGGER guard_plan_semantics ON plans")
    op.execute("DROP FUNCTION guard_plan_semantics()")
    op.drop_table("runtime_evaluations")
    op.drop_table("plan_steps")
    op.drop_table("plans")


_GUARD_PLAN_FUNCTION = r"""
CREATE FUNCTION guard_plan_semantics()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Plan revisions are append-only'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.run_id IS DISTINCT FROM NEW.run_id
       OR OLD.runtime_session_id IS DISTINCT FROM NEW.runtime_session_id
       OR OLD.revision IS DISTINCT FROM NEW.revision
       OR OLD.reason IS DISTINCT FROM NEW.reason
       OR OLD.objective IS DISTINCT FROM NEW.objective
       OR OLD.supersedes_plan_id IS DISTINCT FROM NEW.supersedes_plan_id
       OR OLD.created_by_model_call_id IS DISTINCT FROM NEW.created_by_model_call_id
       OR OLD.content_hash IS DISTINCT FROM NEW.content_hash
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'Plan revision semantics are immutable; create a new revision'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status IN ('superseded', 'completed', 'failed') THEN
        RAISE EXCEPTION 'terminal Plan revision is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$;
"""

_GUARD_PLAN_STEP_FUNCTION = r"""
CREATE FUNCTION guard_plan_step_semantics()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Plan steps cannot be deleted'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.run_id IS DISTINCT FROM NEW.run_id
       OR OLD.plan_id IS DISTINCT FROM NEW.plan_id
       OR OLD.step_key IS DISTINCT FROM NEW.step_key
       OR OLD.position IS DISTINCT FROM NEW.position
       OR OLD.title IS DISTINCT FROM NEW.title
       OR OLD.instruction IS DISTINCT FROM NEW.instruction
       OR OLD.acceptance IS DISTINCT FROM NEW.acceptance
       OR OLD.dependencies IS DISTINCT FROM NEW.dependencies
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'Plan step definition is immutable; create a new Plan revision'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status IN ('completed', 'failed', 'skipped') THEN
        RAISE EXCEPTION 'terminal Plan step is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$;
"""

_REJECT_EVALUATION_CHANGE_FUNCTION = r"""
CREATE FUNCTION reject_runtime_evaluation_change()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
BEGIN
    RAISE EXCEPTION 'runtime evaluation facts are immutable'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$function$;
"""
