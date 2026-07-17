"""Add database guards for reviewed Skill release and scoped canaries.

Revision ID: 20260717_0009
Revises: 20260717_0008
Create Date: 2026-07-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260717_0009"
down_revision: str | None = "20260717_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(_SKILL_VERSION_PUBLISH_POLICY_SQL)
    op.execute(
        "CREATE TRIGGER guard_skill_version_publish_policy "
        "BEFORE UPDATE ON skill_versions FOR EACH ROW "
        "WHEN (OLD.status IS DISTINCT FROM NEW.status AND NEW.status = 'published') "
        "EXECUTE FUNCTION guard_skill_version_publish_policy()"
    )
    op.execute(_SKILL_DEPLOYMENT_INSERT_POLICY_SQL)
    op.execute(
        "CREATE TRIGGER guard_skill_deployment_insert_policy "
        "BEFORE INSERT ON skill_deployments FOR EACH ROW "
        "EXECUTE FUNCTION guard_skill_deployment_insert_policy()"
    )
    op.execute(_SKILL_STOP_POLICY_SQL)
    op.execute(
        "CREATE TRIGGER guard_skill_stop_policy BEFORE UPDATE ON skills "
        "FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status AND "
        "NEW.status IN ('deprecated', 'disabled')) "
        "EXECUTE FUNCTION guard_skill_stop_policy()"
    )
    op.execute(_SKILL_POINTER_POLICY_SQL)
    op.execute(
        "CREATE TRIGGER guard_skill_pointer_policy BEFORE UPDATE ON skills "
        "FOR EACH ROW WHEN (OLD.current_version_id IS DISTINCT FROM NEW.current_version_id) "
        "EXECUTE FUNCTION guard_skill_pointer_policy()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER guard_skill_pointer_policy ON skills")
    op.execute("DROP FUNCTION guard_skill_pointer_policy()")
    op.execute("DROP TRIGGER guard_skill_stop_policy ON skills")
    op.execute("DROP FUNCTION guard_skill_stop_policy()")
    op.execute("DROP TRIGGER guard_skill_deployment_insert_policy ON skill_deployments")
    op.execute("DROP FUNCTION guard_skill_deployment_insert_policy()")
    op.execute("DROP TRIGGER guard_skill_version_publish_policy ON skill_versions")
    op.execute("DROP FUNCTION guard_skill_version_publish_policy()")


_SKILL_VERSION_PUBLISH_POLICY_SQL = r"""
CREATE FUNCTION guard_skill_version_publish_policy()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.growth_sources g
         WHERE g.tenant_id = NEW.tenant_id AND g.skill_version_id = NEW.id
    ) THEN
        RAISE EXCEPTION 'skill publication requires immutable source evidence'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT EXISTS (
        SELECT 1
          FROM (
              SELECT e.status, e.verdict
                FROM public.evaluations e
               WHERE e.tenant_id = NEW.tenant_id
                 AND e.skill_version_id = NEW.id
                 AND e.content_hash = NEW.content_hash
                 AND e.status IN ('completed', 'failed')
               ORDER BY e.created_at DESC, e.id DESC
               LIMIT 1
          ) latest
         WHERE latest.status = 'completed' AND latest.verdict = 'pass'
    ) THEN
        RAISE EXCEPTION 'latest terminal skill evaluation must pass'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.approvals a
         WHERE a.tenant_id = NEW.tenant_id
           AND a.skill_version_id = NEW.id
           AND a.content_hash = NEW.content_hash
           AND a.action = 'publish'
           AND a.status = 'approved'
           AND a.reviewer IS NOT NULL
           AND a.reviewer <> a.requester
           AND (a.expires_at IS NULL OR a.expires_at > clock_timestamp())
    ) THEN
        RAISE EXCEPTION 'skill publication requires independent unexpired approval'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$
"""


_SKILL_DEPLOYMENT_INSERT_POLICY_SQL = r"""
CREATE FUNCTION guard_skill_deployment_insert_policy()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
DECLARE
    owner public.skills%ROWTYPE;
BEGIN
    SELECT * INTO owner FROM public.skills
     WHERE tenant_id = NEW.tenant_id AND id = NEW.skill_id;
    IF NOT FOUND OR owner.status <> 'published' OR owner.current_version_id IS NULL THEN
        RAISE EXCEPTION 'canary deployment requires a published stable skill'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT (
        owner.scope_type = 'tenant'
        OR (owner.scope_type = 'project' AND NEW.scope_type = 'project'
            AND owner.project_id = NEW.project_id)
        OR (owner.scope_type = 'agent' AND NEW.scope_type = 'agent'
            AND owner.agent_id = NEW.agent_id)
    ) THEN
        RAISE EXCEPTION 'canary deployment cannot widen skill scope'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$
"""


_SKILL_STOP_POLICY_SQL = r"""
CREATE FUNCTION guard_skill_stop_policy()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.skill_deployments d
         WHERE d.tenant_id = NEW.tenant_id AND d.skill_id = NEW.id
           AND d.status = 'active'
    ) THEN
        RAISE EXCEPTION 'active canaries must be retired before stopping a skill'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$
"""


_SKILL_POINTER_POLICY_SQL = r"""
CREATE FUNCTION guard_skill_pointer_policy()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $function$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.skill_deployments d
         WHERE d.tenant_id = NEW.tenant_id AND d.skill_id = NEW.id
           AND d.status = 'active'
    ) THEN
        RAISE EXCEPTION 'active canaries must be retired before switching stable pointer'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$
"""
