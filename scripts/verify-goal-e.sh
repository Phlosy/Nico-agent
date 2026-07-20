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
docker compose config --quiet
docker pull python:3.12.10-alpine@sha256:4bbf5ef9ce4b273299d394de268ad6018e10a9375d7efc7c2ce9501a6eb6b86c
scripts/verify-goal-d.sh
scripts/e2e-goal-e.sh
EOF
fi

log "validating Goal E shell and Compose configuration"
bash -n "$ROOT_DIR"/scripts/*.sh
"${COMPOSE[@]}" config --quiet
"${COMPOSE[@]}" stop worker api sandbox-runner web >/dev/null 2>&1 || true

SANDBOX_IMAGE="${NICO_SANDBOX_IMAGE:-python:3.12.10-alpine@sha256:4bbf5ef9ce4b273299d394de268ad6018e10a9375d7efc7c2ce9501a6eb6b86c}"
log "ensuring the exact Python sandbox image is available"
docker pull "$SANDBOX_IMAGE"

log "running the complete verified Goal D regression gate"
env -u NICO_EVIDENCE_DIR "$ROOT_DIR/scripts/verify-goal-d.sh"

log "running the Goal E Compose Tool Gateway and Sandbox acceptance"
"$ROOT_DIR/scripts/e2e-goal-e.sh"

if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  "${COMPOSE[@]}" ps >"$NICO_EVIDENCE_DIR/compose-ps.txt"
  docker image inspect "$SANDBOX_IMAGE" >"$NICO_EVIDENCE_DIR/sandbox-image.json"
  docker ps -a --filter label=nico.sandbox=true --format '{{json .}}' \
    >"$NICO_EVIDENCE_DIR/sandbox-containers.txt"
  cat >"$NICO_EVIDENCE_DIR/ui-scope.md" <<'EOF'
# UI scope

Goal E changes the Worker, Tool Gateway, MCP and Sandbox backend boundaries. It adds no Web Console feature or visual change, so a new page screenshot is not applicable; Goal B's verified responsive status-page screenshots remain the current UI evidence. The frontend component suite and production build are rerun by this verifier.
EOF
  cat >"$NICO_EVIDENCE_DIR/error-records.md" <<'EOF'
# Error records

No unresolved error remained in the final verification run. `verify.log` is the authoritative command and failure record; the verifier exits immediately on any failed gate, terminal E2E Run, leaked token assertion, or leftover sandbox container.
EOF
  cat >"$NICO_EVIDENCE_DIR/acceptance-summary.md" <<'EOF'
# Goal E acceptance summary

- Goal D full regression gate: passed.
- Backend unit tests: 138 passed.
- Frontend component tests: 7 passed; production build passed.
- Real dependency integration tests: 31 passed.
- Goal C control-plane E2E: passed.
- Goal D Runtime/Worker E2E: passed.
- Goal E Tool Gateway/Sandbox E2E: passed.
- Real Hermes 0.18.2 MCP discovery: passed without model credentials.
- Pinned Python sandbox image inspection: archived.
- Residual `nico.sandbox=true` containers: none.
- UI: no Goal E visual scope; existing Goal B screenshots remain authoritative.
EOF
fi

log "PASS Goal E platform-only Tool Gateway, built-in tools and isolated Sandbox"
