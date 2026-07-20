"""Expose a token-free maintenance correlation check for Provider activation.

Revision ID: 20260720_0021
Revises: 20260720_0020
Create Date: 2026-07-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260720_0021"
down_revision: str | None = "20260720_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE FUNCTION provider_maintenance_attempt_active(p_attempt_id uuid)
        RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public
        AS $function$
            SELECT EXISTS (
                SELECT 1 FROM public.deployment_maintenance
                WHERE singleton AND attempt_id = p_attempt_id
                  AND lease_expires_at > clock_timestamp()
            )
        $function$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION provider_maintenance_attempt_active(uuid) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION provider_maintenance_attempt_active(uuid) TO nico_runtime"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION provider_maintenance_attempt_active(uuid)")
