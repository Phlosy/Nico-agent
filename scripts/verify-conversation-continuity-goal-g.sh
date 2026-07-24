#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

required_files=(
  backend/migrations/versions/20260723_0031_user_input_requests.py
  backend/src/nico_agent/user_inputs/__init__.py
  backend/src/nico_agent/user_inputs/contracts.py
  backend/src/nico_agent/user_inputs/service.py
  backend/src/nico_agent/runtime/native/action_dispatcher.py
  backend/src/nico_agent/runtime/native/checkpoint.py
  backend/src/nico_agent/runtime/lifecycle.py
  backend/src/nico_agent/runtime/service.py
  backend/src/nico_agent/runtime/executor.py
  backend/src/nico_agent/domain/models.py
  backend/src/nico_agent/domain/states.py
  backend/tests/unit/test_user_input_requests.py
  backend/tests/integration/test_user_input_runtime.py
  docs/handoffs/2026-07-23-conversation-continuity-goal-g-handoff.md
)

for file in "${required_files[@]}"; do
  [[ -s "$ROOT_DIR/$file" ]] \
    || die "missing or empty Conversation Continuity Goal G file: $file"
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
bash -n "$ROOT_DIR/scripts/verify-conversation-continuity-goal-g.sh"
git -C "$ROOT_DIR" diff --check

rg -q 'revision: str = "20260723_0031"' \
  "$ROOT_DIR/backend/migrations/versions/20260723_0031_user_input_requests.py" \
  || die "Goal G migration revision is missing"
rg -q 'down_revision: str \| None = "20260723_0030"' \
  "$ROOT_DIR/backend/migrations/versions/20260723_0031_user_input_requests.py" \
  || die "Goal G migration does not extend the live head"
rg -q 'FORCE ROW LEVEL SECURITY' \
  "$ROOT_DIR/backend/migrations/versions/20260723_0031_user_input_requests.py" \
  || die "UserInputRequest FORCE RLS is missing"
rg -q 'reconcile_user_input_requests' \
  "$ROOT_DIR/backend/src/nico_agent/database.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/executor.py" \
  "$ROOT_DIR/backend/migrations/versions/20260723_0031_user_input_requests.py" \
  || die "UserInputRequest reconciler is not wired"
rg -q 'USER_INPUT = "user_input"' \
  "$ROOT_DIR/backend/src/nico_agent/runtime/contracts.py" \
  || die "Native UserInput capability is missing"
rg -q 'release_action=False' \
  "$ROOT_DIR/backend/tests/unit/test_action_dispatcher.py" \
  || die "durable request suspension does not prove Action retention"
rg -q 'answered_but_unwoken' \
  "$ROOT_DIR/backend/tests/integration/test_user_input_runtime.py" \
  || die "answered-but-unwoken recovery proof is missing"
rg -q 'ACTION_KIND_UNSUPPORTED' \
  "$ROOT_DIR/backend/tests/unit/test_native_direct_runtime.py" \
  || die "live ask_user Schema guard is not covered"

python_targets=(
  "$ROOT_DIR/backend/migrations/versions/20260723_0031_user_input_requests.py"
  "$ROOT_DIR/backend/src/nico_agent/control_plane.py"
  "$ROOT_DIR/backend/src/nico_agent/database.py"
  "$ROOT_DIR/backend/src/nico_agent/domain/models.py"
  "$ROOT_DIR/backend/src/nico_agent/domain/states.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/contracts.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/executor.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/lifecycle.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/action_dispatcher.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/checkpoint.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/provider.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/service.py"
  "$ROOT_DIR/backend/src/nico_agent/user_inputs/__init__.py"
  "$ROOT_DIR/backend/src/nico_agent/user_inputs/contracts.py"
  "$ROOT_DIR/backend/src/nico_agent/user_inputs/service.py"
  "$ROOT_DIR/backend/tests/integration/test_infrastructure.py"
  "$ROOT_DIR/backend/tests/integration/test_runtime_lifecycle.py"
  "$ROOT_DIR/backend/tests/integration/test_user_input_runtime.py"
  "$ROOT_DIR/backend/tests/unit/test_action_dispatcher.py"
  "$ROOT_DIR/backend/tests/unit/test_runtime_lifecycle.py"
  "$ROOT_DIR/backend/tests/unit/test_user_input_requests.py"
)
"$ROOT_DIR/.venv/bin/ruff" check "${python_targets[@]}"
"$ROOT_DIR/.venv/bin/ruff" format --check "${python_targets[@]}"

"$ROOT_DIR/.venv/bin/pytest" -q \
  "$ROOT_DIR/backend/tests/unit/test_action_dispatcher.py" \
  "$ROOT_DIR/backend/tests/unit/test_runtime_lifecycle.py" \
  "$ROOT_DIR/backend/tests/unit/test_user_input_requests.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_direct_runtime.py"
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
  "$ROOT_DIR/docs/handoffs/2026-07-23-conversation-continuity-goal-g-handoff.md"
  "$ROOT_DIR/docs/runtime.md"
  "$ROOT_DIR/docs/state-machines.md"
)
if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  scan_targets+=("$NICO_EVIDENCE_DIR")
fi
if rg -n 'sk-[A-Za-z0-9]{16,}|protected-after-crash|database-password' \
  "${scan_targets[@]}"; then
  die "Goal G evidence contains a secret-shaped or protected answer value"
fi

log "PASS Conversation Continuity Goal G durable user-input backend gates"
