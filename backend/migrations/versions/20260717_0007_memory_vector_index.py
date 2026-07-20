"""Add immutable versioned pgvector chunks for active Memory.

Revision ID: 20260717_0007
Revises: 20260717_0006
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "20260717_0007"
down_revision: str | None = "20260717_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")
UUID_DEFAULT = sa.text("gen_random_uuid()")


def upgrade() -> None:
    op.create_table(
        "memory_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("memory_content_hash", sa.String(64), nullable=False),
        sa.Column("chunker_name", sa.String(120), nullable=False),
        sa.Column("chunker_version", sa.String(80), nullable=False),
        sa.Column("embedding_provider", sa.String(120), nullable=False),
        sa.Column("embedding_version", sa.String(80), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(384), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint("chunk_index >= 0", name="ck_memory_chunks_index"),
        sa.CheckConstraint(
            "start_offset >= 0 AND end_offset > start_offset",
            name="ck_memory_chunks_offsets",
        ),
        sa.CheckConstraint("embedding_dimension = 384", name="ck_memory_chunks_dimension"),
        sa.CheckConstraint(
            "length(content_hash) = 64 AND length(memory_content_hash) = 64",
            name="ck_memory_chunks_hashes",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "memory_id"],
            ["memories.tenant_id", "memories.id"],
            ondelete="RESTRICT",
            name="fk_memory_chunks_tenant_memory",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_memory_chunks_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "memory_id",
            "embedding_provider",
            "embedding_version",
            "chunker_version",
            "chunk_index",
            name="uq_memory_chunks_profile_index",
        ),
    )
    op.create_index(
        "ix_memory_chunks_tenant_memory",
        "memory_chunks",
        ["tenant_id", "memory_id", "embedding_provider", "embedding_version"],
    )
    op.create_index(
        "ix_memory_chunks_embedding_hnsw",
        "memory_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_with={"m": 16, "ef_construction": 64},
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON memory_chunks TO nico_runtime")
    predicate = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    op.execute("ALTER TABLE memory_chunks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE memory_chunks FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON memory_chunks "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute(
        """
        CREATE FUNCTION guard_memory_chunk_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $function$
        DECLARE
            parent_status text;
            parent_hash text;
            parent_expiry timestamptz;
        BEGIN
            SELECT status, content_hash, expires_at
              INTO parent_status, parent_hash, parent_expiry
              FROM public.memories
             WHERE tenant_id = NEW.tenant_id AND id = NEW.memory_id;
            IF parent_status IS DISTINCT FROM 'active'
               OR (parent_expiry IS NOT NULL AND parent_expiry <= clock_timestamp()) THEN
                RAISE EXCEPTION 'memory chunks require active unexpired memory'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF parent_hash IS DISTINCT FROM NEW.memory_content_hash THEN
                RAISE EXCEPTION 'memory chunk profile hash must match memory'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            IF encode(public.digest(convert_to(NEW.content, 'UTF8'), 'sha256'), 'hex')
               <> NEW.content_hash THEN
                RAISE EXCEPTION 'memory chunk content hash mismatch'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(
        "CREATE TRIGGER guard_memory_chunk_insert BEFORE INSERT ON memory_chunks "
        "FOR EACH ROW EXECUTE FUNCTION guard_memory_chunk_insert()"
    )
    op.execute(
        "CREATE TRIGGER guard_memory_chunks_update BEFORE UPDATE ON memory_chunks "
        "FOR EACH ROW EXECUTE FUNCTION reject_growth_delete()"
    )
    op.execute(
        "CREATE TRIGGER guard_memory_chunks_delete BEFORE DELETE ON memory_chunks "
        "FOR EACH ROW EXECUTE FUNCTION reject_growth_delete()"
    )


def downgrade() -> None:
    op.drop_table("memory_chunks")
    op.execute("DROP FUNCTION guard_memory_chunk_insert()")
