"""Freeze published Memory/Skill context and track its runtime effect.

Revision ID: 20260718_0015
Revises: 20260718_0014
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0015"
down_revision: str | None = "20260718_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")
UUID_DEFAULT = sa.text("gen_random_uuid()")
JSON_OBJECT = sa.text("'{}'::jsonb")
JSON_ARRAY = sa.text("'[]'::jsonb")


def _enable_rls(table: str) -> None:
    predicate = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(
        f'CREATE POLICY tenant_isolation ON "{table}" USING ({predicate}) WITH CHECK ({predicate})'
    )
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')


def upgrade() -> None:
    op.add_column(
        "runtime_sessions",
        sa.Column(
            "knowledge_policy_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=JSON_OBJECT,
        ),
    )
    op.add_column(
        "runtime_sessions",
        sa.Column(
            "knowledge_selection_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=JSON_OBJECT,
        ),
    )
    for name, default in (
        ("memory_refs", JSON_ARRAY),
        ("skill_refs", JSON_ARRAY),
        ("effect_metadata", JSON_OBJECT),
    ):
        op.add_column(
            "context_snapshots",
            sa.Column(name, postgresql.JSONB(), nullable=False, server_default=default),
        )

    op.create_table(
        "runtime_knowledge_usages",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("runtime_session_id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("memory_id", sa.Uuid()),
        sa.Column("skill_id", sa.Uuid()),
        sa.Column("skill_version_id", sa.Uuid()),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("scope_type", sa.String(32), nullable=False),
        sa.Column("selection", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("status", sa.String(32), nullable=False, server_default="selected"),
        sa.Column("first_context_snapshot_id", sa.Uuid()),
        sa.Column("first_model_call_id", sa.Uuid()),
        sa.Column("context_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model_call_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("outcome_status", sa.String(32)),
        sa.Column("result_hash", sa.String(64)),
        sa.Column(
            "effect_metadata", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT
        ),
        sa.Column("first_consumed_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "source_type IN ('memory', 'skill_version')",
            name="ck_runtime_knowledge_usages_source_type",
        ),
        sa.CheckConstraint(
            "(source_type = 'memory' AND memory_id IS NOT NULL AND "
            "skill_id IS NULL AND skill_version_id IS NULL) OR "
            "(source_type = 'skill_version' AND memory_id IS NULL AND "
            "skill_id IS NOT NULL AND skill_version_id IS NOT NULL)",
            name="ck_runtime_knowledge_usages_source",
        ),
        sa.CheckConstraint(
            "status IN ('selected', 'consumed', 'succeeded', 'failed', 'cancelled', 'timed_out')",
            name="ck_runtime_knowledge_usages_status",
        ),
        sa.CheckConstraint("source_version > 0", name="ck_runtime_knowledge_usages_version"),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_runtime_knowledge_usages_hash"),
        sa.CheckConstraint(
            "result_hash IS NULL OR length(result_hash) = 64",
            name="ck_runtime_knowledge_usages_result_hash",
        ),
        sa.CheckConstraint(
            "context_count >= 0 AND model_call_count >= 0",
            name="ck_runtime_knowledge_usages_counts",
        ),
        sa.CheckConstraint("revision > 0", name="ck_runtime_knowledge_usages_revision"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_runtime_knowledge_usages_run",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "runtime_session_id"],
            ["runtime_sessions.tenant_id", "runtime_sessions.run_id", "runtime_sessions.id"],
            ondelete="RESTRICT",
            name="fk_runtime_knowledge_usages_session",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "memory_id"],
            ["memories.tenant_id", "memories.id"],
            ondelete="RESTRICT",
            name="fk_runtime_knowledge_usages_memory",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "skill_id", "skill_version_id"],
            ["skill_versions.tenant_id", "skill_versions.skill_id", "skill_versions.id"],
            ondelete="RESTRICT",
            name="fk_runtime_knowledge_usages_skill_version",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "first_context_snapshot_id"],
            ["context_snapshots.tenant_id", "context_snapshots.run_id", "context_snapshots.id"],
            ondelete="RESTRICT",
            name="fk_runtime_knowledge_usages_first_context",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id", "first_model_call_id"],
            ["model_calls.tenant_id", "model_calls.run_id", "model_calls.id"],
            ondelete="RESTRICT",
            name="fk_runtime_knowledge_usages_first_model_call",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_runtime_knowledge_usages_tenant_id_id"),
    )
    op.create_index(
        "uq_runtime_knowledge_usages_run_memory",
        "runtime_knowledge_usages",
        ["tenant_id", "run_id", "memory_id"],
        unique=True,
        postgresql_where=sa.text("source_type = 'memory'"),
    )
    op.create_index(
        "uq_runtime_knowledge_usages_run_skill",
        "runtime_knowledge_usages",
        ["tenant_id", "run_id", "skill_version_id"],
        unique=True,
        postgresql_where=sa.text("source_type = 'skill_version'"),
    )
    op.create_index(
        "ix_runtime_knowledge_usages_run",
        "runtime_knowledge_usages",
        ["tenant_id", "run_id", "status", "created_at"],
    )
    op.execute(_GUARD_KNOWLEDGE_USAGE_FUNCTION)
    op.execute(
        "CREATE TRIGGER guard_runtime_knowledge_usage "
        "BEFORE INSERT OR UPDATE OR DELETE ON runtime_knowledge_usages "
        "FOR EACH ROW EXECUTE FUNCTION guard_runtime_knowledge_usage()"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON runtime_knowledge_usages TO nico_runtime")
    _enable_rls("runtime_knowledge_usages")


def downgrade() -> None:
    op.execute("DROP TRIGGER guard_runtime_knowledge_usage ON runtime_knowledge_usages")
    op.execute("DROP FUNCTION guard_runtime_knowledge_usage()")
    op.drop_table("runtime_knowledge_usages")
    for name in ("effect_metadata", "skill_refs", "memory_refs"):
        op.drop_column("context_snapshots", name)
    op.drop_column("runtime_sessions", "knowledge_selection_snapshot")
    op.drop_column("runtime_sessions", "knowledge_policy_snapshot")


_GUARD_KNOWLEDGE_USAGE_FUNCTION = r"""
CREATE FUNCTION guard_runtime_knowledge_usage()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
DECLARE
    actual_status text;
    identity_status text;
    actual_version integer;
    actual_hash text;
    actual_expiry timestamptz;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'runtime knowledge usage facts cannot be deleted'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.source_type = 'memory' THEN
            SELECT status, version, content_hash, expires_at
            INTO actual_status, actual_version, actual_hash, actual_expiry
            FROM public.memories
            WHERE tenant_id = NEW.tenant_id AND id = NEW.memory_id;
            IF actual_status IS DISTINCT FROM 'active'
               OR actual_expiry IS NOT NULL AND actual_expiry <= clock_timestamp() THEN
                RAISE EXCEPTION 'only active non-expired Memory can enter runtime context'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        ELSE
            SELECT version.status, skill.status, version.version, version.content_hash
            INTO actual_status, identity_status, actual_version, actual_hash
            FROM public.skill_versions AS version
            JOIN public.skills AS skill
              ON skill.tenant_id = version.tenant_id AND skill.id = version.skill_id
            WHERE version.tenant_id = NEW.tenant_id
              AND version.skill_id = NEW.skill_id
              AND version.id = NEW.skill_version_id;
            IF actual_status IS DISTINCT FROM 'published'
               OR identity_status IS DISTINCT FROM 'published' THEN
                RAISE EXCEPTION 'only published SkillVersion can enter runtime context'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END IF;
        IF actual_version IS DISTINCT FROM NEW.source_version
           OR actual_hash IS DISTINCT FROM NEW.content_hash THEN
            RAISE EXCEPTION 'runtime knowledge version or hash does not match source'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.run_id IS DISTINCT FROM NEW.run_id
       OR OLD.runtime_session_id IS DISTINCT FROM NEW.runtime_session_id
       OR OLD.source_type IS DISTINCT FROM NEW.source_type
       OR OLD.memory_id IS DISTINCT FROM NEW.memory_id
       OR OLD.skill_id IS DISTINCT FROM NEW.skill_id
       OR OLD.skill_version_id IS DISTINCT FROM NEW.skill_version_id
       OR OLD.source_version IS DISTINCT FROM NEW.source_version
       OR OLD.content_hash IS DISTINCT FROM NEW.content_hash
       OR OLD.scope_type IS DISTINCT FROM NEW.scope_type
       OR OLD.selection IS DISTINCT FROM NEW.selection
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'runtime knowledge selection is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status IN ('succeeded', 'failed', 'cancelled', 'timed_out') THEN
        RAISE EXCEPTION 'terminal runtime knowledge effect is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT (
        OLD.status = NEW.status
        OR (OLD.status = 'selected' AND NEW.status IN (
            'consumed', 'succeeded', 'failed', 'cancelled', 'timed_out'
        ))
        OR (OLD.status = 'consumed' AND NEW.status IN (
            'succeeded', 'failed', 'cancelled', 'timed_out'
        ))
    ) THEN
        RAISE EXCEPTION 'invalid runtime knowledge lifecycle transition'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.context_count < OLD.context_count
       OR NEW.model_call_count < OLD.model_call_count
       OR OLD.first_context_snapshot_id IS NOT NULL
          AND NEW.first_context_snapshot_id IS DISTINCT FROM OLD.first_context_snapshot_id
       OR OLD.first_model_call_id IS NOT NULL
          AND NEW.first_model_call_id IS DISTINCT FROM OLD.first_model_call_id THEN
        RAISE EXCEPTION 'runtime knowledge consumption counters are monotonic'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$;
"""
