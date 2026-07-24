#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

required_files=(
  backend/src/nico_agent/runtime/completion_gate.py
  backend/src/nico_agent/runtime/native/completion.py
  backend/src/nico_agent/runtime/native/context.py
  backend/src/nico_agent/runtime/native/loop.py
  backend/src/nico_agent/runtime/service.py
  backend/tests/unit/test_completion_gate.py
  backend/tests/unit/test_native_direct_runtime.py
  backend/tests/unit/test_native_react_loop.py
  backend/tests/unit/test_native_plan_loop.py
  backend/tests/integration/test_runtime_completion_gate.py
  docs/handoffs/2026-07-23-conversation-continuity-goal-j-handoff.md
)

for file in "${required_files[@]}"; do
  [[ -s "$ROOT_DIR/$file" ]] \
    || die "missing or empty Conversation Continuity Goal J file: $file"
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
bash -n "$ROOT_DIR/scripts/verify-conversation-continuity-goal-j.sh"
git -C "$ROOT_DIR" diff --check

rg -Fq 'version: Literal["semantic-completion-v1"]' \
  "$ROOT_DIR/backend/src/nico_agent/runtime/completion_gate.py" \
  || die "versioned semantic Completion Gate policy is missing"
rg -Fq 'allow_legacy_plain_text: Literal[False]' \
  "$ROOT_DIR/backend/src/nico_agent/runtime/completion_gate.py" \
  || die "strict Native final metadata enforcement is missing"
rg -Fq 'SEMANTIC_FINAL_CORRECTION_EXHAUSTED' \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/loop.py" \
  || die "bounded semantic final correction exhaustion is missing"
rg -Fq '"kind": "semantic_final_correction"' \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/loop.py" \
  || die "semantic final correction is not source-linked"
rg -Fq 'completion_pending_user_input_count' \
  "$ROOT_DIR/backend/src/nico_agent/runtime/service.py" \
  || die "pending UserInput completion fact is missing"
rg -Fq '"visibility": (' \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/loop.py" \
  && rg -Fq 'if persist_actions' \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/loop.py" \
  || die "candidate Action text is not explicitly internal"

python_targets=(
  "$ROOT_DIR/backend/src/nico_agent/runtime/__init__.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/actions.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/completion_gate.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/action_dispatcher.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/completion.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/context.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/loop.py"
  "$ROOT_DIR/backend/src/nico_agent/runtime/service.py"
  "$ROOT_DIR/backend/src/nico_agent/model_api.py"
  "$ROOT_DIR/backend/tests/unit/test_agent_actions.py"
  "$ROOT_DIR/backend/tests/unit/test_completion_gate.py"
  "$ROOT_DIR/backend/tests/unit/test_conversation_model_messages.py"
  "$ROOT_DIR/backend/tests/unit/test_native_direct_runtime.py"
  "$ROOT_DIR/backend/tests/unit/test_native_react_loop.py"
  "$ROOT_DIR/backend/tests/unit/test_native_plan_loop.py"
  "$ROOT_DIR/backend/tests/integration/test_runtime_completion_gate.py"
)
"$ROOT_DIR/.venv/bin/ruff" check "${python_targets[@]}"
"$ROOT_DIR/.venv/bin/ruff" format --check "${python_targets[@]}"

"$ROOT_DIR/.venv/bin/pytest" -q \
  "$ROOT_DIR/backend/tests/unit/test_agent_actions.py" \
  "$ROOT_DIR/backend/tests/unit/test_completion_gate.py" \
  "$ROOT_DIR/backend/tests/unit/test_conversation_model_messages.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_direct_runtime.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_react_loop.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_plan_loop.py"
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
  "$ROOT_DIR/docs/handoffs/2026-07-23-conversation-continuity-goal-j-handoff.md"
  "$ROOT_DIR/docs/runtime.md"
  "$ROOT_DIR/docs/testing.md"
)
if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  scan_targets+=("$NICO_EVIDENCE_DIR")
fi
if rg -n 'sk-[A-Za-z0-9]{16,}|protected-after-crash|database-password' \
  "${scan_targets[@]}"; then
  die "Goal J evidence contains a secret-shaped or protected answer value"
fi

log "PASS Conversation Continuity Goal J semantic Completion Gate gates"
