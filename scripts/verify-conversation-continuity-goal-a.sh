#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

required_files=(
  backend/tests/unit/test_conversation_model_messages.py
  backend/tests/integration/test_conversation_model_request_audit.py
  docs/audits/2026-07-23-conversation-continuity-runtime-audit.md
  docs/brainstorms/2026-07-23-conversation-continuity-clarification-requirements.md
  docs/plans/2026-07-23-002-fix-conversation-continuity-clarification-plan.md
  docs/handoffs/2026-07-23-conversation-continuity-goal-a-handoff.md
)

for file in "${required_files[@]}"; do
  [[ -s "$ROOT_DIR/$file" ]] || die "missing or empty Conversation Continuity Goal A file: $file"
done

if git -C "$ROOT_DIR" status --short | awk '{print $2}' | grep -Eq \
  '^(backend/src|backend/migrations|frontend/src)/'; then
  die "Goal A must not modify production source or migrations"
fi

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
bash -n "$ROOT_DIR/scripts/verify-conversation-continuity-goal-a.sh"
git -C "$ROOT_DIR" diff --check

"$ROOT_DIR/.venv/bin/ruff" check \
  "$ROOT_DIR/backend/tests/unit/test_conversation_model_messages.py" \
  "$ROOT_DIR/backend/tests/integration/test_conversation_model_request_audit.py"
"$ROOT_DIR/.venv/bin/ruff" format --check \
  "$ROOT_DIR/backend/tests/unit/test_conversation_model_messages.py" \
  "$ROOT_DIR/backend/tests/integration/test_conversation_model_request_audit.py"
"$ROOT_DIR/.venv/bin/pytest" -q \
  "$ROOT_DIR/backend/tests/unit/test_conversation_model_messages.py"

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
  "$ROOT_DIR/docs/audits/2026-07-23-conversation-continuity-runtime-audit.md"
  "$ROOT_DIR/docs/handoffs/2026-07-23-conversation-continuity-goal-a-handoff.md"
)
if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  scan_targets+=("$NICO_EVIDENCE_DIR")
fi
if rg -n \
  'CC_A_AUDIT_SECRET_REFERENCE|REDACTED_TEST_REFERENCE|sk-[A-Za-z0-9]{16,}' \
  "${scan_targets[@]}"; then
  die "Goal A evidence contains a forbidden credential marker or secret-shaped value"
fi

log "PASS Conversation Continuity Goal A executable request audit gates"
