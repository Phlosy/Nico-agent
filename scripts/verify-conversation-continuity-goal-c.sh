#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

required_files=(
  backend/src/nico_agent/runtime/native/prompts.py
  backend/src/nico_agent/runtime/native/context.py
  backend/tests/unit/test_native_continuity_prompt.py
  backend/tests/unit/test_native_direct_runtime.py
  backend/tests/unit/test_native_react_loop.py
  backend/tests/unit/test_native_plan_loop.py
  backend/tests/unit/test_runtime_preparation.py
  docs/runtime.md
  docs/handoffs/2026-07-23-conversation-continuity-goal-c-handoff.md
)

for file in "${required_files[@]}"; do
  [[ -s "$ROOT_DIR/$file" ]] || die "missing or empty Conversation Continuity Goal C file: $file"
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
bash -n "$ROOT_DIR/scripts/verify-conversation-continuity-goal-c.sh"
git -C "$ROOT_DIR" diff --check

if rg -n \
  'ask_user|你平台是怎么提供de|第二种呢|那个更适合 Mac|depoly' \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/prompts.py"; then
  die "Goal C policy advertises an unavailable action or embeds a case fixture"
fi

"$ROOT_DIR/.venv/bin/ruff" check \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/prompts.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/context.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_continuity_prompt.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_direct_runtime.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_react_loop.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_plan_loop.py" \
  "$ROOT_DIR/backend/tests/unit/test_runtime_preparation.py"
"$ROOT_DIR/.venv/bin/ruff" format --check \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/prompts.py" \
  "$ROOT_DIR/backend/src/nico_agent/runtime/native/context.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_continuity_prompt.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_direct_runtime.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_react_loop.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_plan_loop.py" \
  "$ROOT_DIR/backend/tests/unit/test_runtime_preparation.py"

"$ROOT_DIR/.venv/bin/pytest" -q \
  "$ROOT_DIR/backend/tests/unit/test_native_continuity_prompt.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_direct_runtime.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_react_loop.py" \
  "$ROOT_DIR/backend/tests/unit/test_native_plan_loop.py" \
  "$ROOT_DIR/backend/tests/unit/test_runtime_preparation.py"
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
  "$ROOT_DIR/docs/handoffs/2026-07-23-conversation-continuity-goal-c-handoff.md"
  "$ROOT_DIR/docs/runtime.md"
)
if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  scan_targets+=("$NICO_EVIDENCE_DIR")
fi
if rg -n 'sk-[A-Za-z0-9]{16,}' "${scan_targets[@]}"; then
  die "Goal C evidence contains a secret-shaped value"
fi

log "PASS Conversation Continuity Goal C Prompt-policy gates"
