"""Add staged Conversation attachments for bounded context materialization.

Revision ID: 20260719_0018
Revises: 20260719_0017
Create Date: 2026-07-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260719_0018"
down_revision: str | None = "20260719_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")
UUID_DEFAULT = sa.text("gen_random_uuid()")


def upgrade() -> None:
    op.create_table(
        "conversation_attachments",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("content_type", sa.String(200), nullable=False),
        sa.Column("object_key", sa.String(1000), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="staged"),
        sa.Column("uploaded_by", sa.String(200), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_by_turn_id", sa.Uuid()),
        sa.Column("artifact_id", sa.Uuid()),
        sa.Column("text_excerpt", sa.Text()),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "status IN ('staged', 'consumed', 'deleted', 'expired')",
            name="ck_conversation_attachments_status",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_conversation_attachments_size"),
        sa.CheckConstraint("length(sha256) = 64", name="ck_conversation_attachments_sha256"),
        sa.CheckConstraint(
            "status <> 'consumed' OR (consumed_by_turn_id IS NOT NULL AND artifact_id IS NOT NULL)",
            name="ck_conversation_attachments_consumed",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            ondelete="RESTRICT",
            name="fk_conversation_attachments_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "consumed_by_turn_id"],
            ["conversation_turns.tenant_id", "conversation_turns.id"],
            ondelete="RESTRICT",
            name="fk_conversation_attachments_turn",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "artifact_id"],
            ["artifacts.tenant_id", "artifacts.id"],
            ondelete="RESTRICT",
            name="fk_conversation_attachments_artifact",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_conversation_attachments_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "conversation_id",
            "idempotency_key",
            name="uq_conversation_attachments_idempotency",
        ),
    )
    op.create_index(
        "ix_conversation_attachments_staged",
        "conversation_attachments",
        ["tenant_id", "conversation_id", "status", "created_at"],
    )
    predicate = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(
        "CREATE POLICY tenant_isolation ON conversation_attachments "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute("ALTER TABLE conversation_attachments ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE conversation_attachments FORCE ROW LEVEL SECURITY")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON conversation_attachments TO nico_runtime")


def downgrade() -> None:
    op.drop_table("conversation_attachments")
