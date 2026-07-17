"""Add concurrency-safe idempotency keys for growth evaluations and reviews.

Revision ID: 20260717_0008
Revises: 20260717_0007
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260717_0008"
down_revision: str | None = "20260717_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION reject_growth_source_update()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog, public
        AS $function$
        BEGIN
            RAISE EXCEPTION 'growth sources are immutable'
                USING ERRCODE = 'integrity_constraint_violation';
        END;
        $function$
        """
    )
    op.execute(
        "CREATE TRIGGER guard_growth_sources_update BEFORE UPDATE ON growth_sources "
        "FOR EACH ROW EXECUTE FUNCTION reject_growth_source_update()"
    )
    op.create_index(
        "uq_evaluations_memory_profile",
        "evaluations",
        [
            "tenant_id",
            "memory_id",
            "evaluator_name",
            "evaluator_version",
            "content_hash",
        ],
        unique=True,
        postgresql_where=sa.text("subject_type = 'memory'"),
    )
    op.create_index(
        "uq_evaluations_skill_profile",
        "evaluations",
        [
            "tenant_id",
            "skill_version_id",
            "evaluator_name",
            "evaluator_version",
            "content_hash",
        ],
        unique=True,
        postgresql_where=sa.text("subject_type = 'skill_version'"),
    )
    op.create_index(
        "uq_approvals_memory_requested",
        "approvals",
        ["tenant_id", "memory_id", "action", "content_hash"],
        unique=True,
        postgresql_where=sa.text("subject_type = 'memory' AND status = 'requested'"),
    )
    op.create_index(
        "uq_approvals_skill_requested",
        "approvals",
        ["tenant_id", "skill_version_id", "action", "content_hash"],
        unique=True,
        postgresql_where=sa.text("subject_type = 'skill_version' AND status = 'requested'"),
    )


def downgrade() -> None:
    op.drop_index("uq_approvals_skill_requested", table_name="approvals")
    op.drop_index("uq_approvals_memory_requested", table_name="approvals")
    op.drop_index("uq_evaluations_skill_profile", table_name="evaluations")
    op.drop_index("uq_evaluations_memory_profile", table_name="evaluations")
    op.execute("DROP TRIGGER guard_growth_sources_update ON growth_sources")
    op.execute("DROP FUNCTION reject_growth_source_update()")
