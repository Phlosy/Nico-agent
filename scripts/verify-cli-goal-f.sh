#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

required_files=(
  backend/migrations/versions/20260719_0019_tool_approvals.py
  backend/src/nico_agent/tool_approvals/api.py
  backend/src/nico_agent/tool_approvals/service.py
  backend/tests/unit/test_tool_approvals.py
  backend/tests/integration/test_native_react_runtime.py
  docs/handoffs/2026-07-19-cli-goal-f-handoff.md
  scripts/e2e-cli-goal-f.sh
)

for file in "${required_files[@]}"; do
  [[ -s "$ROOT_DIR/$file" ]] || die "missing or empty CLI Goal F file: $file"
done

for command in '"approvals"' '"approve"' '"reject"'; do
  rg -Fq "$command" "$ROOT_DIR/backend/src/nico_agent/cli/slash.py" \
    || die "missing CLI Goal F slash command: $command"
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
"$ROOT_DIR/scripts/e2e-cli-goal-d.sh"
"$ROOT_DIR/scripts/e2e-cli-goal-e.sh"
"$ROOT_DIR/scripts/e2e-cli-goal-f.sh"

printf 'PASS CLI Goal F durable approval, recovery, reconnect and audit\n'
