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
fi

log "validating shell and Compose configuration"
bash -n "$ROOT_DIR"/scripts/*.sh
"${COMPOSE[@]}" config --quiet

"$ROOT_DIR/scripts/bootstrap.sh"
"$ROOT_DIR/scripts/test.sh"
"$ROOT_DIR/scripts/test-integration.sh"
"$ROOT_DIR/scripts/e2e-goal-c.sh"
"$ROOT_DIR/scripts/e2e-goal-d.sh"

if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  "${COMPOSE[@]}" ps >"$NICO_EVIDENCE_DIR/compose-ps.txt"
fi

log "PASS Goal D runtime providers, leased worker, recovery and trajectory"
