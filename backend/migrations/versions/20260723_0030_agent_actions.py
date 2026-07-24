"""Persist provider-neutral AgentAction batches and repair relations.

Revision ID: 20260723_0030
Revises: 20260723_0029
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260723_0030"
down_revision: str | None = "20260723_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")
UUID_DEFAULT = sa.text("gen_random_uuid()")
JSON_OBJECT = sa.text("'{}'::jsonb")


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
        "agent_action_batches",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("runtime_session_id", sa.Uuid(), nullable=False),
        sa.Column("context_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("model_call_id", sa.Uuid(), nullable=False),
        sa.Column("run_step_id", sa.Uuid(), nullable=False),
        sa.Column("replay_of_batch_id", sa.Uuid()),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("parse_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("batch_key", sa.String(64), nullable=False),
        sa.Column("source_format", sa.String(32), nullable=False),
        sa.Column("response_hash", sa.String(64), nullable=False),
        sa.Column("action_count", sa.Integer(), nullable=False),
        sa.Column("dispatch_cursor", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint("schema_version > 0", name="ck_agent_action_batches_schema_version"),
        sa.CheckConstraint("parse_revision > 0", name="ck_agent_action_batches_parse_revision"),
        sa.CheckConstraint("action_count > 0", name="ck_agent_action_batches_action_count"),
        sa.CheckConstraint(
            "dispatch_cursor >= 0 AND dispatch_cursor <= action_count",
            name="ck_agent_action_batches_dispatch_cursor",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'dispatching', 'completed', 'failed')",
            name="ck_agent_action_batches_status",
        ),
        sa.CheckConstraint(
            "source_format IN "
            "('structured_json', 'plain_json', 'provider_tool_calls', 'legacy_plain_text')",
            name="ck_agent_action_batches_source_format",
        ),
        sa.CheckConstraint("length(batch_key) = 64", name="ck_agent_action_batches_key"),
        sa.CheckConstraint(
            "length(response_hash) = 64", name="ck_agent_action_batches_response_hash"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_agent_action_batches_run",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "runtime_session_id"],
            ["runtime_sessions.tenant_id", "runtime_sessions.run_id", "runtime_sessions.id"],
            ondelete="RESTRICT",
            name="fk_agent_action_batches_session",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "context_snapshot_id"],
            ["context_snapshots.tenant_id", "context_snapshots.run_id", "context_snapshots.id"],
            ondelete="RESTRICT",
            name="fk_agent_action_batches_context",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "model_call_id"],
            ["model_calls.tenant_id", "model_calls.run_id", "model_calls.id"],
            ondelete="RESTRICT",
            name="fk_agent_action_batches_model_call",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "run_step_id"],
            ["run_steps.tenant_id", "run_steps.run_id", "run_steps.id"],
            ondelete="RESTRICT",
            name="fk_agent_action_batches_run_step",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_agent_action_batches_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "run_id", "id", name="uq_agent_action_batches_tenant_run_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "model_call_id",
            name="uq_agent_action_batches_model_call",
        ),
    )
    op.create_foreign_key(
        "fk_agent_action_batches_replay_of",
        "agent_action_batches",
        "agent_action_batches",
        ["tenant_id", "run_id", "replay_of_batch_id"],
        ["tenant_id", "run_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_agent_action_batches_run_created",
        "agent_action_batches",
        ["tenant_id", "run_id", "created_at"],
    )

    op.create_table(
        "agent_actions",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("action_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("provider_call_id", sa.String(300)),
        sa.Column("tool_name", sa.String(120)),
        sa.Column(
            "intent_redacted",
            postgresql.JSONB(),
            nullable=False,
            server_default=JSON_OBJECT,
        ),
        sa.Column("completion", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("arguments_hash", sa.String(64)),
        sa.Column("question_hash", sa.String(64)),
        sa.Column("reason_hash", sa.String(64)),
        sa.Column("compatibility_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("outcome_ref", sa.String(500)),
        sa.Column("observation_ref", sa.String(500)),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint("ordinal >= 0", name="ck_agent_actions_ordinal"),
        sa.CheckConstraint(
            "kind IN ('final', 'tool_call', 'ask_user')", name="ck_agent_actions_kind"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'dispatched', 'succeeded', 'failed', 'blocked')",
            name="ck_agent_actions_status",
        ),
        sa.CheckConstraint("length(action_id) = 64", name="ck_agent_actions_action_id"),
        sa.CheckConstraint(
            "content_hash IS NULL OR length(content_hash) = 64",
            name="ck_agent_actions_content_hash",
        ),
        sa.CheckConstraint(
            "arguments_hash IS NULL OR length(arguments_hash) = 64",
            name="ck_agent_actions_arguments_hash",
        ),
        sa.CheckConstraint(
            "question_hash IS NULL OR length(question_hash) = 64",
            name="ck_agent_actions_question_hash",
        ),
        sa.CheckConstraint(
            "reason_hash IS NULL OR length(reason_hash) = 64",
            name="ck_agent_actions_reason_hash",
        ),
        sa.CheckConstraint(
            "octet_length(intent_redacted::text) <= 32768",
            name="ck_agent_actions_intent_bound",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "batch_id"],
            [
                "agent_action_batches.tenant_id",
                "agent_action_batches.run_id",
                "agent_action_batches.id",
            ],
            ondelete="RESTRICT",
            name="fk_agent_actions_batch",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_agent_actions_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "run_id", "id", name="uq_agent_actions_tenant_run_id"),
        sa.UniqueConstraint(
            "tenant_id", "run_id", "batch_id", "ordinal", name="uq_agent_actions_batch_ordinal"
        ),
        sa.UniqueConstraint(
            "tenant_id", "run_id", "batch_id", "action_id", name="uq_agent_actions_batch_action"
        ),
    )
    op.create_index(
        "ix_agent_actions_batch",
        "agent_actions",
        ["tenant_id", "run_id", "batch_id", "ordinal"],
    )

    op.create_table(
        "agent_action_repairs",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("runtime_session_id", sa.Uuid(), nullable=False),
        sa.Column("source_model_call_id", sa.Uuid(), nullable=False),
        sa.Column("source_batch_id", sa.Uuid()),
        sa.Column("result_batch_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("repair_ordinal", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("reason_code", sa.String(120), nullable=False),
        sa.Column("observation_ref", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint("repair_ordinal > 0", name="ck_agent_action_repairs_ordinal"),
        sa.CheckConstraint(
            "kind IN ('post_model_call_commit', 'parse_correction', "
            "'clarification_correction', 'semantic_final_correction', 'replay')",
            name="ck_agent_action_repairs_kind",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_agent_action_repairs_run",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "runtime_session_id"],
            ["runtime_sessions.tenant_id", "runtime_sessions.run_id", "runtime_sessions.id"],
            ondelete="RESTRICT",
            name="fk_agent_action_repairs_session",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "source_model_call_id"],
            ["model_calls.tenant_id", "model_calls.run_id", "model_calls.id"],
            ondelete="RESTRICT",
            name="fk_agent_action_repairs_source_model_call",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "source_batch_id"],
            [
                "agent_action_batches.tenant_id",
                "agent_action_batches.run_id",
                "agent_action_batches.id",
            ],
            ondelete="RESTRICT",
            name="fk_agent_action_repairs_source_batch",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "result_batch_id"],
            [
                "agent_action_batches.tenant_id",
                "agent_action_batches.run_id",
                "agent_action_batches.id",
            ],
            ondelete="RESTRICT",
            name="fk_agent_action_repairs_result_batch",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_agent_action_repairs_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "source_model_call_id",
            "kind",
            "repair_ordinal",
            name="uq_agent_action_repairs_source_kind_ordinal",
        ),
    )
    op.create_index(
        "ix_agent_action_repairs_run_created",
        "agent_action_repairs",
        ["tenant_id", "run_id", "created_at"],
    )

    for statement in (
        _GUARD_BATCH_FUNCTION,
        _GUARD_ACTION_FUNCTION,
        _REJECT_REPAIR_CHANGE_FUNCTION,
    ):
        op.execute(statement)
    op.execute(
        "CREATE TRIGGER guard_agent_action_batch BEFORE UPDATE OR DELETE "
        "ON agent_action_batches FOR EACH ROW EXECUTE FUNCTION guard_agent_action_batch()"
    )
    op.execute(
        "CREATE TRIGGER guard_agent_action_intent BEFORE UPDATE OR DELETE "
        "ON agent_actions FOR EACH ROW EXECUTE FUNCTION guard_agent_action_intent()"
    )
    op.execute(
        "CREATE TRIGGER guard_agent_action_repair BEFORE UPDATE OR DELETE "
        "ON agent_action_repairs FOR EACH ROW EXECUTE FUNCTION reject_agent_action_repair_change()"
    )

    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON agent_action_batches, agent_actions, "
        "agent_action_repairs TO nico_runtime"
    )
    for table in ("agent_action_batches", "agent_actions", "agent_action_repairs"):
        _enable_rls(table)


def downgrade() -> None:
    op.execute("DROP TRIGGER guard_agent_action_repair ON agent_action_repairs")
    op.execute("DROP FUNCTION reject_agent_action_repair_change()")
    op.execute("DROP TRIGGER guard_agent_action_intent ON agent_actions")
    op.execute("DROP FUNCTION guard_agent_action_intent()")
    op.execute("DROP TRIGGER guard_agent_action_batch ON agent_action_batches")
    op.execute("DROP FUNCTION guard_agent_action_batch()")
    op.drop_table("agent_action_repairs")
    op.drop_table("agent_actions")
    op.drop_constraint(
        "fk_agent_action_batches_replay_of", "agent_action_batches", type_="foreignkey"
    )
    op.drop_table("agent_action_batches")


_GUARD_BATCH_FUNCTION = r"""
CREATE FUNCTION guard_agent_action_batch()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'AgentAction batch facts are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF ROW(
        OLD.tenant_id, OLD.run_id, OLD.runtime_session_id, OLD.context_snapshot_id,
        OLD.model_call_id, OLD.run_step_id, OLD.replay_of_batch_id, OLD.schema_version,
        OLD.parse_revision, OLD.batch_key, OLD.source_format, OLD.response_hash, OLD.action_count
    ) IS DISTINCT FROM ROW(
        NEW.tenant_id, NEW.run_id, NEW.runtime_session_id, NEW.context_snapshot_id,
        NEW.model_call_id, NEW.run_step_id, NEW.replay_of_batch_id, NEW.schema_version,
        NEW.parse_revision, NEW.batch_key, NEW.source_format, NEW.response_hash, NEW.action_count
    ) THEN
        RAISE EXCEPTION 'AgentAction batch identity is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.dispatch_cursor < OLD.dispatch_cursor THEN
        RAISE EXCEPTION 'AgentAction dispatch cursor cannot move backwards'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status IN ('completed', 'failed') AND ROW(
        NEW.status, NEW.dispatch_cursor, NEW.completed_at
    ) IS DISTINCT FROM ROW(
        OLD.status, OLD.dispatch_cursor, OLD.completed_at
    ) THEN
        RAISE EXCEPTION 'terminal AgentAction batch outcome is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$;
