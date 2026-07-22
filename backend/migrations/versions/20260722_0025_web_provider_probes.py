"""Allow durable Web Provider verification probes.

Revision ID: 20260722_0025
Revises: 20260721_0024
Create Date: 2026-07-22
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260722_0025"
down_revision: str | None = "20260721_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_provider_probes_kind", "provider_probes", type_="check")
    op.create_check_constraint(
        "ck_provider_probes_kind",
        "provider_probes",
        "kind IN ('discover_models', 'verify_completion', 'verify_web')",
    )
    op.drop_constraint("ck_provider_probes_protocol", "provider_probes", type_="check")
    op.create_check_constraint(
        "ck_provider_probes_protocol",
        "provider_probes",
        "protocol IN ('openai_compatible', 'anthropic_messages', 'google_gemini', "
        "'web_search')",
    )
    op.drop_constraint(
        "ck_provider_probes_credential_ref",
        "provider_probes",
        type_="check",
    )
    op.create_check_constraint(
        "ck_provider_probes_credential_ref",
        "provider_probes",
        "credential_ref = 'none' OR credential_ref ~ "
        "'^(env:NICO_(MODEL|TOOL)_SECRET_[A-Z0-9_]{1,100}|"
        "secret:[A-Za-z0-9._:/-]{1,240})$'",
    )


def downgrade() -> None:
    op.execute("ALTER TABLE provider_probes DISABLE TRIGGER guard_provider_probe_write")
    op.execute("DELETE FROM provider_probes WHERE kind = 'verify_web'")
    op.execute("ALTER TABLE provider_probes ENABLE TRIGGER guard_provider_probe_write")
    op.drop_constraint(
        "ck_provider_probes_credential_ref",
        "provider_probes",
        type_="check",
    )
    op.create_check_constraint(
        "ck_provider_probes_credential_ref",
        "provider_probes",
        "credential_ref ~ '^(env:NICO_MODEL_SECRET_[A-Z0-9_]{1,100}|"
        "secret:[A-Za-z0-9._:/-]{1,240})$'",
    )
    op.drop_constraint("ck_provider_probes_protocol", "provider_probes", type_="check")
    op.create_check_constraint(
        "ck_provider_probes_protocol",
        "provider_probes",
        "protocol IN ('openai_compatible', 'anthropic_messages', 'google_gemini')",
    )
    op.drop_constraint("ck_provider_probes_kind", "provider_probes", type_="check")
    op.create_check_constraint(
        "ck_provider_probes_kind",
        "provider_probes",
        "kind IN ('discover_models', 'verify_completion')",
    )
