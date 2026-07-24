"""Converge endpoint capabilities on the structured AgentAction protocol.

Revision ID: 20260724_0032
Revises: 20260723_0031
Create Date: 2026-07-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260724_0032"
down_revision: str | None = "20260723_0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The CTE computes the complete target value before updating. The
    # inequality guard makes reruns no-ops and the audit row records only
    # endpoints whose capability projection actually changed. The endpoint
    # guard is suspended only for this transaction-scoped data migration;
    # normal application writes remain protected before and after it.
    op.execute(
        "ALTER TABLE model_endpoints DISABLE TRIGGER guard_model_endpoint_semantics"
    )
    op.execute(
        r"""
        WITH projected AS (
            SELECT
                id,
                tenant_id,
                capabilities AS old_capabilities,
                (
                    (
                        CASE
                            WHEN provider_key = 'deepseek'
                            THEN capabilities - 'json_schema'
                            ELSE capabilities
                        END
                    ) - 'tools' - 'structured_output'
                    || CASE
                        WHEN capabilities ->> 'tools' = 'true'
                        THEN '{"native_tool_calling": true}'::jsonb
                        ELSE '{}'::jsonb
                    END
                    || CASE
                        WHEN provider_key <> 'deepseek'
                          AND capabilities ->> 'structured_output' = 'true'
                        THEN '{"json_schema": true}'::jsonb
                        ELSE '{}'::jsonb
                    END
                    || CASE
                        WHEN provider_key = 'deepseek'
                        THEN '{"json_object": true}'::jsonb
                        ELSE '{}'::jsonb
                    END
                ) AS new_capabilities,
                provider_key
            FROM model_endpoints
        ),
        changed AS (
            UPDATE model_endpoints AS endpoint
            SET
                capabilities = projected.new_capabilities,
                catalog_revision = CASE
                    WHEN projected.provider_key = 'deepseek' THEN '2026-07-24'
                    ELSE endpoint.catalog_revision
                END,
                updated_at = now()
            FROM projected
            WHERE endpoint.id = projected.id
              AND (
                  endpoint.capabilities IS DISTINCT FROM projected.new_capabilities
                  OR (
                      projected.provider_key = 'deepseek'
                      AND endpoint.catalog_revision IS DISTINCT FROM '2026-07-24'
                  )
              )
            RETURNING
                endpoint.id,
                endpoint.tenant_id,
                projected.provider_key,
                projected.old_capabilities,
                projected.new_capabilities
        )
        INSERT INTO audit_records (
            tenant_id,
            action,
            resource_type,
            resource_id,
            actor_id,
            details,
            correlation_id
        )
        SELECT
            tenant_id,
            'model_endpoint.capabilities_migrated',
            'model_endpoint',
            id,
            'migration:20260724_0032',
            jsonb_build_object(
                'provider_key', provider_key,
                'old_capabilities', old_capabilities,
                'new_capabilities', new_capabilities,
                'catalog_revision', '2026-07-24'
            ),
            gen_random_uuid()
        FROM changed
        """
    )
    op.execute("ALTER TABLE model_endpoints ENABLE TRIGGER guard_model_endpoint_semantics")
    op.execute("ALTER TABLE agent_action_batches DISABLE TRIGGER guard_agent_action_batch")
    op.drop_constraint(
        "ck_agent_action_batches_source_format",
        "agent_action_batches",
        type_="check",
    )
    op.execute(
        """
        UPDATE agent_action_batches
        SET source_format = CASE source_format
            WHEN 'structured_json' THEN 'json_schema'
            WHEN 'plain_json' THEN 'json_object'
            WHEN 'provider_tool_calls' THEN 'native_tool_calls'
            WHEN 'legacy_plain_text' THEN 'protocol_rejected'
            ELSE source_format
        END
        WHERE source_format IN (
            'structured_json',
            'plain_json',
            'provider_tool_calls',
            'legacy_plain_text'
        )
        """
    )
    op.create_check_constraint(
        "ck_agent_action_batches_source_format",
        "agent_action_batches",
        "source_format IN "
        "('json_object', 'json_schema', 'native_tool_calls', 'protocol_rejected')",
    )
    op.execute("ALTER TABLE agent_action_batches ENABLE TRIGGER guard_agent_action_batch")
    op.execute("ALTER TABLE agent_action_repairs DISABLE TRIGGER guard_agent_action_repair")
    op.drop_constraint(
        "ck_agent_action_repairs_kind",
        "agent_action_repairs",
        type_="check",
    )
    op.execute(
        """
        UPDATE agent_action_repairs
        SET kind = 'completion_gate_correction'
        WHERE kind = 'semantic_final_correction'
        """
    )
    op.create_check_constraint(
        "ck_agent_action_repairs_kind",
        "agent_action_repairs",
        "kind IN ('post_model_call_commit', 'parse_correction', "
        "'clarification_correction', 'completion_gate_correction', 'replay')",
    )
    op.execute("ALTER TABLE agent_action_repairs ENABLE TRIGGER guard_agent_action_repair")
    op.drop_column("agent_actions", "compatibility_mode")
    op.execute(_GUARD_ACTION_FUNCTION)
    op.execute(_QUEUE_PROJECTION_FUNCTION)
    op.execute(
        """
        WITH resumed AS (
            UPDATE conversations AS conversation
            SET queue_state = 'active',
                queue_pause_reason = NULL,
                queue_pause_turn_id = NULL,
                queue_paused_at = NULL,
                revision = conversation.revision + 1,
                updated_at = clock_timestamp()
            FROM conversation_turns AS turn
            JOIN runs AS run
              ON run.tenant_id = turn.tenant_id
             AND run.id = turn.run_id
            WHERE conversation.tenant_id = turn.tenant_id
              AND conversation.id = turn.conversation_id
              AND conversation.queue_state = 'paused'
              AND conversation.queue_pause_reason = 'run_failed'
              AND conversation.queue_pause_turn_id = turn.id
              AND COALESCE(run.error ->> 'code', '') IN (
                  'SEMANTIC_FINAL_CORRECTION_EXHAUSTED',
                  'AGENT_ACTION_CORRECTION_EXHAUSTED',
                  'PROVIDER_STRUCTURED_OUTPUT_UNSUPPORTED',
                  'MODEL_CAPABILITY_MISMATCH'
              )
            RETURNING conversation.id, conversation.tenant_id, turn.id AS turn_id
        )
        INSERT INTO audit_records (
            tenant_id,
            action,
            resource_type,
            resource_id,
            actor_id,
            details,
            correlation_id
        )
        SELECT
            tenant_id,
            'conversation.queue.protocol_failure_resumed',
            'conversation',
            id,
            'migration:20260724_0032',
            jsonb_build_object(
                'turn_id', turn_id,
                'reason', 'structured_agent_action_protocol_convergence'
            ),
            gen_random_uuid()
        FROM resumed
        """
    )


def downgrade() -> None:
    # Capability convergence is intentionally forward-only: reconstructing
    # ambiguous legacy keys would re-enable the removed dual protocol.
    pass


_GUARD_ACTION_FUNCTION = r"""
CREATE OR REPLACE FUNCTION guard_agent_action_intent()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'AgentAction facts are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF ROW(
        OLD.tenant_id, OLD.run_id, OLD.batch_id, OLD.ordinal, OLD.action_id, OLD.kind,
        OLD.provider_call_id, OLD.tool_name, OLD.intent_redacted, OLD.completion,
        OLD.content_hash, OLD.arguments_hash, OLD.question_hash, OLD.reason_hash
    ) IS DISTINCT FROM ROW(
        NEW.tenant_id, NEW.run_id, NEW.batch_id, NEW.ordinal, NEW.action_id, NEW.kind,
        NEW.provider_call_id, NEW.tool_name, NEW.intent_redacted, NEW.completion,
        NEW.content_hash, NEW.arguments_hash, NEW.question_hash, NEW.reason_hash
    ) THEN
        RAISE EXCEPTION 'AgentAction intent and source are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status IN ('succeeded', 'failed', 'blocked') AND ROW(
        NEW.status, NEW.outcome_ref, NEW.observation_ref, NEW.completed_at
    ) IS DISTINCT FROM ROW(
        OLD.status, OLD.outcome_ref, OLD.observation_ref, OLD.completed_at
    ) THEN
        RAISE EXCEPTION 'terminal AgentAction outcome is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$function$;
