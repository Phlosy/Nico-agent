#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

required_files=(
  backend/migrations/versions/20260719_0017_conversations.py
  backend/src/nico_agent/conversations/__init__.py
  backend/src/nico_agent/conversations/api.py
  backend/src/nico_agent/conversations/contracts.py
  backend/src/nico_agent/conversations/service.py
  backend/src/nico_agent/cli/chat.py
  backend/src/nico_agent/cli/sse.py
  backend/tests/integration/test_conversation_api.py
  backend/tests/unit/test_cli_chat.py
  backend/tests/unit/test_cli_sse.py
  backend/tests/unit/test_conversation_contracts.py
  docs/handoffs/2026-07-19-cli-goal-c-handoff.md
  scripts/e2e-cli-goal-c.sh
)

for file in "${required_files[@]}"; do
  [[ -s "$ROOT_DIR/$file" ]] || die "missing or empty CLI Goal C file: $file"
done

grep -Fq 'down_revision: str | None = "20260718_0016"' \
  "$ROOT_DIR/backend/migrations/versions/20260719_0017_conversations.py" \
  || die "Conversation migration does not extend the prior head"
for path in \
  /conversations \
  '/conversations/{conversation_id}/turns' \
  '/conversation-turns/{turn_id}/cancel' \
  '/conversation-turns/{turn_id}/retry'; do
  rg -Fq "$path" "$ROOT_DIR/backend/src/nico_agent/conversations/api.py" \
    || die "missing Conversation API path: $path"
done

if rg -n 'from nico_agent\.(control_plane|database|domain\.models|runtime|tools|worker)' \
  "$ROOT_DIR/backend/src/nico_agent/cli"; then
  die "CLI imports a forbidden server execution implementation"
fi

python3 "$ROOT_DIR/scripts/check-docs.py"
(
  cd "$ROOT_DIR"
  npx --yes markdownlint-cli2@0.22.1 "*.md" "backend/*.md" "docs/*.md" ".github/*.md"
)
bash -n "$ROOT_DIR"/scripts/*.sh
git -C "$ROOT_DIR" diff --check
"${COMPOSE[@]}" config --quiet

"$ROOT_DIR/scripts/test.sh"
"$ROOT_DIR/scripts/test-integration.sh"
"$ROOT_DIR/scripts/e2e-cli-goal-b.sh"
"$ROOT_DIR/scripts/e2e-cli-goal-c.sh"

printf 'PASS CLI Goal C durable Conversation and streaming chat\n'
