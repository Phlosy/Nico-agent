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
        "deployment_maintenance",
        sa.Column("singleton", sa.Boolean(), primary_key=True, server_default=sa.true()),
        sa.Column("attempt_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint("singleton", name="ck_deployment_maintenance_singleton"),
        sa.CheckConstraint("length(token_hash) = 64", name="ck_deployment_maintenance_token_hash"),
    )
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
    op.execute(_claim_next_provider_probe())
    op.execute("REVOKE ALL ON FUNCTION claim_next_provider_probe(text, integer) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION claim_next_provider_probe(text, integer) TO nico_worker_claimer"
    )
    for function_definition in _maintenance_functions():
        op.execute(function_definition)
    for signature in (
        "acquire_runtime_maintenance(uuid, text, integer)",
        "renew_runtime_maintenance(uuid, text, integer)",
        "release_runtime_maintenance(uuid, text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(_run_maintenance_guard())
    op.execute("REVOKE ALL ON FUNCTION guard_run_insert_during_maintenance() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER guard_run_insert_during_maintenance BEFORE INSERT ON runs "
        "FOR EACH STATEMENT EXECUTE FUNCTION guard_run_insert_during_maintenance()"
    )
    op.execute(_claim_next_run(maintenance_aware=True))
    op.execute("REVOKE ALL ON FUNCTION claim_next_run(text, integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION claim_next_run(text, integer) TO nico_worker_claimer")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS guard_run_insert_during_maintenance ON runs")
    op.execute("DROP FUNCTION IF EXISTS guard_run_insert_during_maintenance()")
    op.execute(_claim_next_run(maintenance_aware=False))
    op.execute("REVOKE ALL ON FUNCTION claim_next_run(text, integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION claim_next_run(text, integer) TO nico_worker_claimer")
    for signature in (
        "release_runtime_maintenance(uuid, text)",
        "renew_runtime_maintenance(uuid, text, integer)",
        "acquire_runtime_maintenance(uuid, text, integer)",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute("DROP FUNCTION IF EXISTS claim_next_provider_probe(text, integer)")
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
    op.execute("DROP TABLE IF EXISTS deployment_maintenance")


def _claim_next_provider_probe() -> str:
    return r"""
    CREATE FUNCTION claim_next_provider_probe(p_worker_id text, p_lease_seconds integer)
    RETURNS TABLE(probe_id uuid, tenant_id uuid, lease_token text, previous_status text)
    LANGUAGE plpgsql
    SECURITY DEFINER
    SET search_path = pg_catalog, public
    AS $function$
    DECLARE
        claimed_at timestamptz := clock_timestamp();
    BEGIN
        IF p_worker_id IS NULL OR length(p_worker_id) < 1 OR length(p_worker_id) > 200 THEN
            RAISE EXCEPTION 'worker_id must contain between 1 and 200 characters';
        END IF;
        IF p_lease_seconds < 30 OR p_lease_seconds > 3600 THEN
            RAISE EXCEPTION 'lease_seconds must be between 30 and 3600';
        END IF;

        RETURN QUERY
        WITH candidate AS (
            SELECT p.id, p.status::text AS previous_status
            FROM public.provider_probes AS p
            WHERE p.status = 'pending'
               OR (p.status = 'running' AND p.lease_expires_at <= claimed_at)
            ORDER BY p.created_at, p.id
            FOR UPDATE OF p SKIP LOCKED
            LIMIT 1
        ), claimed AS (
            UPDATE public.provider_probes AS p
            SET status = 'running',
                worker_id = p_worker_id,
                lease_token = gen_random_uuid()::text,
                lease_expires_at = claimed_at + make_interval(secs => p_lease_seconds),
                started_at = COALESCE(p.started_at, claimed_at),
                attempt = p.attempt + 1,
                revision = p.revision + 1,
                updated_at = claimed_at
            FROM candidate AS c
            WHERE p.id = c.id
            RETURNING p.id, p.tenant_id, p.lease_token, c.previous_status
        )
        SELECT c.id, c.tenant_id, c.lease_token::text, c.previous_status
        FROM claimed AS c;
    END;
    $function$;
    """


def _maintenance_functions() -> tuple[str, ...]:
    definitions = r"""
    CREATE FUNCTION acquire_runtime_maintenance(
        p_attempt_id uuid, p_token text, p_lease_seconds integer
    )
    RETURNS TABLE(active_run_count bigint, lease_expires_at timestamptz)
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
    AS $function$
    DECLARE
        v_now timestamptz := clock_timestamp();
        run_count bigint;
        expires_at timestamptz;
    BEGIN
        IF p_attempt_id IS NULL OR p_token IS NULL OR length(p_token) < 32
           OR length(p_token) > 500 THEN
            RAISE EXCEPTION 'invalid runtime maintenance capability';
        END IF;
        IF p_lease_seconds < 30 OR p_lease_seconds > 900 THEN
            RAISE EXCEPTION 'maintenance lease_seconds must be between 30 and 900';
        END IF;
        PERFORM pg_advisory_xact_lock(hashtextextended('nico:runtime-maintenance', 0));
        DELETE FROM public.deployment_maintenance
        WHERE singleton AND deployment_maintenance.lease_expires_at <= v_now;

        IF EXISTS (SELECT 1 FROM public.deployment_maintenance WHERE singleton) THEN
            IF EXISTS (
                SELECT 1 FROM public.deployment_maintenance
                WHERE singleton AND attempt_id = p_attempt_id
                  AND token_hash = encode(public.digest(p_token, 'sha256'), 'hex')
            ) THEN
                UPDATE public.deployment_maintenance
                SET lease_expires_at = v_now + make_interval(secs => p_lease_seconds),
                    updated_at = v_now
                WHERE singleton
                RETURNING deployment_maintenance.lease_expires_at INTO expires_at;
                RETURN QUERY SELECT 0::bigint, expires_at;
                RETURN;
            END IF;
            RAISE EXCEPTION 'RUNTIME_MAINTENANCE_ALREADY_ACTIVE';
        END IF;

        SELECT count(*) INTO run_count FROM public.runs
        WHERE status NOT IN ('completed', 'failed', 'cancelled', 'timed_out');
        IF run_count > 0 THEN
            RETURN QUERY SELECT run_count, NULL::timestamptz;
            RETURN;
        END IF;
        expires_at := v_now + make_interval(secs => p_lease_seconds);
        INSERT INTO public.deployment_maintenance(
            singleton, attempt_id, token_hash, lease_expires_at, created_at, updated_at
        ) VALUES (
            true, p_attempt_id, encode(public.digest(p_token, 'sha256'), 'hex'),
            expires_at, v_now, v_now
        );
        RETURN QUERY SELECT 0::bigint, expires_at;
    END;
    $function$;

    CREATE FUNCTION renew_runtime_maintenance(
        p_attempt_id uuid, p_token text, p_lease_seconds integer
    ) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
    AS $function$
    DECLARE v_now timestamptz := clock_timestamp();
    BEGIN
        IF p_lease_seconds < 30 OR p_lease_seconds > 900 THEN
            RETURN false;
        END IF;
        PERFORM pg_advisory_xact_lock(hashtextextended('nico:runtime-maintenance', 0));
        UPDATE public.deployment_maintenance
        SET lease_expires_at = v_now + make_interval(secs => p_lease_seconds),
            updated_at = v_now
        WHERE singleton AND attempt_id = p_attempt_id
          AND lease_expires_at > v_now
          AND token_hash = encode(public.digest(p_token, 'sha256'), 'hex');
        RETURN FOUND;
    END;
    $function$;

    CREATE FUNCTION release_runtime_maintenance(p_attempt_id uuid, p_token text)
    RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
    AS $function$
    BEGIN
        PERFORM pg_advisory_xact_lock(hashtextextended('nico:runtime-maintenance', 0));
        DELETE FROM public.deployment_maintenance
        WHERE singleton AND attempt_id = p_attempt_id
          AND token_hash = encode(public.digest(p_token, 'sha256'), 'hex');
        RETURN FOUND;
    END;
    $function$;
    """
    return tuple(
        f"{definition.strip()}\n$function$;"
        for definition in definitions.split("$function$;")
        if definition.strip()
    )


def _run_maintenance_guard() -> str:
    return r"""
    CREATE FUNCTION guard_run_insert_during_maintenance()
    RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
    AS $function$
    BEGIN
        PERFORM pg_advisory_xact_lock(hashtextextended('nico:runtime-maintenance', 0));
        IF EXISTS (
            SELECT 1 FROM public.deployment_maintenance
            WHERE singleton AND lease_expires_at > clock_timestamp()
        ) THEN
            RAISE EXCEPTION 'RUNTIME_MAINTENANCE'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NULL;
    END;
    $function$;
    """


def _claim_next_run(*, maintenance_aware: bool) -> str:
    guard = ""
    if maintenance_aware:
        guard = r"""
            PERFORM pg_advisory_xact_lock(hashtextextended('nico:runtime-maintenance', 0));
            IF EXISTS (
                SELECT 1 FROM public.deployment_maintenance
                WHERE singleton AND lease_expires_at > claimed_at
            ) THEN
                RETURN;
            END IF;
        """
    return f"""
    CREATE OR REPLACE FUNCTION claim_next_run(p_worker_id text, p_lease_seconds integer)
    RETURNS TABLE(run_id uuid, tenant_id uuid, lease_token uuid, previous_status text)
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public
    AS $function$
    DECLARE claimed_at timestamptz := clock_timestamp();
    BEGIN
        IF p_worker_id IS NULL OR length(p_worker_id) < 1 OR length(p_worker_id) > 200 THEN
            RAISE EXCEPTION 'worker_id must contain between 1 and 200 characters';
        END IF;
        IF p_lease_seconds < 5 OR p_lease_seconds > 3600 THEN
            RAISE EXCEPTION 'lease_seconds must be between 5 and 3600';
        END IF;
        {guard}
        RETURN QUERY
        WITH candidate AS (
            SELECT r.id FROM public.runs AS r
            JOIN public.tasks AS t ON t.tenant_id = r.tenant_id AND t.id = r.task_id
            WHERE r.status IN ('pending', 'planning', 'running', 'waiting_for_tool')
              AND (r.lease_expires_at IS NULL OR r.lease_expires_at <= claimed_at)
            ORDER BY t.priority DESC, r.created_at, r.id
            FOR UPDATE OF r SKIP LOCKED LIMIT 1
        ), claimed AS (
            UPDATE public.runs AS r
            SET lease_owner = p_worker_id, lease_token = gen_random_uuid(),
                lease_expires_at = claimed_at + make_interval(secs => p_lease_seconds),
                heartbeat_at = claimed_at, updated_at = claimed_at
            FROM candidate AS c WHERE r.id = c.id
            RETURNING r.id, r.tenant_id, r.lease_token, r.status
        )
        SELECT c.id, c.tenant_id, c.lease_token, c.status::text FROM claimed AS c;
    END;
    $function$;
    """


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
