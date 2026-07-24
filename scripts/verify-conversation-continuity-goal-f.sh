#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

required_files=(
  backend/src/nico_agent/runtime/native/action_dispatcher.py
  backend/src/nico_agent/runtime/native/action_parser.py
  backend/src/nico_agent/runtime/native/checkpoint.py
  backend/src/nico_agent/runtime/native/context.py
  backend/src/nico_agent/runtime/native/loop.py
  backend/src/nico_agent/runtime/native/prompts.py
  backend/src/nico_agent/runtime/service.py
  backend/src/nico_agent/runtime/tools.py
  backend/tests/unit/test_action_dispatcher.py
  backend/tests/integration/test_agent_action_dispatch.py
  docs/handoffs/2026-07-23-conversation-continuity-goal-f-handoff.md
)

for file in "${required_files[@]}"; do
  [[ -s "$ROOT_DIR/$file" ]] || die "missing or empty Conversation Continuity Goal F file: $file"
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
bash -n "$ROOT_DIR/scripts/verify-conversation-continuity-goal-f.sh"
git -C "$ROOT_DIR" diff --check

rg -q "class AgentActionDispatcher" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/action_dispatcher.py" \
  || die "Goal F dispatcher is missing"
rg -q "action_handler: RuntimeActionHandler" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/contracts.py" \
  || die "RuntimeServices does not expose the Action persistence authority"
rg -q "prepared.descriptor.implementation == \"native\"" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/executor.py" \
  || die "Action handler is not isolated to Native providers"
rg -q "ACTION_KIND_UNSUPPORTED" \
  "$ROOT_DIR/backend/tests/unit/test_native_direct_runtime.py" \
  || die "out-of-contract ask_user rejection is not covered"
rg -q "status == \"unknown\"" \
  "$ROOT_DIR/backend/tests/integration/test_agent_action_dispatch.py" \
  || die "unknown-effect recovery is not covered by PostgreSQL"

python_targets=(
  "$ROOT_DIR/backend/src/nico_agent/runtime/contracts.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/executor.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/action_dispatcher.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/action_parser.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/checkpoint.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/context.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/loop.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/prompts.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/service.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/tools.py"
  "$ROOT_DIR/backend/tests/integration/test_agent_action_dispatch.py"
  "$ROOT_DIR/backend/tests/integration/test_agent_action_persistence.py"
  "$ROOT_DIR/backend/tests/unit/test_action_dispatcher.py"
  "$ROOT_DIR/backend/tests/unit/test_agent_actions.py"
  "$ROOT_DIR/backend/tests/unit/test_native_continuity_prompt.py"
  "$ROOT_DIR/backend/tests/unit/test_native_direct_runtime.py"
  "$ROOT_DIR/backend/tests/unit/test_native_plan_loop.py"
  "$ROOT_DIR/backend/tests/unit/test_native_react_loop.py"
)
"$ROOT_DIR/.venv/bin/ruff" check "${python_targets[@]}"
"$ROOT_DIR/.venv/bin/ruff" format --check "${python_targets[@]}"

"$ROOT_DIR/.venv/bin/pytest" -q \
  "$ROOT_DIR/backend/tests/unit/test_action_dispatcher.py" \
  "$ROOT_DIR/backend/tests/unit/test_agent_actions.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_continuity_prompt.py" \
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
  "$ROOT_DIR/docs/handoffs/2026-07-23-conversation-continuity-goal-f-handoff.md"
  "$ROOT_DIR/docs/runtime.md"
)
if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  scan_targets+=("$NICO_EVIDENCE_DIR")
fi
if rg -n 'sk-[A-Za-z0-9]{16,}' "${scan_targets[@]}"; then
  die "Goal F evidence contains a secret-shaped value"
fi

log "PASS Conversation Continuity Goal F compatibility Action dispatch gates"
