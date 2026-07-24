#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

required_files=(
  backend/src/nico_agent/domain/context.py
  backend/src/nico_agent/conversations/context.py
  backend/src/nico_agent/runtime/contracts.py
  backend/src/nico_agent/runtime/native/context.py
  backend/tests/unit/test_conversation_context.py
  backend/tests/unit/test_conversation_model_messages.py
  backend/tests/integration/test_conversation_model_request_audit.py
  docs/runtime.md
  docs/handoffs/2026-07-23-conversation-continuity-goal-b-handoff.md
)

for file in "${required_files[@]}"; do
  [[ -s "$ROOT_DIR/$file" ]] || die "missing or empty Conversation Continuity Goal B file: $file"
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
bash -n "$ROOT_DIR/scripts/verify-conversation-continuity-goal-b.sh"
git -C "$ROOT_DIR" diff --check

"$ROOT_DIR/.venv/bin/ruff" check \
  "$ROOT_DIR/backend/src/nico_agent/domain/context.py" \
  "$ROOT_DIR/backend/src/nico_agent/conversations/contracts.py" \
  "$ROOT_DIR/backend/src/nico_agent/conversations/context.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/contracts.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/service.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/context.py" \
  "$ROOT_DIR/backend/tests/unit/test_conversation_context.py" \
  "$ROOT_DIR/backend/tests/unit/test_conversation_model_messages.py" \
  "$ROOT_DIR/backend/tests/unit/test_runtime_preparation.py" \
  "$ROOT_DIR/backend/tests/integration/test_conversation_model_request_audit.py" \
  "$ROOT_DIR/backend/tests/integration/test_native_runtime_persistence.py"
"$ROOT_DIR/.venv/bin/ruff" format --check \
  "$ROOT_DIR/backend/src/nico_agent/domain/context.py" \
  "$ROOT_DIR/backend/src/nico_agent/conversations/contracts.py" \
  "$ROOT_DIR/backend/src/nico_agent/conversations/context.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/contracts.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/service.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/context.py" \
  "$ROOT_DIR/backend/tests/unit/test_conversation_context.py" \
  "$ROOT_DIR/backend/tests/unit/test_conversation_model_messages.py" \
  "$ROOT_DIR/backend/tests/unit/test_runtime_preparation.py" \
  "$ROOT_DIR/backend/tests/integration/test_conversation_model_request_audit.py" \
  "$ROOT_DIR/backend/tests/integration/test_native_runtime_persistence.py"

"$ROOT_DIR/.venv/bin/pytest" -q \
  "$ROOT_DIR/backend/tests/unit/test_conversation_context.py" \
  "$ROOT_DIR/backend/tests/unit/test_conversation_model_messages.py" \
  "$ROOT_DIR/backend/tests/unit/test_runtime_preparation.py" \
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
  "$ROOT_DIR/docs/handoffs/2026-07-23-conversation-continuity-goal-b-handoff.md"
  "$ROOT_DIR/docs/runtime.md"
)
if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  scan_targets+=("$NICO_EVIDENCE_DIR")
fi
if rg -n \
  'CC_A_AUDIT_SECRET_REFERENCE|REDACTED_TEST_REFERENCE|sk-[A-Za-z0-9]{16,}' \
  "${scan_targets[@]}"; then
  die "Goal B evidence contains a forbidden credential marker or secret-shaped value"
fi

log "PASS Conversation Continuity Goal B message-fidelity gates"
