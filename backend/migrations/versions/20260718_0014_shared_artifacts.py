"""Add content-addressed artifacts and explicit parent/child sharing links.

Revision ID: 20260718_0014
Revises: 20260718_0013
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0014"
down_revision: str | None = "20260718_0013"
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
        "artifacts",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_run_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("content_type", sa.String(200), nullable=False),
        sa.Column("artifact_type", sa.String(64), nullable=False, server_default="file"),
        sa.Column("status", sa.String(32), nullable=False, server_default="uploading"),
        sa.Column("temp_object_key", sa.String(1000)),
        sa.Column("object_key", sa.String(1000)),
        sa.Column("sha256", sa.String(64)),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("error", postgresql.JSONB()),
        sa.Column("available_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('uploading', 'available', 'failed', 'deleted')",
            name="ck_artifacts_status",
        ),
        sa.CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="ck_artifacts_size"),
        sa.CheckConstraint("sha256 IS NULL OR length(sha256) = 64", name="ck_artifacts_sha256"),
        sa.CheckConstraint(
            "status <> 'available' OR "
            "(sha256 IS NOT NULL AND size_bytes IS NOT NULL AND object_key IS NOT NULL)",
            name="ck_artifacts_available_content",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            ondelete="RESTRICT",
            name="fk_artifacts_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner_run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_artifacts_owner_run",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_artifacts_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "id", "owner_run_id", name="uq_artifacts_owner_id"),
        sa.UniqueConstraint(
            "tenant_id", "owner_run_id", "idempotency_key", name="uq_artifacts_run_idempotency"
        ),
    )
    op.create_index(
        "ix_artifacts_owner_created", "artifacts", ["tenant_id", "owner_run_id", "created_at"]
    )
    op.create_index("ix_artifacts_hash", "artifacts", ["tenant_id", "sha256"])

    op.create_table(
        "shared_artifact_links",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("owner_run_id", sa.Uuid(), nullable=False),
        sa.Column("grantee_run_id", sa.Uuid(), nullable=False),
        sa.Column("visibility", sa.String(32), nullable=False),
        sa.Column("purpose", sa.String(120), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "visibility IN ('parent', 'child')", name="ck_shared_artifact_links_visibility"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked', 'expired')", name="ck_shared_artifact_links_status"
        ),
        sa.CheckConstraint("owner_run_id <> grantee_run_id", name="ck_shared_artifact_links_runs"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "artifact_id", "owner_run_id"],
            ["artifacts.tenant_id", "artifacts.id", "artifacts.owner_run_id"],
            ondelete="RESTRICT",
            name="fk_shared_artifact_links_artifact_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "grantee_run_id"],
            ["runs.tenant_id", "runs.id"],
            ondelete="RESTRICT",
            name="fk_shared_artifact_links_grantee",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_shared_artifact_links_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "artifact_id",
            "grantee_run_id",
            "purpose",
            name="uq_shared_artifact_links_grant",
        ),
    )
    op.create_index(
        "ix_shared_artifact_links_grantee",
        "shared_artifact_links",
        ["tenant_id", "grantee_run_id", "status", "created_at"],
    )
    op.execute(_GUARD_ARTIFACT_FUNCTION)
    op.execute(
        "CREATE TRIGGER guard_artifact_semantics BEFORE UPDATE OR DELETE ON artifacts "
        "FOR EACH ROW EXECUTE FUNCTION guard_artifact_semantics()"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON artifacts, shared_artifact_links TO nico_runtime")
    for table in ("artifacts", "shared_artifact_links"):
        _enable_rls(table)


def downgrade() -> None:
    op.execute("DROP TRIGGER guard_artifact_semantics ON artifacts")
    op.execute("DROP FUNCTION guard_artifact_semantics()")
    op.drop_table("shared_artifact_links")
    op.drop_table("artifacts")


_GUARD_ARTIFACT_FUNCTION = r"""
CREATE FUNCTION guard_artifact_semantics()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Artifacts cannot be deleted; mark them deleted'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
       OR OLD.project_id IS DISTINCT FROM NEW.project_id
       OR OLD.owner_run_id IS DISTINCT FROM NEW.owner_run_id
       OR OLD.name IS DISTINCT FROM NEW.name
       OR OLD.content_type IS DISTINCT FROM NEW.content_type
       OR OLD.artifact_type IS DISTINCT FROM NEW.artifact_type
       OR OLD.idempotency_key IS DISTINCT FROM NEW.idempotency_key
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'Artifact ownership and definition are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status IN ('available', 'failed', 'deleted') THEN
        RAISE EXCEPTION 'terminal Artifact metadata is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$;
"""
