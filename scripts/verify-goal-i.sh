#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  mkdir -p "$NICO_EVIDENCE_DIR"
  exec > >(tee "$NICO_EVIDENCE_DIR/verify.log") 2>&1
  {
    date -u +'%Y-%m-%dT%H:%M:%SZ'
    git -C "$ROOT_DIR" rev-parse HEAD
    git -C "$ROOT_DIR" status --short
    python3 --version
    docker --version
    docker compose version
  } >"$NICO_EVIDENCE_DIR/environment.txt"
  cat >"$NICO_EVIDENCE_DIR/commands.txt" <<'EOF'
bash -n scripts/*.sh
docker compose --profile goal-g config --quiet
scripts/test.sh
scripts/test-integration.sh
scripts/e2e.sh
scripts/e2e-goal-i.sh
EOF
fi

log "validating Goal I source, shell and Compose configuration"
bash -n "$ROOT_DIR"/scripts/*.sh
"${COMPOSE[@]}" --profile goal-g config --quiet

log "running source, migration, dependency and legacy E2E regression gates"
"${COMPOSE[@]}" --profile goal-g stop api worker fake-model >/dev/null 2>&1 || true
"$ROOT_DIR/scripts/test.sh"
"$ROOT_DIR/scripts/test-integration.sh"
"$ROOT_DIR/scripts/e2e.sh"

log "running Goal I hermetic Plan/Reflection/Completion acceptance"
"$ROOT_DIR/scripts/e2e-goal-i.sh"

log "PASS Goal I code-complete planning runtime; external live-model verification remains credentialed"

if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  "${COMPOSE[@]}" --profile goal-g ps >"$NICO_EVIDENCE_DIR/compose-ps.txt"
  cat >"$NICO_EVIDENCE_DIR/acceptance-summary.md" <<'EOF'
# Goal I acceptance summary

- Bounded structured Plan and DAG validation: passed.
- Append-only Plan revisions and old revision read API: passed.
- Failed execution validation, constrained Reflection and corrective Replan: passed.
- Deterministic Completion Evaluation before optional billed model judge: passed.
- Separate ModelCall accounting for Planner, Reflection, execution and judge: passed.
- Compact schema-v3 checkpoint and recovery without rebilling completed model calls: passed.
- PlanStep exact-version Tool Gateway execution, pre-action checkpoint and parent trace: passed.
- PostgreSQL migration 0012 upgrade, full downgrade-to-base and reapply: passed.
- RLS, immutable Plan semantics and append-only runtime evaluations: passed.
- Existing Direct, ReAct, Tool Gateway, growth and control-plane regressions: passed.
- External operator-model acceptance: required separately with a real endpoint and credential ref.
EOF
  (
    cd "$NICO_EVIDENCE_DIR"
    find . -type f ! -name manifest.sha256 -print0 | sort -z | xargs -0 sha256sum \
      >manifest.sha256
  )
fi
