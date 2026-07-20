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
docker compose --profile goal-k config --quiet
scripts/test.sh
scripts/test-integration.sh
scripts/e2e.sh
scripts/e2e-goal-k.sh
EOF
fi

log "validating Goal K source, shell and Compose configuration"
bash -n "$ROOT_DIR"/scripts/*.sh
"${COMPOSE[@]}" --profile goal-k config --quiet

log "running source, migration, dependency and legacy E2E regression gates"
"${COMPOSE[@]}" --profile goal-k stop api worker fake-model >/dev/null 2>&1 || true
"$ROOT_DIR/scripts/test.sh"
"$ROOT_DIR/scripts/test-integration.sh"
"$ROOT_DIR/scripts/e2e.sh"

log "running Goal K published Memory/Skill runtime acceptance"
"$ROOT_DIR/scripts/e2e-goal-k.sh"

log "PASS Goal K code-complete runtime knowledge integration; live external model verification remains credentialed"

if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  "${COMPOSE[@]}" --profile goal-k ps >"$NICO_EVIDENCE_DIR/compose-ps.txt"
  cat >"$NICO_EVIDENCE_DIR/error-records.md" <<'EOF'
# Error records

No unresolved error remained in the final verification run. `verify.log` is the authoritative command and failure record.
EOF
  cat >"$NICO_EVIDENCE_DIR/acceptance-summary.md" <<'EOF'
# Goal K acceptance summary

- Tenant ∩ AgentVersion Memory/Skill policies with explicit scopes and caps: passed.
- Published, active, unexpired Memory recall through pgvector: passed.
- Published stable Skill resolution with candidate/draft exclusion: passed.
- Exact source ID, version and content hash frozen per Run: passed.
- Published knowledge is rendered as untrusted data, never as permission: passed.
- ContextSnapshot and ModelCall consumption linkage with counters: passed.
- Terminal outcome/effect metadata and Growth trajectory propagation: passed.
- Child knowledge policy can only narrow Parent and delegation permissions: passed.
- Public runtime API omits the private content-bearing selection snapshot: passed.
- Full downgrade-to-base and reapply, FORCE RLS and legacy regressions: passed.
- External operator-model acceptance: required separately with a real endpoint and credential ref.
EOF
  (
    cd "$NICO_EVIDENCE_DIR"
    find . -type f ! -name manifest.sha256 -print0 | sort -z | xargs -0 sha256sum \
      >manifest.sha256
  )
fi
