#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

required_files=(
  backend/migrations/versions/20260722_0026_chat_session_controls.py
  backend/src/nico_agent/cli/chat_controls.py
  backend/src/nico_agent/cli/chat_session.py
  backend/src/nico_agent/conversations/api.py
  backend/src/nico_agent/tool_approvals/service.py
  backend/tests/integration/test_conversation_api.py
  backend/tests/integration/test_runtime_leasing.py
  backend/tests/integration/test_tool_gateway.py
  docs/cli.md
  docs/cli-development.md
  docs/testing.md
  scripts/e2e-cli-session-controls.sh
  scripts/verify-cli-session-controls.sh
)

for file in "${required_files[@]}"; do
  [[ -s "$ROOT_DIR/$file" ]] || die "missing or empty session-controls file: $file"
done

for command in '"permissions"' '"queue"'; do
  rg -Fq "$command" "$ROOT_DIR/backend/src/nico_agent/cli/slash.py" \
    || die "missing session-controls slash command: $command"
done

if rg -n 'from nico_agent\.(control_plane|database|domain\.models|runtime|tools|worker)' \
  "$ROOT_DIR/backend/src/nico_agent/cli"; then
  die "CLI imports a forbidden server execution implementation"
fi

if rg -n 'wait and send later|等待或取消|明确选择 local guidance' \
  "$ROOT_DIR/backend/src/nico_agent/cli" "$ROOT_DIR/docs/cli.md"; then
  die "stale active-Run input guidance remains"
fi

python3 "$ROOT_DIR/scripts/check-docs.py"
(
  cd "$ROOT_DIR"
  npx --yes markdownlint-cli2@0.22.1 \
    "*.md" "backend/*.md" "docs/*.md" "docs/progress/feature-matrix.md" ".github/*.md"
)
bash -n "$ROOT_DIR"/scripts/*.sh
git -C "$ROOT_DIR" diff --check
"${COMPOSE[@]}" config --quiet

"$ROOT_DIR/.venv/bin/ruff" check "$ROOT_DIR/backend/src" "$ROOT_DIR/backend/tests"
"$ROOT_DIR/.venv/bin/ruff" format --check "$ROOT_DIR/backend/src" "$ROOT_DIR/backend/tests"
"$ROOT_DIR/.venv/bin/pytest" -q \
  "$ROOT_DIR/backend/tests/unit/test_conversation_contracts.py" \
  "$ROOT_DIR/backend/tests/unit/test_tool_approvals.py" \
  "$ROOT_DIR/backend/tests/unit/test_cli_client.py" \
  "$ROOT_DIR/backend/tests/unit/test_cli_slash.py" \
  "$ROOT_DIR/backend/tests/unit/test_cli_chat.py" \
  "$ROOT_DIR/backend/tests/unit/test_cli_renderers.py" \
  "$ROOT_DIR/backend/tests/unit/test_cli_execution.py" \
  "$ROOT_DIR/backend/tests/unit/test_cli_app.py"

"$ROOT_DIR/scripts/test-integration.sh"
"$ROOT_DIR/scripts/e2e-cli-goal-c.sh"
"$ROOT_DIR/scripts/e2e-cli-goal-d.sh"
"$ROOT_DIR/scripts/e2e-cli-goal-e.sh"
"$ROOT_DIR/scripts/e2e-cli-goal-f.sh"
"$ROOT_DIR/scripts/e2e-cli-session-controls.sh"

printf 'PASS CLI durable queue, approval modes and asynchronous session controls\n'
