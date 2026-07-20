"""Add Provider onboarding provenance and durable probes.

Revision ID: 20260720_0020
Revises: 20260719_0019
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260720_0020"
down_revision: str | None = "20260719_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOW = sa.text("now()")
UUID_DEFAULT = sa.text("gen_random_uuid()")
JSON_OBJECT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "provider_probes",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=UUID_DEFAULT),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False, server_default="verify_completion"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("provider_key", sa.String(120), nullable=False),
        sa.Column("protocol", sa.String(50), nullable=False),
        sa.Column("base_url", sa.String(2000), nullable=False),
        sa.Column("credential_ref", sa.String(300), nullable=False),
        sa.Column(
            "provider_options", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT
        ),
        sa.Column("model_name", sa.String(200)),
        sa.Column("catalog_revision", sa.String(64), nullable=False),
        sa.Column("candidate_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_detail", sa.String(500)),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("worker_id", sa.String(200)),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("activation_correlation_id", sa.Uuid()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "kind IN ('discover_models', 'verify_completion')",
            name="ck_provider_probes_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled', 'activated')",
            name="ck_provider_probes_status",
        ),
        sa.CheckConstraint(
            "protocol IN ('openai_compatible', 'anthropic_messages', 'google_gemini')",
            name="ck_provider_probes_protocol",
        ),
        sa.CheckConstraint("length(candidate_hash) = 64", name="ck_provider_probes_hash"),
        sa.CheckConstraint("attempt >= 0", name="ck_provider_probes_attempt"),
        sa.CheckConstraint(
            "credential_ref ~ '^(env:NICO_MODEL_SECRET_[A-Z0-9_]{1,100}|"
            "secret:[A-Za-z0-9._:/-]{1,240})$'",
            name="ck_provider_probes_credential_ref",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(provider_options) = 'object' AND jsonb_typeof(result) = 'object'",
            name="ck_provider_probes_json_shapes",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            ondelete="RESTRICT",
            name="fk_provider_probes_tenant",
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_provider_probes_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_provider_probes_idempotency"),
    )
    op.create_index(
        "ix_provider_probes_claimable",
        "provider_probes",
        ["status", "lease_expires_at", "created_at"],
    )
    op.create_index(
        "ix_provider_probes_tenant_provider",
        "provider_probes",
        ["tenant_id", "provider_key", "created_at"],
    )

    op.drop_constraint("ck_model_endpoints_protocol", "model_endpoints", type_="check")
    op.create_check_constraint(
        "ck_model_endpoints_protocol",
        "model_endpoints",
        "protocol IN ('openai_compatible', 'anthropic_messages', 'google_gemini')",
    )
    op.add_column(
        "model_endpoints",
        sa.Column("provider_key", sa.String(120), nullable=False, server_default="custom"),
    )
    op.add_column("model_endpoints", sa.Column("catalog_revision", sa.String(64)))
    op.add_column(
        "model_endpoints",
        sa.Column(
            "provider_options", postgresql.JSONB(), nullable=False, server_default=JSON_OBJECT
        ),
    )
    op.add_column("model_endpoints", sa.Column("verified_probe_id", sa.Uuid()))
    op.add_column("model_endpoints", sa.Column("verified_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_model_endpoints_provider_key",
        "model_endpoints",
        "provider_key ~ '^[a-z][a-z0-9_-]{1,118}[a-z0-9]$'",
    )
    op.create_check_constraint(
        "ck_model_endpoints_provider_options",
        "model_endpoints",
        "jsonb_typeof(provider_options) = 'object'",
    )
    op.create_check_constraint(
        "ck_model_endpoints_verification",
        "model_endpoints",
        "(verified_probe_id IS NULL) = (verified_at IS NULL)",
    )
    op.create_foreign_key(
        "fk_model_endpoints_verified_probe",
        "model_endpoints",
        "provider_probes",
        ["tenant_id", "verified_probe_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_model_endpoints_tenant_provider",
        "model_endpoints",
        ["tenant_id", "provider_key", "revision"],
    )

    op.execute(_provider_probe_guard())
    op.execute(
        "CREATE TRIGGER guard_provider_probe_write BEFORE UPDATE OR DELETE "
        "ON provider_probes FOR EACH ROW EXECUTE FUNCTION guard_provider_probe_write()"
    )
    op.execute(_expanded_endpoint_guard())

    predicate = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
    op.execute(
        "CREATE POLICY tenant_isolation ON provider_probes "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute("ALTER TABLE provider_probes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE provider_probes FORCE ROW LEVEL SECURITY")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON provider_probes TO nico_runtime")


def downgrade() -> None:
    op.execute(
        """
        DO $block$
        BEGIN
            IF EXISTS (SELECT 1 FROM public.provider_probes LIMIT 1)
               OR EXISTS (
                    SELECT 1 FROM public.model_endpoints
                    WHERE provider_key <> 'custom'
                       OR catalog_revision IS NOT NULL
                       OR provider_options <> '{}'::jsonb
                       OR verified_probe_id IS NOT NULL
                       OR verified_at IS NOT NULL
                    LIMIT 1
               ) THEN
                RAISE EXCEPTION
                    'provider onboarding data exists; restore legacy Agent routes and remove '
                    'feature state before downgrade';
            END IF;
        END;
        $block$;
        """
    )

    op.drop_index("ix_model_endpoints_tenant_provider", table_name="model_endpoints")
    op.drop_constraint("fk_model_endpoints_verified_probe", "model_endpoints", type_="foreignkey")
    op.execute(_legacy_endpoint_guard())
    for constraint in (
        "ck_model_endpoints_verification",
        "ck_model_endpoints_provider_options",
        "ck_model_endpoints_provider_key",
    ):
        op.drop_constraint(constraint, "model_endpoints", type_="check")
    for column in (
        "verified_at",
        "verified_probe_id",
        "provider_options",
        "catalog_revision",
        "provider_key",
    ):
        op.drop_column("model_endpoints", column)
    op.drop_constraint("ck_model_endpoints_protocol", "model_endpoints", type_="check")
    op.create_check_constraint(
        "ck_model_endpoints_protocol",
        "model_endpoints",
        "protocol IN ('openai_compatible')",
    )

    op.execute("DROP TRIGGER guard_provider_probe_write ON provider_probes")
    op.execute("DROP FUNCTION guard_provider_probe_write()")
    op.drop_table("provider_probes")


def _provider_probe_guard() -> str:
    return r"""
    CREATE FUNCTION guard_provider_probe_write()
    RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = pg_catalog, public
    AS $function$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'provider probe facts cannot be deleted';
        END IF;
        IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
           OR NEW.kind IS DISTINCT FROM OLD.kind
           OR NEW.provider_key IS DISTINCT FROM OLD.provider_key
           OR NEW.protocol IS DISTINCT FROM OLD.protocol
           OR NEW.base_url IS DISTINCT FROM OLD.base_url
           OR NEW.credential_ref IS DISTINCT FROM OLD.credential_ref
           OR NEW.provider_options IS DISTINCT FROM OLD.provider_options
           OR NEW.model_name IS DISTINCT FROM OLD.model_name
           OR NEW.catalog_revision IS DISTINCT FROM OLD.catalog_revision
           OR NEW.candidate_hash IS DISTINCT FROM OLD.candidate_hash
           OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'provider probe identity is immutable';
        END IF;
        IF OLD.status IN ('failed', 'cancelled', 'activated') THEN
            RAISE EXCEPTION 'terminal provider probe is immutable';
        END IF;
        IF OLD.status = 'succeeded' THEN
            IF NEW.status <> 'activated'
               OR NEW.activated_at IS NULL
               OR NEW.activation_correlation_id IS NULL
               OR NEW.result IS DISTINCT FROM OLD.result
               OR NEW.error_code IS DISTINCT FROM OLD.error_code
               OR NEW.error_detail IS DISTINCT FROM OLD.error_detail
               OR NEW.completed_at IS DISTINCT FROM OLD.completed_at
               OR NEW.verified_at IS DISTINCT FROM OLD.verified_at THEN
                RAISE EXCEPTION 'succeeded provider probe permits one activation transition';
            END IF;
        END IF;
        IF NEW.revision <> OLD.revision + 1 THEN
            RAISE EXCEPTION 'provider probe revision must increment exactly once';
        END IF;
        RETURN NEW;
    END;
    $function$;
    """


def _expanded_endpoint_guard() -> str:
    return r"""
    CREATE OR REPLACE FUNCTION guard_model_endpoint_semantics()
    RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM public.agent_versions v
            WHERE v.tenant_id = OLD.tenant_id AND v.model_endpoint_id = OLD.id
        ) AND (
            OLD.stable_key IS DISTINCT FROM NEW.stable_key
            OR OLD.revision IS DISTINCT FROM NEW.revision
            OR OLD.protocol IS DISTINCT FROM NEW.protocol
            OR OLD.base_url IS DISTINCT FROM NEW.base_url
            OR OLD.credential_ref IS DISTINCT FROM NEW.credential_ref
            OR OLD.allowed_models IS DISTINCT FROM NEW.allowed_models
            OR OLD.capabilities IS DISTINCT FROM NEW.capabilities
            OR OLD.tls_policy IS DISTINCT FROM NEW.tls_policy
            OR OLD.provider_key IS DISTINCT FROM NEW.provider_key
            OR OLD.catalog_revision IS DISTINCT FROM NEW.catalog_revision
            OR OLD.provider_options IS DISTINCT FROM NEW.provider_options
            OR OLD.verified_probe_id IS DISTINCT FROM NEW.verified_probe_id
            OR OLD.verified_at IS DISTINCT FROM NEW.verified_at
        ) THEN
            RAISE EXCEPTION 'referenced model endpoint semantics are immutable; create a revision'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $function$;
    """


def _legacy_endpoint_guard() -> str:
    return r"""
    CREATE OR REPLACE FUNCTION guard_model_endpoint_semantics()
    RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM public.agent_versions v
            WHERE v.tenant_id = OLD.tenant_id AND v.model_endpoint_id = OLD.id
        ) AND (
            OLD.stable_key IS DISTINCT FROM NEW.stable_key
            OR OLD.revision IS DISTINCT FROM NEW.revision
            OR OLD.protocol IS DISTINCT FROM NEW.protocol
            OR OLD.base_url IS DISTINCT FROM NEW.base_url
            OR OLD.allowed_models IS DISTINCT FROM NEW.allowed_models
            OR OLD.tls_policy IS DISTINCT FROM NEW.tls_policy
        ) THEN
            RAISE EXCEPTION 'referenced model endpoint semantics are immutable; create a revision'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $function$;
    """
