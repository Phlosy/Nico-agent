"""Add recoverable ReAct step and checkpoint metadata.

Revision ID: 20260718_0011
Revises: 20260718_0010
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260718_0011"
down_revision: str | None = "20260718_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("checkpoint_schema_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "runs",
        sa.Column("checkpoint_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("runs", sa.Column("checkpoint_hash", sa.String(64)))
    op.create_check_constraint("ck_runs_checkpoint_schema", "runs", "checkpoint_schema_version > 0")
    op.create_check_constraint("ck_runs_checkpoint_revision", "runs", "checkpoint_revision >= 0")
    op.create_check_constraint(
        "ck_runs_checkpoint_hash",
        "runs",
        "checkpoint_hash IS NULL OR length(checkpoint_hash) = 64",
    )
    op.execute(
        """
        UPDATE runs
        SET checkpoint_schema_version = CASE
                WHEN checkpoint IS NOT NULL
                 AND checkpoint->>'schema_version' ~ '^[1-9][0-9]*$'
                THEN (checkpoint->>'schema_version')::integer
                ELSE 1
            END,
            checkpoint_revision = CASE WHEN checkpoint IS NULL THEN 0 ELSE 1 END,
            checkpoint_hash = CASE
                WHEN checkpoint->>'checkpoint_hash' ~ '^[0-9a-f]{64}$'
                THEN checkpoint->>'checkpoint_hash'
                ELSE NULL
            END
        """
    )

    op.add_column("model_calls", sa.Column("replay_of_model_call_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_model_calls_replay_of",
        "model_calls",
        "model_calls",
        ["tenant_id", "run_id", "replay_of_model_call_id"],
        ["tenant_id", "run_id", "id"],
        ondelete="RESTRICT",
    )

    op.add_column(
        "runtime_sessions",
        sa.Column("checkpoint_schema_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "runtime_sessions",
        sa.Column("checkpoint_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("runtime_sessions", sa.Column("checkpoint_hash", sa.String(64)))
    op.create_check_constraint(
        "ck_runtime_sessions_checkpoint_schema",
        "runtime_sessions",
        "checkpoint_schema_version > 0",
    )
    op.create_check_constraint(
        "ck_runtime_sessions_checkpoint_revision",
        "runtime_sessions",
        "checkpoint_revision >= 0",
    )
    op.create_check_constraint(
        "ck_runtime_sessions_checkpoint_hash",
        "runtime_sessions",
        "checkpoint_hash IS NULL OR length(checkpoint_hash) = 64",
    )
    op.execute(
        """
        UPDATE runtime_sessions
        SET checkpoint_schema_version = CASE
                WHEN checkpoint IS NOT NULL
                 AND checkpoint->>'schema_version' ~ '^[1-9][0-9]*$'
                THEN (checkpoint->>'schema_version')::integer
                ELSE 1
            END,
            checkpoint_revision = CASE WHEN checkpoint IS NULL THEN 0 ELSE 1 END,
            checkpoint_hash = CASE
                WHEN checkpoint->>'checkpoint_hash' ~ '^[0-9a-f]{64}$'
                THEN checkpoint->>'checkpoint_hash'
                ELSE NULL
            END
        """
    )

    for column in (
        sa.Column("step_key", sa.String(200)),
        sa.Column("step_type", sa.String(32)),
        sa.Column("iteration", sa.Integer()),
        sa.Column("parent_step_id", sa.Uuid()),
        sa.Column("context_snapshot_id", sa.Uuid()),
        sa.Column("model_call_id", sa.Uuid()),
    ):
        op.add_column("run_steps", column)
    op.create_check_constraint(
        "ck_run_steps_step_type",
        "run_steps",
        "step_type IS NULL OR step_type IN ('reasoning', 'tool', 'observation', "
        "'planning', 'reflection', 'delegation', 'aggregation')",
    )
    op.create_check_constraint(
        "ck_run_steps_iteration",
        "run_steps",
        "iteration IS NULL OR iteration > 0",
    )
    op.create_unique_constraint(
        "uq_run_steps_step_key",
        "run_steps",
        ["tenant_id", "run_id", "step_key"],
    )
    op.create_foreign_key(
        "fk_run_steps_parent",
        "run_steps",
        "run_steps",
        ["tenant_id", "run_id", "parent_step_id"],
        ["tenant_id", "run_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_run_steps_context_snapshot",
        "run_steps",
        "context_snapshots",
        ["tenant_id", "run_id", "context_snapshot_id"],
        ["tenant_id", "run_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_run_steps_model_call",
        "run_steps",
        "model_calls",
        ["tenant_id", "run_id", "model_call_id"],
        ["tenant_id", "run_id", "id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_run_steps_model_call", "run_steps", type_="foreignkey")
    op.drop_constraint("fk_run_steps_context_snapshot", "run_steps", type_="foreignkey")
    op.drop_constraint("fk_run_steps_parent", "run_steps", type_="foreignkey")
    op.drop_constraint("uq_run_steps_step_key", "run_steps", type_="unique")
    op.drop_constraint("ck_run_steps_iteration", "run_steps", type_="check")
    op.drop_constraint("ck_run_steps_step_type", "run_steps", type_="check")
    for column in (
        "model_call_id",
        "context_snapshot_id",
        "parent_step_id",
        "iteration",
        "step_type",
        "step_key",
    ):
        op.drop_column("run_steps", column)

    op.drop_constraint("ck_runtime_sessions_checkpoint_hash", "runtime_sessions", type_="check")
    op.drop_constraint("ck_runtime_sessions_checkpoint_revision", "runtime_sessions", type_="check")
    op.drop_constraint("ck_runtime_sessions_checkpoint_schema", "runtime_sessions", type_="check")
    op.drop_column("runtime_sessions", "checkpoint_hash")
    op.drop_column("runtime_sessions", "checkpoint_revision")
    op.drop_column("runtime_sessions", "checkpoint_schema_version")

    op.drop_constraint("fk_model_calls_replay_of", "model_calls", type_="foreignkey")
    op.drop_column("model_calls", "replay_of_model_call_id")

    op.drop_constraint("ck_runs_checkpoint_hash", "runs", type_="check")
    op.drop_constraint("ck_runs_checkpoint_revision", "runs", type_="check")
    op.drop_constraint("ck_runs_checkpoint_schema", "runs", type_="check")
    op.drop_column("runs", "checkpoint_hash")
    op.drop_column("runs", "checkpoint_revision")
    op.drop_column("runs", "checkpoint_schema_version")
