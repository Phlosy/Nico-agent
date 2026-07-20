"""Record provider compatibility and legacy resolver telemetry.

Revision ID: 20260718_0016
Revises: 20260718_0015
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260718_0016"
down_revision: str | None = "20260718_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runtime_sessions",
        sa.Column("provider_resolution_source", sa.String(64)),
    )
    op.add_column(
        "runtime_sessions",
        sa.Column("legacy_resolver_used", sa.Boolean()),
    )
    op.add_column(
        "runtime_sessions",
        sa.Column(
            "provider_compatibility",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_runtime_sessions_resolution_source",
        "runtime_sessions",
        "provider_resolution_source IS NULL OR provider_resolution_source IN ("
        "'agent_version', 'legacy_run_config', 'legacy_model_config', "
        "'legacy_default_mock', 'persisted_session')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_runtime_sessions_resolution_source",
        "runtime_sessions",
        type_="check",
    )
    op.drop_column("runtime_sessions", "provider_compatibility")
    op.drop_column("runtime_sessions", "legacy_resolver_used")
    op.drop_column("runtime_sessions", "provider_resolution_source")
