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

load_env_values() {
  local file="$1"
  [[ -f "$file" ]] || die "environment file not found: $file"
  local python="${NICO_ENV_PYTHON:-python3}"
  local assignments
  assignments="$("$python" - "$file" <<'PY'
import re
import shlex
import sys

from dotenv import dotenv_values
from dotenv.parser import parse_stream

path = sys.argv[1]
with open(path, encoding="utf-8") as source:
    bindings = list(parse_stream(source))
if any(binding.error for binding in bindings):
    raise SystemExit(f"invalid dotenv syntax: {path}")

for key, value in dotenv_values(path).items():
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
        raise SystemExit(f"invalid environment key in {path}: {key}")
    print(f"export {key}={shlex.quote(value or '')}")
PY
)" || die "could not parse environment file: $file"
  eval "$assignments"
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
  local desired_python existing_python
  desired_python="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  existing_python=""
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    existing_python="$("$ROOT_DIR/.venv/bin/python" -c \
      'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' \
      2>/dev/null || true)"
  fi
  if [[ -x "$ROOT_DIR/.venv/bin/python" && "$existing_python" != "$desired_python" ]]; then
    log "recreating .venv for Python $desired_python (was ${existing_python:-unusable})"
    rm -rf -- "$ROOT_DIR/.venv"
  fi
  if [[ ! -x "$ROOT_DIR/.venv/bin/python" ]]; then
    python3 -m venv "$ROOT_DIR/.venv"
  fi
  "$ROOT_DIR/.venv/bin/pip" install --quiet -e "$ROOT_DIR/backend[dev]"
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
