#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

required_files=(
  backend/src/nico_agent/runtime/lifecycle.py
  backend/migrations/versions/20260723_0027_runtime_lifecycle_authority.py
  backend/tests/unit/test_runtime_lifecycle.py
  backend/tests/integration/test_runtime_lifecycle.py
  scripts/verify-runtime-hardening-goal-a-mixed-version.sh
  docs/handoffs/2026-07-23-runtime-hardening-goal-a-handoff.md
  docs/runtime.md
  docs/state-machines.md
)

for file in "${required_files[@]}"; do
  [[ -s "$ROOT_DIR/$file" ]] || die "missing or empty Runtime Hardening Goal A file: $file"
done

immutable_migration="backend/migrations/versions/20260722_0026_chat_session_controls.py"
immutable_migration_blob="814cf538a7945260aac78258887a5fc654b51003"
[[ -f "$ROOT_DIR/$immutable_migration" ]] || die "immutable migration 0026 is missing"
worktree_blob="$(git -C "$ROOT_DIR" hash-object -- "$immutable_migration")"
if [[ "$worktree_blob" != "$immutable_migration_blob" ]]; then
  die "immutable migration 0026 worktree blob does not match the pinned baseline"
fi
index_blob="$(git -C "$ROOT_DIR" rev-parse --verify ":$immutable_migration" 2>/dev/null || true)"
if [[ -n "$index_blob" && "$index_blob" != "$immutable_migration_blob" ]]; then
  die "immutable migration 0026 index blob does not match the pinned baseline"
fi

if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  mkdir -p "$NICO_EVIDENCE_DIR"
  exec > >(tee "$NICO_EVIDENCE_DIR/verify.log") 2>&1
  {
    date -u +'%Y-%m-%dT%H:%M:%SZ'
    git -C "$ROOT_DIR" rev-parse HEAD
    git -C "$ROOT_DIR" status --short
    python3 --version
  } >"$NICO_EVIDENCE_DIR/environment.txt"
fi

python3 "$ROOT_DIR/scripts/check-docs.py"
bash -n "$ROOT_DIR/scripts/verify-runtime-hardening-goal-a.sh"
bash -n "$ROOT_DIR/scripts/verify-runtime-hardening-goal-a-mixed-version.sh"
git -C "$ROOT_DIR" diff --check HEAD

"$ROOT_DIR/.venv/bin/ruff" check \
  "$ROOT_DIR/backend/src/nico_agent/domain/states.py" \
  "$ROOT_DIR/backend/src/nico_agent/domain/models.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/contracts.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/lifecycle.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/service.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/executor.py" \
  "$ROOT_DIR/backend/src/nico_agent/control_plane.py" \
  "$ROOT_DIR/backend/src/nico_agent/tools/gateway.py" \
  "$ROOT_DIR/backend/src/nico_agent/tool_approvals/service.py" \
  "$ROOT_DIR/backend/src/nico_agent/coordination/service.py" \
  "$ROOT_DIR/backend/tests/unit/test_runtime_lifecycle.py" \
  "$ROOT_DIR/backend/tests/integration/test_runtime_lifecycle.py"

"$ROOT_DIR/.venv/bin/ruff" format --check \
  "$ROOT_DIR/backend/src/nico_agent/domain/states.py" \
  "$ROOT_DIR/backend/src/nico_agent/domain/models.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/contracts.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/lifecycle.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/service.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/executor.py" \
  "$ROOT_DIR/backend/src/nico_agent/control_plane.py" \
  "$ROOT_DIR/backend/src/nico_agent/tools/gateway.py" \
  "$ROOT_DIR/backend/src/nico_agent/tool_approvals/service.py" \
  "$ROOT_DIR/backend/src/nico_agent/coordination/service.py" \
  "$ROOT_DIR/backend/tests/unit/test_runtime_lifecycle.py" \
  "$ROOT_DIR/backend/tests/integration/test_runtime_lifecycle.py"

"$ROOT_DIR/.venv/bin/pytest" -q \
  "$ROOT_DIR/backend/tests/unit/test_runtime_lifecycle.py" \
  "$ROOT_DIR/backend/tests/unit/test_domain_states.py" \
  "$ROOT_DIR/backend/tests/unit/test_tool_approvals.py" \
  "$ROOT_DIR/backend/tests/unit/test_worker.py" \
  "$ROOT_DIR/backend/tests/unit/test_runtime_preparation.py" \
  "$ROOT_DIR/backend/tests/unit/test_runtime_contract_v2.py"

if [[ "${RUN_INTEGRATION:-0}" == "1" ]]; then
  "$ROOT_DIR/.venv/bin/pytest" -q \
    "$ROOT_DIR/backend/tests/integration/test_runtime_lifecycle.py"
  "$ROOT_DIR/scripts/verify-runtime-hardening-goal-a-mixed-version.sh"
else
  log "RUN_INTEGRATION is not 1; PostgreSQL proof must be supplied separately"
fi

log "PASS Runtime Hardening Goal A focused lifecycle gates"
