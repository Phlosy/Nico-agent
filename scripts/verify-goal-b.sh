#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

log "validating shell and Compose configuration"
bash -n "$ROOT_DIR"/scripts/*.sh
"${COMPOSE[@]}" config --quiet

"$ROOT_DIR/scripts/bootstrap.sh"
"$ROOT_DIR/scripts/test.sh"
"$ROOT_DIR/scripts/test-integration.sh"
"$ROOT_DIR/scripts/e2e.sh"

log "PASS Goal B project skeleton and infrastructure"
