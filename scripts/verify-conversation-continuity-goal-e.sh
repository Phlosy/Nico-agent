#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

required_files=(
  backend/migrations/versions/20260723_0030_agent_actions.py
  backend/src/nico_agent/runtime/actions.py
  backend/src/nico_agent/runtime/native/action_parser.py
  backend/src/nico_agent/runtime/native/checkpoint.py
  backend/src/nico_agent/runtime/native/loop.py
  backend/src/nico_agent/runtime/service.py
  backend/tests/integration/test_agent_action_persistence.py
  backend/tests/unit/test_agent_actions.py
  docs/handoffs/2026-07-23-conversation-continuity-goal-e-handoff.md
)

for file in "${required_files[@]}"; do
  [[ -s "$ROOT_DIR/$file" ]] || die "missing or empty Conversation Continuity Goal E file: $file"
done

if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  mkdir -p "$NICO_EVIDENCE_DIR"
  exec > >(tee "$NICO_EVIDENCE_DIR/verify.log") 2>&1
  {
    date -u +'%Y-%m-%dT%H:%M:%SZ'
    git -C "$ROOT_DIR" rev-parse HEAD
    git -C "$ROOT_DIR" branch --show-current
    git -C "$ROOT_DIR" status --short
    "$ROOT_DIR/.venv/bin/python" --version
    docker --version
  } >"$NICO_EVIDENCE_DIR/environment.txt"
fi

python3 "$ROOT_DIR/scripts/check-docs.py"
bash -n "$ROOT_DIR/scripts/verify-conversation-continuity-goal-e.sh"
git -C "$ROOT_DIR" diff --check

heads="$("$ROOT_DIR/.venv/bin/alembic" -c "$ROOT_DIR/backend/alembic.ini" heads)"
[[ "$heads" == *"20260723_0030 (head)"* ]] || die "AgentAction migration is not the linear head"

migration="$ROOT_DIR/backend/migrations/versions/20260723_0030_agent_actions.py"
for table in agent_action_batches agent_actions agent_action_repairs; do
  rg -q "\"$table\"" "$migration" || die "AgentAction migration omits $table"
done
rg -q "FORCE ROW LEVEL SECURITY" "$migration" \
  || die "AgentAction migration does not enable FORCE RLS"
rg -q 'down_revision: str \| None = "20260723_0029"' "$migration" \
  || die "AgentAction migration does not extend live head 0029"

python_targets=(
  "$ROOT_DIR/backend/migrations/versions/20260723_0030_agent_actions.py"
  "$ROOT_DIR/backend/src/nico_agent/domain/models.py"
  "$ROOT_DIR/backend/src/nico_agent/domain/states.py"
  "$ROOT_DIR/backend/src/nico_agent/model_api.py"
  "$ROOT_DIR/backend/src/nico_agent/model_api_schemas.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/contracts.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/action_parser.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/checkpoint.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/loop.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/service.py"
  "$ROOT_DIR/backend/tests/integration/test_agent_action_persistence.py"
  "$ROOT_DIR/backend/tests/integration/test_infrastructure.py"
  "$ROOT_DIR/backend/tests/integration/test_runtime_lifecycle.py"
  "$ROOT_DIR/backend/tests/unit/test_agent_actions.py"
  "$ROOT_DIR/backend/tests/unit/test_native_direct_runtime.py"
)
"$ROOT_DIR/.venv/bin/ruff" check "${python_targets[@]}"
"$ROOT_DIR/.venv/bin/ruff" format --check "${python_targets[@]}"

"$ROOT_DIR/.venv/bin/pytest" -q \
  "$ROOT_DIR/backend/tests/unit/test_agent_actions.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_direct_runtime.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_react_loop.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_plan_loop.py"
"$ROOT_DIR/.venv/bin/pytest" -q "$ROOT_DIR/backend/tests/unit"

if [[ "${RUN_INTEGRATION:-0}" == "1" ]]; then
  if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
    "$ROOT_DIR/scripts/test-integration.sh" 2>&1 \
      | tee "$NICO_EVIDENCE_DIR/integration.log"
  else
    "$ROOT_DIR/scripts/test-integration.sh"
  fi
else
  log "RUN_INTEGRATION is not 1; PostgreSQL proof must be supplied separately"
fi

scan_targets=(
  "$ROOT_DIR/docs/handoffs/2026-07-23-conversation-continuity-goal-e-handoff.md"
  "$ROOT_DIR/docs/domain-model.md"
  "$ROOT_DIR/docs/runtime.md"
)
if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  scan_targets+=("$NICO_EVIDENCE_DIR")
fi
if rg -n 'sk-[A-Za-z0-9]{16,}' "${scan_targets[@]}"; then
  die "Goal E evidence contains a secret-shaped value"
fi

log "PASS Conversation Continuity Goal E durable AgentAction gates"
