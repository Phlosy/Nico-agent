#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

required_files=(
  backend/evals/conversation_continuity_cases.json
  backend/src/nico_agent/evals/conversation_continuity.py
  backend/tests/unit/test_conversation_continuity_eval.py
  backend/tests/integration/test_conversation_continuity_e2e.py
  scripts/eval-conversation-continuity.py
  scripts/e2e-conversation-continuity.sh
  docs/handoffs/2026-07-23-conversation-continuity-goal-k-handoff.md
)

for file in "${required_files[@]}"; do
  [[ -s "$ROOT_DIR/$file" ]] \
    || die "missing or empty Conversation Continuity Goal K file: $file"
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
bash -n \
  "$ROOT_DIR/scripts/e2e-conversation-continuity.sh" \
  "$ROOT_DIR/scripts/verify-conversation-continuity-goal-k.sh"
"$ROOT_DIR/.venv/bin/python" - \
  "$ROOT_DIR/scripts/eval-conversation-continuity.py" <<'PY'
import sys
from pathlib import Path

source = Path(sys.argv[1]).read_text(encoding="utf-8")
compile(source, sys.argv[1], "exec")
PY
git -C "$ROOT_DIR" diff --check

"$ROOT_DIR/.venv/bin/python" - <<'PY'
from nico_agent.evals.conversation_continuity import load_continuity_suite

suite = load_continuity_suite()
assert len(suite.cases) == 11
assert {case.acceptance_example for case in suite.cases} == {
    f"AE{index}" for index in range(1, 12)
}
PY

for metric in \
  direct_answer_rate \
  unnecessary_clarification_rate \
  wrong_intent_rate \
  unsafe_high_risk_action_rate \
  extra_model_calls_total \
  paired_token_delta_total \
  paired_latency_delta_mean_ms; do
  rg -Fq "$metric" \
    "$ROOT_DIR/backend/src/nico_agent/evals/conversation_continuity.py" \
    || die "required continuity metric is missing: $metric"
done
rg -Fq 'credential_status="unavailable"' \
  "$ROOT_DIR/backend/src/nico_agent/evals/conversation_continuity.py" \
  || die "explicit unavailable credential record is missing"
rg -Fq 'scripts/e2e-conversation-continuity.sh' \
  "$ROOT_DIR/scripts/test-integration.sh" \
  || die "continuity E2E is not part of the real dependency gate"

python_targets=(
  "$ROOT_DIR/backend/src/nico_agent/evals/__init__.py"
  "$ROOT_DIR/backend/src/nico_agent/evals/conversation_continuity.py"
  "$ROOT_DIR/backend/tests/unit/test_conversation_continuity_eval.py"
  "$ROOT_DIR/backend/tests/integration/test_conversation_continuity_e2e.py"
  "$ROOT_DIR/scripts/eval-conversation-continuity.py"
)
"$ROOT_DIR/.venv/bin/ruff" check "${python_targets[@]}"
"$ROOT_DIR/.venv/bin/ruff" format --check "${python_targets[@]}"

"$ROOT_DIR/.venv/bin/pytest" -q \
  "$ROOT_DIR/backend/tests/unit/test_conversation_continuity_eval.py"
"$ROOT_DIR/scripts/e2e-conversation-continuity.sh"
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
  "$ROOT_DIR/docs/handoffs/2026-07-23-conversation-continuity-goal-k-handoff.md"
  "$ROOT_DIR/docs/runtime.md"
  "$ROOT_DIR/docs/testing.md"
)
if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  scan_targets+=("$NICO_EVIDENCE_DIR")
fi
if rg -n 'sk-[A-Za-z0-9]{16,}|protected-after-crash|database-password' \
  "${scan_targets[@]}"; then
  die "Goal K evidence contains a secret-shaped or protected answer value"
fi

log "PASS Conversation Continuity Goal K evaluation and closure gates"
