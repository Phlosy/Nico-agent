#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command docker
require_command curl
ensure_env_file

if [[ "${1:-}" == "--detach" ]]; then
  log "starting the platform in the background"
  "${COMPOSE[@]}" up --detach --build
  load_env_file
  wait_for_url "http://localhost:${API_PORT:-18000}/api/v1/health/ready" "API"
  wait_for_url "http://localhost:${WEB_PORT:-18080}/healthz" "Web"
  wait_for_service_health api
  wait_for_service_health web
  "${COMPOSE[@]}" ps
else
  log "starting the platform; use Ctrl-C to stop attached logs"
  "${COMPOSE[@]}" up --build
fi
