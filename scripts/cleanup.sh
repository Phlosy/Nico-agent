#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

args=(down --remove-orphans)
if [[ "${1:-}" == "--volumes" || "${1:-}" == "--all" ]]; then
  args+=(--volumes)
fi

"${COMPOSE[@]}" "${args[@]}"

if [[ "${1:-}" == "--all" ]]; then
  rm -rf "$ROOT_DIR/.venv" "$ROOT_DIR/frontend/node_modules" "$ROOT_DIR/frontend/dist"
  log "removed containers, volumes and local dependency directories"
else
  log "removed platform containers; pass --volumes to remove data or --all for local dependencies"
fi

