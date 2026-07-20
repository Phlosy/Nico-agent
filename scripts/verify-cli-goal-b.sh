#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

required_files=(
  backend/src/nico_agent/cli/__init__.py
  backend/src/nico_agent/cli/__main__.py
  backend/src/nico_agent/cli/app.py
  backend/src/nico_agent/cli/client.py
  backend/src/nico_agent/cli/config.py
  backend/src/nico_agent/cli/errors.py
  backend/src/nico_agent/cli/output.py
  backend/tests/unit/test_cli_app.py
  backend/tests/unit/test_cli_client.py
  backend/tests/unit/test_cli_config.py
  backend/tests/unit/test_cli_output.py
  docs/cli.md
  docs/cli-development.md
  docs/handoffs/2026-07-19-cli-goal-b-handoff.md
  scripts/e2e-cli-goal-b.sh
)

for file in "${required_files[@]}"; do
  [[ -s "$ROOT_DIR/$file" ]] || die "missing or empty CLI Goal B file: $file"
done

grep -Fq 'nico = "nico_agent.cli.app:run"' "$ROOT_DIR/backend/pyproject.toml" \
  || die "nico project script is missing"
for dependency in typer rich prompt-toolkit platformdirs httpx; do
  grep -Fq "\"$dependency==" "$ROOT_DIR/backend/pyproject.toml" \
    || die "CLI dependency is not pinned: $dependency"
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

printf 'PASS CLI Goal B base client, profiles, output and real API commands\n'
