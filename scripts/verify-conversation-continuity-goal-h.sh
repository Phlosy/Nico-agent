#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

required_files=(
  backend/src/nico_agent/user_inputs/api.py
  backend/src/nico_agent/user_inputs/service.py
  backend/src/nico_agent/cli/client.py
  backend/src/nico_agent/cli/chat_session.py
  backend/src/nico_agent/cli/renderers.py
  backend/src/nico_agent/cli/user_inputs.py
  backend/src/nico_agent/testing/user_input_e2e_server.py
  backend/tests/unit/test_cli_client.py
  backend/tests/unit/test_cli_chat.py
  backend/tests/unit/test_cli_user_input.py
  backend/tests/integration/test_user_input_api.py
  scripts/e2e-runtime-user-input.sh
  docs/handoffs/2026-07-23-conversation-continuity-goal-h-handoff.md
)

for file in "${required_files[@]}"; do
  [[ -s "$ROOT_DIR/$file" ]] \
    || die "missing or empty Conversation Continuity Goal H file: $file"
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
bash -n "$ROOT_DIR/scripts/e2e-runtime-user-input.sh"
bash -n "$ROOT_DIR/scripts/verify-conversation-continuity-goal-h.sh"
git -C "$ROOT_DIR" diff --check

rg -Fq 'user-input-requests/{request_id}/answer' \
  "$ROOT_DIR/backend/src/nico_agent/user_inputs/api.py" \
  "$ROOT_DIR/backend/src/nico_agent/cli/client.py" \
  || die "UserInput answer API/client contract is missing"
rg -q 'list_user_inputs' "$ROOT_DIR/backend/src/nico_agent/cli/chat_session.py" \
  || die "CLI restart does not discover pending UserInput requests"
rg -q '_USER_INPUT_PROMPT' "$ROOT_DIR/backend/src/nico_agent/cli/chat_session.py" \
  || die "Agent question does not have a distinct input owner"
rg -q 'client.answer_user_input' "$ROOT_DIR/backend/src/nico_agent/cli/user_inputs.py" \
  || die "CLI answer does not use the dedicated UserInput endpoint"
if rg -n 'create_conversation_turn' "$ROOT_DIR/backend/src/nico_agent/cli/user_inputs.py"; then
  die "UserInput answer incorrectly creates a Conversation Turn"
fi
rg -q 'ACTION_KIND_UNSUPPORTED' \
  "$ROOT_DIR/backend/tests/unit/test_native_direct_runtime.py" \
  || die "live ask_user Schema guard was removed before U9"

python_targets=(
  "$ROOT_DIR/backend/src/nico_agent/api.py"
  "$ROOT_DIR/backend/src/nico_agent/user_inputs/api.py"
  "$ROOT_DIR/backend/src/nico_agent/user_inputs/service.py"
  "$ROOT_DIR/backend/src/nico_agent/cli/client.py"
  "$ROOT_DIR/backend/src/nico_agent/cli/chat_session.py"
  "$ROOT_DIR/backend/src/nico_agent/cli/renderers.py"
  "$ROOT_DIR/backend/src/nico_agent/cli/user_inputs.py"
  "$ROOT_DIR/backend/src/nico_agent/testing/user_input_e2e_server.py"
  "$ROOT_DIR/backend/tests/unit/test_cli_client.py"
  "$ROOT_DIR/backend/tests/unit/test_cli_chat.py"
  "$ROOT_DIR/backend/tests/unit/test_cli_user_input.py"
  "$ROOT_DIR/backend/tests/integration/test_user_input_api.py"
)
"$ROOT_DIR/.venv/bin/ruff" check "${python_targets[@]}"
"$ROOT_DIR/.venv/bin/ruff" format --check "${python_targets[@]}"

"$ROOT_DIR/.venv/bin/pytest" -q \
  "$ROOT_DIR/backend/tests/unit/test_cli_chat.py" \
  "$ROOT_DIR/backend/tests/unit/test_cli_client.py" \
  "$ROOT_DIR/backend/tests/unit/test_cli_renderers.py" \
  "$ROOT_DIR/backend/tests/unit/test_cli_user_input.py"
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

"$ROOT_DIR/scripts/e2e-runtime-user-input.sh"

scan_targets=(
  "$ROOT_DIR/docs/handoffs/2026-07-23-conversation-continuity-goal-h-handoff.md"
  "$ROOT_DIR/docs/cli.md"
  "$ROOT_DIR/docs/runtime.md"
)
if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  scan_targets+=("$NICO_EVIDENCE_DIR")
fi
if rg -n 'sk-[A-Za-z0-9]{16,}|protected-after-crash|database-password' \
  "${scan_targets[@]}"; then
  die "Goal H evidence contains a secret-shaped or protected answer value"
fi

log "PASS Conversation Continuity Goal H UserInput API and CLI gates"
