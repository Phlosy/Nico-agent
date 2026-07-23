"""Persist Agent-scoped chat defaults.

Revision ID: 20260723_0028
Revises: 20260723_0027
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260723_0028"
down_revision: str | None = "20260723_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "default_approval_mode",
            sa.String(32),
            nullable=False,
            server_default="ask",
        ),
    )
    op.add_column(
        "agents",
        sa.Column("approval_owner_actor_id", sa.String(200), nullable=True),
    )
    op.execute(
        """
        WITH owners AS (
            SELECT DISTINCT ON (agent.tenant_id, agent.id)
                agent.tenant_id,
                agent.id AS agent_id,
                audit.actor_id
            FROM agents AS agent
            JOIN audit_records AS audit
              ON audit.tenant_id = agent.tenant_id
             AND (
                    (
                        audit.action IN ('agent.create', 'agent.clone')
                        AND
                        audit.resource_type = 'agent'
                        AND audit.resource_id = agent.id
                    )
                    OR (
                        audit.action IN (
                            'agent_capabilities.activate',
                            'web_provider_activation.complete'
                        )
                        AND audit.details -> 'result' ->> 'agent_id' = agent.id::text
                    )
                 )
            ORDER BY
                agent.tenant_id,
                agent.id,
                CASE
                    WHEN audit.action IN ('agent.create', 'agent.clone') THEN 0
                    ELSE 1
                END,
                audit.sequence
        )
        UPDATE agents AS agent
        SET approval_owner_actor_id = owners.actor_id
        FROM owners
        WHERE owners.tenant_id = agent.tenant_id
          AND owners.agent_id = agent.id
        """
    )
    op.execute(
        """
        UPDATE conversations
        SET approval_mode = 'ask',
            revision = revision + 1
        WHERE approval_mode <> 'ask'
        """
    )
    op.create_check_constraint(
        "ck_agents_default_approval_mode",
        "agents",
        "default_approval_mode IN ('ask', 'auto-medium', 'auto-all')",
    )
    op.create_index(
        "ix_conversations_agent",
        "conversations",
        ["tenant_id", "agent_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_agent", table_name="conversations")
    op.drop_constraint(
        "ck_agents_default_approval_mode",
        "agents",
        type_="check",
    )
    op.drop_column("agents", "approval_owner_actor_id")
    op.drop_column("agents", "default_approval_mode")
