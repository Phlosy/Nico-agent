#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command docker
ensure_env_file
"${COMPOSE[@]}" config --quiet

log "pulling pinned infrastructure images"
"${COMPOSE[@]}" pull postgres redis minio minio-init
SANDBOX_IMAGE="${NICO_SANDBOX_IMAGE:-python:3.12.10-alpine@sha256:4bbf5ef9ce4b273299d394de268ad6018e10a9375d7efc7c2ce9501a6eb6b86c}"
log "pulling pinned Python sandbox image"
docker pull "$SANDBOX_IMAGE"
log "building API, worker, Sandbox Runner and Web images"
"${COMPOSE[@]}" build api worker sandbox-runner web
log "bootstrap complete; run scripts/dev.sh"
