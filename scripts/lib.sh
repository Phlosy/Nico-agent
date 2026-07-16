#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose --project-directory "$ROOT_DIR" --file "$ROOT_DIR/docker-compose.yml")

log() {
  printf '[nico] %s\n' "$*"
}

die() {
  printf '[nico] ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

ensure_env_file() {
  if [[ ! -f "$ROOT_DIR/.env" ]]; then
    cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
    log "created .env from .env.example"
  fi
}

load_env_file() {
  ensure_env_file
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
}

ensure_python_environment() {
  require_command python3
  if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
    python3 -m venv "$ROOT_DIR/.venv"
  fi
  "$ROOT_DIR/.venv/bin/pip" install -e "$ROOT_DIR/backend[dev]"
}

ensure_frontend_environment() {
  require_command npm
  npm --prefix "$ROOT_DIR/frontend" ci
}

wait_for_url() {
  local url="$1"
  local label="$2"
  local attempts="${3:-60}"
  local delay="${4:-2}"
  for ((attempt = 1; attempt <= attempts; attempt += 1)); do
    if curl --fail --silent --show-error --max-time 3 "$url" >/dev/null 2>&1; then
      log "$label is ready"
      return 0
    fi
    sleep "$delay"
  done
  die "$label did not become ready: $url"
}

wait_for_service_health() {
  local service="$1"
  local attempts="${2:-30}"
  local container_id
  container_id="$("${COMPOSE[@]}" ps --quiet "$service")"
  [[ -n "$container_id" ]] || die "service has no container: $service"

  for ((attempt = 1; attempt <= attempts; attempt += 1)); do
    local health_state
    health_state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")"
    if [[ "$health_state" == "healthy" ]]; then
      log "$service container is healthy"
      return 0
    fi
    if [[ "$health_state" == "unhealthy" || "$health_state" == "exited" ]]; then
      "${COMPOSE[@]}" logs --no-color --tail=50 "$service" >&2
      die "$service entered $health_state state"
    fi
    sleep 2
  done
  die "$service container did not become healthy"
}
