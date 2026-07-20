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
docker compose --profile goal-h config --quiet
scripts/test.sh
scripts/test-integration.sh
scripts/e2e.sh
scripts/e2e-goal-g.sh
scripts/e2e-goal-h.sh
EOF
fi

log "validating Goal H source, shell and Compose configuration"
bash -n "$ROOT_DIR"/scripts/*.sh
"${COMPOSE[@]}" --profile goal-h config --quiet

log "running full source, migration and real-dependency regression gates"
"${COMPOSE[@]}" --profile goal-h stop api worker fake-model >/dev/null 2>&1 || true
"$ROOT_DIR/scripts/test.sh"
"$ROOT_DIR/scripts/test-integration.sh"
"$ROOT_DIR/scripts/e2e.sh"

log "running native Direct regression and ReAct fault-injection acceptance"
env -u NICO_EVIDENCE_DIR "$ROOT_DIR/scripts/e2e-goal-g.sh"
"$ROOT_DIR/scripts/e2e-goal-h.sh"

log "PASS Goal H code-complete ReAct recovery; external live-model verification remains credentialed"

if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  "${COMPOSE[@]}" --profile goal-h ps >"$NICO_EVIDENCE_DIR/compose-ps.txt"
  cat >"$NICO_EVIDENCE_DIR/error-records.md" <<'EOF'
# Error records

No unresolved error remained in the final verification run. `verify.log` is the authoritative command and failure record; every gate exits non-zero on failure. The Goal H E2E deliberately kills the first Worker after `file.write` succeeds and archives only non-secret container state.
EOF
  cat >"$NICO_EVIDENCE_DIR/acceptance-summary.md" <<'EOF'
# Goal H acceptance summary

- Native ReAct multi-round model/tool/observation loop: passed.
- Exact-version Tool Gateway authorization, budgets and deterministic observations: passed.
- Versioned integrity-checked pre-action and post-observation checkpoints: passed.
- PostgreSQL migration 0011 upgrade, full downgrade-to-base and reapply: passed.
- RunStep/ToolCall/ModelCall/ContextSnapshot/Event/Audit matching facts: passed.
- Real Compose Worker SIGKILL after successful `file.write`: passed.
- Expired-lease takeover by a distinct Worker: passed.
- Successful side effect replay prevention through stable idempotency key/cache: passed.
- Recovered Python sandbox call and final model answer: passed.
- Runtime event sequence continuity and evidence credential scan: passed.
- Nico Native Direct regression without Hermes: passed.
- External operator-model acceptance: required separately when a real endpoint and credential ref are supplied.
EOF
  (
    cd "$NICO_EVIDENCE_DIR"
    find . -type f ! -name manifest.sha256 -print0 | sort -z | xargs -0 sha256sum \
      >manifest.sha256
  )
fi
