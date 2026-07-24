#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

required_files=(
  backend/src/nico_agent/runtime/clarification.py
  backend/src/nico_agent/runtime/native/action_parser.py
  backend/src/nico_agent/runtime/native/checkpoint.py
  backend/src/nico_agent/runtime/native/context.py
  backend/src/nico_agent/runtime/native/loop.py
  backend/tests/unit/test_clarification_gate.py
  backend/tests/unit/test_native_direct_runtime.py
  backend/tests/unit/test_native_react_loop.py
  backend/tests/integration/test_clarification_runtime.py
  docs/handoffs/2026-07-23-conversation-continuity-goal-i-handoff.md
)

for file in "${required_files[@]}"; do
  [[ -s "$ROOT_DIR/$file" ]] \
    || die "missing or empty Conversation Continuity Goal I file: $file"
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
bash -n "$ROOT_DIR/scripts/verify-conversation-continuity-goal-i.sh"
git -C "$ROOT_DIR" diff --check

rg -Fq 'version: Literal["clarification-v1"]' \
  "$ROOT_DIR/backend/src/nico_agent/runtime/clarification.py" \
  || die "versioned Clarification policy is missing"
rg -Fq 'ClarificationDecisionKind.ALLOW_ASK_USER' \
  "$ROOT_DIR/backend/src/nico_agent/runtime/clarification.py" \
  || die "Clarification decision contract is missing"
rg -Fq 'CLARIFICATION_CORRECTION_EXHAUSTED' \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/loop.py" \
  || die "bounded clarification correction exhaustion is missing"
rg -Fq '"source_batch_key": active_batch.content_hash' \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/loop.py" \
  || die "Direct clarification repair is not source-linked"
rg -Fq 'clarification_source_batch_key' \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/checkpoint.py" \
  || die "ReAct clarification recovery state is missing"
rg -Fq 'user_input_handler_enabled=_user_input_handler.get() is not None' \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/loop.py" \
  || die "ask_user is not capability-gated by the durable handler"

python_targets=(
  "$ROOT_DIR/backend/src/nico_agent/runtime/clarification.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/action_parser.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/checkpoint.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/context.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/loop.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/prompts.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/service.py"
  "$ROOT_DIR/backend/tests/unit/test_agent_actions.py"
  "$ROOT_DIR/backend/tests/unit/test_clarification_gate.py"
  "$ROOT_DIR/backend/tests/unit/test_native_continuity_prompt.py"
  "$ROOT_DIR/backend/tests/unit/test_native_direct_runtime.py"
  "$ROOT_DIR/backend/tests/unit/test_native_react_loop.py"
  "$ROOT_DIR/backend/tests/integration/test_clarification_runtime.py"
)
"$ROOT_DIR/.venv/bin/ruff" check "${python_targets[@]}"
"$ROOT_DIR/.venv/bin/ruff" format --check "${python_targets[@]}"

"$ROOT_DIR/.venv/bin/pytest" -q \
  "$ROOT_DIR/backend/tests/unit/test_agent_actions.py" \
  "$ROOT_DIR/backend/tests/unit/test_clarification_gate.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_continuity_prompt.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_direct_runtime.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_react_loop.py"
"$ROOT_DIR/scripts/test.sh"

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
  "$ROOT_DIR/docs/handoffs/2026-07-23-conversation-continuity-goal-i-handoff.md"
  "$ROOT_DIR/docs/runtime.md"
  "$ROOT_DIR/docs/testing.md"
)
if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  scan_targets+=("$NICO_EVIDENCE_DIR")
fi
if rg -n 'sk-[A-Za-z0-9]{16,}|protected-after-crash|database-password' \
  "${scan_targets[@]}"; then
  die "Goal I evidence contains a secret-shaped or protected answer value"
fi

log "PASS Conversation Continuity Goal I Clarification Gate gates"
