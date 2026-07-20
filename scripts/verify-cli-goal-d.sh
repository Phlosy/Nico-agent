#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

required_files=(
  backend/src/nico_agent/cli/execution.py
  backend/src/nico_agent/cli/logo.py
  backend/src/nico_agent/cli/renderers.py
  backend/src/nico_agent/cli/slash.py
  backend/tests/unit/test_cli_execution.py
  backend/tests/unit/test_cli_logo.py
  backend/tests/unit/test_cli_renderers.py
  backend/tests/unit/test_cli_slash.py
  docs/handoffs/2026-07-19-cli-goal-d-handoff.md
  scripts/e2e-cli-goal-d.sh
)

for file in "${required_files[@]}"; do
  [[ -s "$ROOT_DIR/$file" ]] || die "missing or empty CLI Goal D file: $file"
done

for command in '@app.command("exec")' '@run_app.command("watch")'; do
  rg -Fq "$command" "$ROOT_DIR/backend/src/nico_agent/cli/app.py" \
    || die "missing CLI Goal D command registration: $command"
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

printf 'PASS CLI Goal D first-class exec, watch, slash and terminal rendering\n'
