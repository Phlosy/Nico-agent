#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

required_files=(
  backend/src/nico_agent/runtime/actions.py
  backend/src/nico_agent/runtime/native/action_parser.py
  backend/tests/unit/test_agent_actions.py
  backend/tests/unit/test_model_provider_adapters.py
  docs/handoffs/2026-07-23-conversation-continuity-goal-d-handoff.md
)

for file in "${required_files[@]}"; do
  [[ -s "$ROOT_DIR/$file" ]] || die "missing or empty Conversation Continuity Goal D file: $file"
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
bash -n "$ROOT_DIR/scripts/verify-conversation-continuity-goal-d.sh"
git -C "$ROOT_DIR" diff --check

if rg -n 'action_parser|parse_agent_actions' \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/loop.py"; then
  die "Goal D must not move the live Native dispatcher"
fi
if git -C "$ROOT_DIR" status --short | awk '{print $2}' | grep -Eq \
  '^backend/migrations/'; then
  die "Goal D must not add or modify persistence migrations"
fi

"$ROOT_DIR/.venv/bin/ruff" check \
  "$ROOT_DIR/backend/src/nico_agent/models/contracts.py" \
  "$ROOT_DIR/backend/src/nico_agent/models/gateway.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/actions.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/contracts.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/__init__.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/action_parser.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/loop.py" \
  "$ROOT_DIR/backend/tests/unit/test_agent_actions.py" \
  "$ROOT_DIR/backend/tests/unit/test_model_provider_adapters.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_direct_runtime.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_react_loop.py"
"$ROOT_DIR/.venv/bin/ruff" format --check \
  "$ROOT_DIR/backend/src/nico_agent/models/contracts.py" \
  "$ROOT_DIR/backend/src/nico_agent/models/gateway.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/actions.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/contracts.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/__init__.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/action_parser.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/loop.py" \
  "$ROOT_DIR/backend/tests/unit/test_agent_actions.py" \
  "$ROOT_DIR/backend/tests/unit/test_model_provider_adapters.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_direct_runtime.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_react_loop.py"

"$ROOT_DIR/.venv/bin/pytest" -q \
  "$ROOT_DIR/backend/tests/unit/test_agent_actions.py" \
  "$ROOT_DIR/backend/tests/unit/test_model_provider_adapters.py" \
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
  "$ROOT_DIR/docs/handoffs/2026-07-23-conversation-continuity-goal-d-handoff.md"
)
if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  scan_targets+=("$NICO_EVIDENCE_DIR")
fi
if rg -n 'sk-[A-Za-z0-9]{16,}' "${scan_targets[@]}"; then
  die "Goal D evidence contains a secret-shaped value"
fi

log "PASS Conversation Continuity Goal D AgentAction contract gates"
