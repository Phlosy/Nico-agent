"""Index Projects that are due for managed supervision.

Revision ID: 20260721_0023
Revises: 20260721_0022
Create Date: 2026-07-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260721_0023"
down_revision: str | None = "20260721_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_projects_due_supervision",
        "projects",
        ["next_supervision_at", "id"],
        postgresql_where=sa.text(
            "status = 'active' AND kind = 'shared' "
            "AND supervision_cadence_seconds IS NOT NULL "
            "AND next_supervision_at IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_projects_due_supervision", table_name="projects")
