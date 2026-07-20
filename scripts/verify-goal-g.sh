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
scripts/e2e-goal-g.sh
EOF
fi

log "validating Goal G source, shell and Compose configuration"
bash -n "$ROOT_DIR"/scripts/*.sh
"${COMPOSE[@]}" --profile goal-g config --quiet

log "running full source, migration and real-dependency regression gates"
"${COMPOSE[@]}" --profile goal-g stop api worker fake-model >/dev/null 2>&1 || true
"$ROOT_DIR/scripts/test.sh"
"$ROOT_DIR/scripts/test-integration.sh"
"$ROOT_DIR/scripts/e2e.sh"

log "running the hermetic OpenAI-compatible Compose acceptance"
"$ROOT_DIR/scripts/e2e-goal-g.sh"

log "PASS Goal G code-complete native runtime; external live-model verification remains credentialed"

if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  "${COMPOSE[@]}" --profile goal-g ps >"$NICO_EVIDENCE_DIR/compose-ps.txt"
  cat >"$NICO_EVIDENCE_DIR/error-records.md" <<'EOF'
# Error records

No unresolved error remained in the final verification run. `verify.log` is the authoritative command and failure record; every gate exits non-zero on failure.
EOF
  cat >"$NICO_EVIDENCE_DIR/acceptance-summary.md" <<'EOF'
# Goal G acceptance summary

- Runtime protocol v2 and v1 terminal compatibility: passed.
- `nico_native` direct runtime without Hermes: passed.
- OpenAI-compatible streaming, usage, tool-delta parsing, retry and redaction: passed.
- ModelCall and ContextSnapshot RLS/immutability persistence: passed.
- Resumable SSE event query: passed.
- Hermetic Compose fake-model acceptance and zero credential leakage scan: passed.
- External operator model acceptance: required separately when `NICO_TEST_MODEL_*` credentials are supplied.
EOF
  (
    cd "$NICO_EVIDENCE_DIR"
    find . -type f ! -name manifest.sha256 -print0 | sort -z | xargs -0 sha256sum >manifest.sha256
  )
fi
