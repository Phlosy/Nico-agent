#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command docker
ensure_env_file
"${COMPOSE[@]}" config --quiet

log "pulling pinned infrastructure images"
"${COMPOSE[@]}" pull postgres redis minio minio-init
log "building API, worker and Web images"
"${COMPOSE[@]}" build api worker web
log "bootstrap complete; run scripts/dev.sh"