"""

_GUARD_ACTION_FUNCTION = r"""
CREATE FUNCTION guard_agent_action_intent()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'AgentAction facts are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF ROW(
        OLD.tenant_id, OLD.run_id, OLD.batch_id, OLD.ordinal, OLD.action_id, OLD.kind,
        OLD.provider_call_id, OLD.tool_name, OLD.intent_redacted, OLD.completion,
        OLD.content_hash, OLD.arguments_hash, OLD.question_hash, OLD.reason_hash,
        OLD.compatibility_mode
    ) IS DISTINCT FROM ROW(
        NEW.tenant_id, NEW.run_id, NEW.batch_id, NEW.ordinal, NEW.action_id, NEW.kind,
        NEW.provider_call_id, NEW.tool_name, NEW.intent_redacted, NEW.completion,
        NEW.content_hash, NEW.arguments_hash, NEW.question_hash, NEW.reason_hash,
        NEW.compatibility_mode
    ) THEN
        RAISE EXCEPTION 'AgentAction intent and source are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status IN ('succeeded', 'failed', 'blocked') AND ROW(
        NEW.status, NEW.outcome_ref, NEW.observation_ref, NEW.completed_at
    ) IS DISTINCT FROM ROW(
        OLD.status, OLD.outcome_ref, OLD.observation_ref, OLD.completed_at
    ) THEN
        RAISE EXCEPTION 'terminal AgentAction outcome is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$;
"""

_REJECT_REPAIR_CHANGE_FUNCTION = r"""
CREATE FUNCTION reject_agent_action_repair_change()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
BEGIN
    RAISE EXCEPTION 'AgentAction repair relations are immutable'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$function$;
"""
