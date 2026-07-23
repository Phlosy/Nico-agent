#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command git
ensure_python_environment

previous_ref="${NICO_PREVIOUS_REF:-HEAD^}"
previous_commit="$(git -C "$ROOT_DIR" rev-parse --verify "${previous_ref}^{commit}")"
temporary_root="$(mktemp -d "${TMPDIR:-/tmp}/nico-rh-a-previous.XXXXXX")"

cleanup() {
  rm -rf -- "$temporary_root"
}
trap cleanup EXIT

if ! git -C "$ROOT_DIR" cat-file -e \
  "$previous_commit:backend/migrations/versions/20260722_0026_chat_session_controls.py"; then
  die "previous application ref does not contain migration 0026: $previous_commit"
fi
if git -C "$ROOT_DIR" cat-file -e \
  "$previous_commit:backend/migrations/versions/20260723_0027_runtime_lifecycle_authority.py" \
  2>/dev/null; then
  die "previous application ref already contains Runtime lifecycle migration 0027"
fi

git -C "$ROOT_DIR" archive "$previous_commit" | tar -x -C "$temporary_root"

log "verifying the configured database is exactly schema 20260723_0027"
PYTHONPATH="$ROOT_DIR/backend/src" "$ROOT_DIR/.venv/bin/python" <<'PY'
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import Settings


EXPECTED_COLUMNS = {
    "lifecycle_reason",
    "lifecycle_metadata",
    "lifecycle_revision",
}
EXPECTED_FUNCTIONS = (
    "public.runtime_lifecycle_transition_allowed(text,text)",
    "public.runtime_run_claimable(text,timestamptz,timestamptz)",
    "public.transition_run_lifecycle(uuid,uuid,text,text,text,jsonb,text,uuid,text,text)",
    "public.claim_next_run(text,integer)",
    "public.reconcile_coordination_waiters()",
    "public.reconcile_expired_tool_approvals()",
)


async def verify_schema() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    try:
        async with engine.connect() as connection:
            heads = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalars().all()
            if heads != ["20260723_0027"]:
                raise RuntimeError(
                    "configured database is not exactly at Alembic 20260723_0027 "
                    f"(found {heads!r})"
                )

            columns = set(
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = 'runs' "
                            "AND column_name IN "
                            "('lifecycle_reason', 'lifecycle_metadata', 'lifecycle_revision')"
                        )
                    )
                )
                .scalars()
                .all()
            )
            if columns != EXPECTED_COLUMNS:
                raise RuntimeError(
                    "configured database is missing Runtime lifecycle columns "
                    f"(found {sorted(columns)!r})"
                )

            functions = dict(
                (
                    await connection.execute(
                        text(
                            "SELECT signature, to_regprocedure(signature) IS NOT NULL "
                            "FROM unnest(CAST(:signatures AS text[])) AS signature"
                        ),
                        {"signatures": list(EXPECTED_FUNCTIONS)},
                    )
                ).all()
            )
            missing = [
                signature
                for signature in EXPECTED_FUNCTIONS
                if not functions.get(signature, False)
            ]
            if missing:
                raise RuntimeError(
                    "configured database is missing Runtime lifecycle functions "
                    f"{missing!r}"
                )
    finally:
        await engine.dispose()


asyncio.run(verify_schema())
PY

export RUN_INTEGRATION=1
export PYTHONPATH="$temporary_root/backend/src"

log "testing previous application $previous_commit against the expanded 0027 schema"
"$ROOT_DIR/.venv/bin/python" -m pytest -q \
  -c "$temporary_root/backend/pyproject.toml" \
  "$temporary_root/backend/tests/integration/test_runtime_leasing.py::test_conversation_queue_claims_only_the_head_in_sequence" \
  "$temporary_root/backend/tests/integration/test_runtime_leasing.py::test_abnormal_head_pauses_queue_but_future_cancel_does_not" \
  "$temporary_root/backend/tests/integration/test_runtime_leasing.py::test_paused_queue_allows_started_head_lease_recovery" \
  "$temporary_root/backend/tests/integration/test_runtime_leasing.py::test_paused_queue_allows_only_pause_cause_retry" \
  "$temporary_root/backend/tests/integration/test_native_react_runtime.py::test_sensitive_tool_approval_suspends_decides_and_recovers_exactly_once" \
  "$temporary_root/backend/tests/integration/test_coordination_service.py::test_tree_cancel_releases_reservations_and_prevents_late_wake" \
  "$temporary_root/backend/tests/integration/test_coordination_service.py::test_reconciler_wakes_terminal_tree_and_retry_request_is_idempotent"

log "PASS previous API/Worker queue, approval and coordination paths on schema 0027"
