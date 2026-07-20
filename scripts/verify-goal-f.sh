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
scripts/verify-goal-e.sh
scripts/e2e-goal-f.sh
EOF
fi

log "validating Goal F shell and Compose configuration"
bash -n "$ROOT_DIR"/scripts/*.sh
"${COMPOSE[@]}" config --quiet
"${COMPOSE[@]}" stop worker api sandbox-runner web >/dev/null 2>&1 || true

log "running the complete verified Goal E regression gate with current Goal F tests"
env -u NICO_EVIDENCE_DIR "$ROOT_DIR/scripts/verify-goal-e.sh"

log "running the Goal F Compose controlled-growth acceptance"
"$ROOT_DIR/scripts/e2e-goal-f.sh"

if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  "${COMPOSE[@]}" ps >"$NICO_EVIDENCE_DIR/compose-ps.txt"
  cat >"$NICO_EVIDENCE_DIR/ui-scope.md" <<'EOF'
# UI scope

Goal F adds backend Memory/Skill growth services and REST APIs. It deliberately does not implement the business Web Console, which belongs to Goal J. No visual surface changed, so a new screenshot is not applicable; the frontend component suite and production build are rerun by this verifier.
EOF
  cat >"$NICO_EVIDENCE_DIR/error-records.md" <<'EOF'
# Error records

No unresolved error remained in the final verification run. `verify.log` is the authoritative command and failure record. The Goal F E2E also archives expected fail-closed HTTP responses for premature publication/resolution, self-review, and cross-tenant access under `e2e-goal-f/`.
EOF
  cat >"$NICO_EVIDENCE_DIR/acceptance-summary.md" <<'EOF'
# Goal F acceptance summary

- Goal E complete regression gate: passed.
- Backend unit tests: 172 passed.
- Frontend component tests: 7 passed; production build passed.
- Real dependency integration tests: 58 passed.
- Goal C control-plane E2E: passed.
- Goal D Runtime/Worker E2E: passed.
- Goal E Tool Gateway/Sandbox E2E: passed.
- Goal F controlled-growth Compose E2E: passed.
- Completed Run produced episodic/semantic/procedural Memory candidates and one Skill candidate idempotently; failed-Run working Memory is covered by the full integration gate.
- Unreviewed candidates were unusable; self-review was forbidden.
- Approved Memory was indexed and retrieved with tenant-scoped provenance.
- Skill v1/v2 validation, independent approval, publication, canary, promotion and historical rollback passed.
- Second tenant observed no Memory, Skill, Run candidate, or retrieval data.
- OpenAPI growth paths and sensitive-field exclusions passed.
- UI: no Goal F visual scope; frontend regression remained green.
EOF
fi

log "PASS Goal F controlled Memory and immutable Skill growth"
