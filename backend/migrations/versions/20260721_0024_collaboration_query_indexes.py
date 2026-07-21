"""Index collaboration timeline and supervision projections.

Revision ID: 20260721_0024
Revises: 20260721_0023
Create Date: 2026-07-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260721_0024"
down_revision: str | None = "20260721_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_events_tenant_run_sequence",
        "events",
        ["tenant_id", "run_id", "sequence"],
    )
    op.create_index(
        "ix_artifacts_project_available",
        "artifacts",
        ["tenant_id", "project_id", sa.text("created_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("status = 'available'"),
    )


def downgrade() -> None:
    op.drop_index("ix_artifacts_project_available", table_name="artifacts")
    op.drop_index("ix_events_tenant_run_sequence", table_name="events")
