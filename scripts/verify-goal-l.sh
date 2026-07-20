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
python3 scripts/check-docs.py
npx --yes markdownlint-cli2@0.22.1 "*.md" "backend/*.md" "docs/*.md" ".github/*.md"
docker compose config --quiet
docker compose --profile hermes config --quiet
docker compose --profile goal-l config --quiet
scripts/test.sh
scripts/test-integration.sh
scripts/e2e.sh
scripts/e2e-goal-g.sh
scripts/e2e-goal-h.sh
scripts/e2e-goal-i.sh
scripts/e2e-goal-j.sh
scripts/e2e-goal-k.sh
scripts/e2e-goal-l.sh
EOF
fi

log "validating Goal L source, user documentation and Compose profiles"
bash -n "$ROOT_DIR"/scripts/*.sh
python3 "$ROOT_DIR/scripts/check-docs.py"
(
  cd "$ROOT_DIR"
  npx --yes markdownlint-cli2@0.22.1 "*.md" "backend/*.md" "docs/*.md" ".github/*.md"
)
"${COMPOSE[@]}" config --quiet
"${COMPOSE[@]}" --profile hermes config --quiet
"${COMPOSE[@]}" --profile goal-l config --quiet

default_services="$("${COMPOSE[@]}" config --services)"
for forbidden in worker-hermes worker-hermes-contract hermes-state-init; do
  if grep -Fxq "$forbidden" <<<"$default_services"; then
    die "default Compose unexpectedly includes optional Hermes service: $forbidden"
  fi
done

default_config="$("${COMPOSE[@]}" config)"
if grep -Eq 'NICO_HERMES_ENABLED|NICO_HERMES_STATE_ROOT' <<<"$default_config"; then
  die "default Compose unexpectedly injects or mounts Hermes configuration"
fi

log "running source, migration, dependency and base Compose regression gates"
"${COMPOSE[@]}" --profile goal-l stop api worker worker-hermes-contract fake-model >/dev/null 2>&1 || true
"$ROOT_DIR/scripts/test.sh"
"$ROOT_DIR/scripts/test-integration.sh"
"$ROOT_DIR/scripts/e2e.sh"

log "running Goal G-L Native, recovery, planning, coordination, knowledge and adapter acceptance"
for goal in g h i j k l; do
  "$ROOT_DIR/scripts/e2e-goal-${goal}.sh"
done

log "PASS Goal L compatibility closure; external credentialed model verification remains separate"

if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  "${COMPOSE[@]}" --profile goal-l ps >"$NICO_EVIDENCE_DIR/compose-ps.txt"
  cat >"$NICO_EVIDENCE_DIR/error-records.md" <<'EOF'
# Error records

No unresolved error remained in the final verification run. `verify.log` is the authoritative command and failure record.
EOF
  cat >"$NICO_EVIDENCE_DIR/acceptance-summary.md" <<'EOF'
# Goal L acceptance summary

- Hermes implements the protocol-v2 terminal outcome contract behind an explicit opt-in registry setting: passed.
- Default Compose contains no Hermes binary, provider registration, state mount, or optional service: passed.
- Optional fake/local Hermes profile succeeds with the pinned 0.18.2 compatibility contract: passed.
- Disabled, missing, mismatched, failed, cancelled, resumed and redacted Hermes paths: passed.
- Historical RuntimeSession provider/version/protocol remains authoritative with no Native fallback: passed.
- Provider implementation/capability/compatibility matrix matches Native, Mock and Hermes behavior: passed.
- Legacy provider resolution emits persisted deprecation source, Event and Audit telemetry: passed.
- Read-only Run Inspector fixed ordering, deep link, loading/empty/error/partial/cancelled/redacted and hostile text behavior: passed.
- Documentation links, Markdown style, publication-marker scan and credential-pattern scan: passed.
- Goal G-L hermetic behavior, full migration replay, unit, integration, frontend and Compose regression: passed.
- External operator-model and credentialed Hermes inference: required separately and not represented by fake evidence.
EOF
  (
    cd "$NICO_EVIDENCE_DIR"
    find . -type f ! -name manifest.sha256 -print0 | sort -z | xargs -0 sha256sum \
      >manifest.sha256
  )
fi
