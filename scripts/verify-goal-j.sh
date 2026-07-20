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
docker compose --profile goal-j config --quiet
scripts/test.sh
scripts/test-integration.sh
scripts/e2e.sh
scripts/e2e-goal-j.sh
EOF
fi

log "validating Goal J source, shell and Compose configuration"
bash -n "$ROOT_DIR"/scripts/*.sh
"${COMPOSE[@]}" --profile goal-j config --quiet

log "running source, migration, dependency and legacy E2E regression gates"
"${COMPOSE[@]}" --profile goal-j stop api worker fake-model >/dev/null 2>&1 || true
"$ROOT_DIR/scripts/test.sh"
"$ROOT_DIR/scripts/test-integration.sh"
"$ROOT_DIR/scripts/e2e.sh"

log "running Goal J dynamic multi-Agent, Artifact and Worker-failure acceptance"
"$ROOT_DIR/scripts/e2e-goal-j.sh"

log "PASS Goal J code-complete dynamic coordination; external live-model verification remains credentialed"

if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  "${COMPOSE[@]}" --profile goal-j ps >"$NICO_EVIDENCE_DIR/compose-ps.txt"
  cat >"$NICO_EVIDENCE_DIR/error-records.md" <<'EOF'
# Error records

No unresolved error remained in the final verification run. `verify.log` is the authoritative command and failure record; the Goal J E2E deliberately sends SIGKILL to the active Worker while both Child model calls are in flight.
EOF
  cat >"$NICO_EVIDENCE_DIR/acceptance-summary.md" <<'EOF'
# Goal J acceptance summary

- Dynamic Parent-to-Child delegation without fixed Team or Workflow models: passed.
- Two parallel Child Runs with durable ancestry, messages and explicit budget grants: passed.
- Tenant, Parent, Child and delegation permission intersection with secret references only: passed.
- Parent suspension releases its Worker lease; all terminal children wake it transactionally: passed.
- Worker SIGKILL, expired Child lease takeover and checkpoint-based recovery: passed.
- PostgreSQL-authoritative coordination facts, reconciliation and tree cancellation: passed.
- Content-addressed private MinIO Artifacts with PostgreSQL metadata: passed.
- Child-to-direct-Parent sharing, sibling denial, hash/size verification and terminal immutability: passed.
- No anonymous MinIO access, public object keys, credentials or orphan temporary objects: passed.
- Fail-fast, best-effort and bounded model-judge aggregation policies: passed.
- Full downgrade-to-base and reapply, FORCE RLS and legacy regression suites: passed.
- External operator-model acceptance: required separately with a real endpoint and credential ref.
EOF
  (
    cd "$NICO_EVIDENCE_DIR"
    find . -type f ! -name manifest.sha256 -print0 | sort -z | xargs -0 sha256sum \
      >manifest.sha256
  )
fi