"""


_QUEUE_PROJECTION_FUNCTION = r"""
CREATE OR REPLACE FUNCTION project_conversation_turn_from_run()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public AS $function$
DECLARE
    projected_status text;
    current_turn public.conversation_turns%ROWTYPE;
    is_head boolean := false;
    pause_reason text;
BEGIN
    SELECT * INTO current_turn
    FROM public.conversation_turns
    WHERE tenant_id = NEW.tenant_id AND run_id = NEW.id
    FOR UPDATE;

    IF FOUND THEN
        is_head := NOT EXISTS (
            SELECT 1
            FROM public.conversation_turns AS earlier
            WHERE earlier.tenant_id = current_turn.tenant_id
              AND earlier.conversation_id = current_turn.conversation_id
              AND earlier.sequence < current_turn.sequence
              AND earlier.status IN (
                  'accepted', 'queued', 'running', 'waiting_for_approval'
              )
        );
    END IF;

    projected_status := CASE NEW.status
        WHEN 'pending' THEN 'queued'
        WHEN 'waiting_for_approval' THEN 'waiting_for_approval'
        WHEN 'completed' THEN 'completed'
        WHEN 'failed' THEN 'failed'
        WHEN 'timed_out' THEN 'failed'
        WHEN 'cancelled' THEN 'cancelled'
        ELSE 'running'
    END;
    UPDATE public.conversation_turns
       SET status = projected_status,
           assistant_output = NEW.result,
           usage = COALESCE(NEW.cost, '{}'::jsonb),
           error = NEW.error,
           revision = revision + 1,
           updated_at = clock_timestamp()
     WHERE tenant_id = NEW.tenant_id
       AND run_id = NEW.id
       AND (status, assistant_output, usage, error)
           IS DISTINCT FROM
           (projected_status, NEW.result, COALESCE(NEW.cost, '{}'::jsonb), NEW.error);

    pause_reason := CASE
        WHEN NEW.status = 'failed'
         AND COALESCE(NEW.error ->> 'code', '') IN (
             'AGENT_ACTION_CORRECTION_EXHAUSTED',
             'PROVIDER_STRUCTURED_OUTPUT_UNSUPPORTED',
             'MODEL_CAPABILITY_MISMATCH'
         )
            THEN NULL
        WHEN NEW.status = 'failed' THEN 'run_failed'
        WHEN NEW.status = 'timed_out' THEN 'run_timed_out'
        WHEN NEW.status = 'cancelled' THEN 'turn_cancelled'
        ELSE NULL
    END;
    IF current_turn.id IS NOT NULL AND is_head AND pause_reason IS NOT NULL THEN
        UPDATE public.conversations
           SET queue_state = 'paused',
               queue_pause_reason = pause_reason,
               queue_pause_turn_id = current_turn.id,
               queue_paused_at = clock_timestamp(),
               revision = revision + 1,
               updated_at = clock_timestamp()
         WHERE tenant_id = current_turn.tenant_id
           AND id = current_turn.conversation_id
           AND queue_state = 'active';
    END IF;
    RETURN NEW;
END;
$function$;
"""
