#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

DRY_RUN=false
ACTION=""
LOCAL_API_PORT=8000
LOCAL_WEB_PORT=5173
LOCAL_SANDBOX_PORT=8090
DEV_BIN_DIR="${NICO_BIN_DIR:-$HOME/.local/bin}"
DEV_HOME="${NICO_DEV_HOME:-$ROOT_DIR/.nico/dev}"
if [[ "$DEV_HOME" != /* ]]; then
  DEV_HOME="$ROOT_DIR/${DEV_HOME#./}"
fi
DEV_CONFIG_FILE="$DEV_HOME/config/cli.toml"
DEV_SECRET_FILE="$DEV_HOME/config/model-secrets.env"
DEV_SERVICE_COMMAND="$DEV_HOME/bin/nico-service"
DEV_CONTROL_DIR="$DEV_HOME/run"
DEV_RESTART_REQUEST="$DEV_CONTROL_DIR/worker-restart-request.json"
DEV_RESTART_RESPONSE="$DEV_CONTROL_DIR/worker-restart-response.json"
DEV_SUPERVISOR_PID="$DEV_CONTROL_DIR/supervisor.pid"

usage() {
  printf '%s\n' \
    'Usage: scripts/local-dev.sh [--dry-run] <command>' \
    '' \
    'Commands:' \
    '  setup       Install the editable backend and frontend dependencies' \
    '  infra-up    Start PostgreSQL, Redis and MinIO with Compose' \
    '  run         Start infrastructure, migrations and source processes' \
    '  infra-down  Stop development infrastructure and preserve its volumes'
}

while (($#)); do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    setup | infra-up | run | infra-down)
      [[ -z "$ACTION" ]] || die "only one command may be selected"
      ACTION="$1"
      shift
      ;;
    *) die "unknown option: $1" ;;
  esac
done

[[ -n "$ACTION" ]] || {
  usage >&2
  exit 2
}

print_command() {
  printf '[nico-dev] $'
  printf ' %q' "$@"
  printf '\n'
}

web_search_local_enabled() {
  case "${NICO_DEV_WEB_SEARCH:-brave}" in
    brave) return 1 ;;
    searxng) return 0 ;;
    *) die "NICO_DEV_WEB_SEARCH must be brave or searxng" ;;
  esac
}

load_local_environment() {
  if [[ "$DRY_RUN" == false ]]; then
    ensure_env_file
    load_env_values "$ROOT_DIR/.env"
  fi

  export NICO_ENVIRONMENT=development
  export NICO_DEV_ROOT="$ROOT_DIR"
  export NICO_DEV_HOME="$DEV_HOME"
  export NICO_HOME="$DEV_HOME"
  export NICO_CONFIG_FILE="$DEV_CONFIG_FILE"
  export NICO_PROFILE=development
  export NICO_DATABASE_HOST=127.0.0.1
  export NICO_DATABASE_PORT="${POSTGRES_PORT:-15432}"
  export NICO_DATABASE_NAME="${POSTGRES_DB:-nico_agent}"
  export NICO_DATABASE_USER="${POSTGRES_USER:-nico}"
  export NICO_DATABASE_PASSWORD="${POSTGRES_PASSWORD:-nico-change-me}"
  export NICO_REDIS_URL="redis://127.0.0.1:${REDIS_PORT:-16379}/0"
  export NICO_MINIO_URL="http://127.0.0.1:${MINIO_API_PORT:-19010}"
  export NICO_MINIO_ACCESS_KEY="${MINIO_ROOT_USER:-nico-minio}"
  export NICO_MINIO_SECRET_KEY="${MINIO_ROOT_PASSWORD:-nico-minio-change-me}"
  export NICO_MINIO_BUCKET="${MINIO_BUCKET:-nico-artifacts}"
  export NICO_API_HOST=127.0.0.1
  export NICO_API_PORT="$LOCAL_API_PORT"
  export NICO_SANDBOX_RUNNER_URL="http://127.0.0.1:$LOCAL_SANDBOX_PORT"
  export NICO_WORKSPACE_ROOT="${NICO_WORKSPACE_ROOT:-$ROOT_DIR/.nico/workspaces}"
  export NICO_WORKER_HEALTH_MARKER="${NICO_WORKER_HEALTH_MARKER:-$ROOT_DIR/.nico/worker-ready}"
  export NICO_MODEL_SECRETS_FILE="$DEV_SECRET_FILE"
  export NICO_MODEL_ENDPOINT_WRITES_ENABLED="${NICO_MODEL_ENDPOINT_WRITES_ENABLED:-true}"
  export NICO_WEB_PROVIDER_WRITES_ENABLED="${NICO_WEB_PROVIDER_WRITES_ENABLED:-true}"
  if web_search_local_enabled; then
    export NICO_WEB_SEARXNG_ENDPOINT="http://127.0.0.1:${SEARXNG_PORT:-18888}/search"
    export NICO_WEB_SEARXNG_ALLOW_PRIVATE=true
  fi
  export NICO_CORS_ORIGINS="[\"http://localhost:$LOCAL_WEB_PORT\",\"http://127.0.0.1:$LOCAL_WEB_PORT\"]"
  export PYTHONPATH="$ROOT_DIR/backend/src${PYTHONPATH:+:$PYTHONPATH}"

}

prepare_development_home() {
  if [[ "$DRY_RUN" == true ]]; then
    print_command install -d -m 700 "$DEV_HOME" "$DEV_HOME/bin" \
      "$DEV_HOME/config" "$DEV_HOME/state" "$DEV_CONTROL_DIR"
    print_command install -m 700 "$ROOT_DIR/scripts/nico-dev-service" "$DEV_SERVICE_COMMAND"
    print_command install -m 600 /dev/null "$DEV_SECRET_FILE"
    return
  fi

  install -d -m 700 "$DEV_HOME" "$DEV_HOME/bin" \
    "$DEV_HOME/config" "$DEV_HOME/state" "$DEV_CONTROL_DIR"
  local directory
  for directory in "$DEV_HOME" "$DEV_HOME/bin" "$DEV_HOME/config" \
    "$DEV_HOME/state" "$DEV_CONTROL_DIR"; do
    [[ -d "$directory" && ! -L "$directory" ]] || \
      die "development state directories must not be symlinks: $directory"
  done
  chmod 700 "$DEV_HOME" "$DEV_HOME/bin" "$DEV_HOME/config" \
    "$DEV_HOME/state" "$DEV_CONTROL_DIR"
  if [[ -e "$DEV_SERVICE_COMMAND" || -L "$DEV_SERVICE_COMMAND" ]]; then
    [[ -f "$DEV_SERVICE_COMMAND" && ! -L "$DEV_SERVICE_COMMAND" ]] || \
      die "development service command must be a regular file"
  fi
  install -m 700 "$ROOT_DIR/scripts/nico-dev-service" "$DEV_SERVICE_COMMAND"
  if [[ -e "$DEV_SECRET_FILE" || -L "$DEV_SECRET_FILE" ]]; then
    [[ -f "$DEV_SECRET_FILE" && ! -L "$DEV_SECRET_FILE" ]] || \
      die "development model secret store must be a regular file"
  else
    install -m 600 /dev/null "$DEV_SECRET_FILE"
  fi
  chmod 600 "$DEV_SECRET_FILE"
}

stop_containerized_app_services() {
  local running
  running="$("${COMPOSE[@]}" ps --services \
    --status running --status restarting --status paused)"
  local application_services=()
  local service
  while IFS= read -r service; do
    [[ -n "$service" ]] || continue
    case "$service" in
      postgres | redis | minio | minio-init | searxng | fake-model) ;;
      api | worker | worker-hermes | worker-hermes-contract | sandbox-runner | web)
        application_services+=("$service")
        ;;
      *) die "unrecognized Compose service is running: $service" ;;
    esac
  done <<< "$running"
  if ((${#application_services[@]})); then
    log "stopping containerized application services before source startup"
    "${COMPOSE[@]}" stop "${application_services[@]}"
  fi
}

wait_for_completed_service() {
  local service="$1"
  local attempts="${2:-30}"
  local container_id
  container_id="$("${COMPOSE[@]}" ps --all --quiet "$service")"
  [[ -n "$container_id" ]] || die "service has no container: $service"

  for ((attempt = 1; attempt <= attempts; attempt += 1)); do
    local state exit_code inspection
    inspection="$(docker inspect --format '{{.State.Status}} {{.State.ExitCode}}' "$container_id")"
    read -r state exit_code <<< "$inspection"
    if [[ "$state" == "exited" && "$exit_code" == "0" ]]; then
      log "$service completed successfully"
      return 0
    fi
    if [[ "$state" == "exited" && "$exit_code" != "0" ]]; then
      "${COMPOSE[@]}" logs --no-color --tail=50 "$service" >&2
      die "$service exited with status $exit_code"
    fi
    sleep 1
  done
  die "$service did not complete"
}

setup() {
  if [[ "$DRY_RUN" == true ]]; then
    print_command python3 -m venv "$ROOT_DIR/.venv"
    print_command "$ROOT_DIR/.venv/bin/pip" install --quiet -e "$ROOT_DIR/backend[dev]"
    print_command npm --prefix "$ROOT_DIR/frontend" ci
    print_command install -d -m 755 "$DEV_BIN_DIR"
    print_command ln -sfn "$ROOT_DIR/scripts/nico-dev" "$DEV_BIN_DIR/nico"
    prepare_development_home
    return
  fi

  ensure_env_file
  log "installing the editable backend environment"
  ensure_python_environment
  record_python_environment
  log "installing frontend dependencies"
  ensure_frontend_environment
  record_frontend_environment
  install_dev_cli
  prepare_development_home
  log "local development dependencies are ready"
}

python_environment_fingerprint() {
  local interpreter="${1:-python3}"
  "$interpreter" - "$ROOT_DIR/backend/pyproject.toml" <<'PY'
import hashlib
from pathlib import Path
import sys

digest = hashlib.sha256(Path(sys.argv[1]).read_bytes())
digest.update(f"{sys.version_info.major}.{sys.version_info.minor}".encode())
print(digest.hexdigest())
PY
}

record_python_environment() {
  python_environment_fingerprint "$ROOT_DIR/.venv/bin/python" \
    > "$ROOT_DIR/.venv/.nico-editable-fingerprint"
}

sync_python_environment() {
  require_command python3
  local stamp="$ROOT_DIR/.venv/.nico-editable-fingerprint"
  local expected desired_python existing_python
  expected="$(python_environment_fingerprint)"
  desired_python="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  existing_python=""
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    existing_python="$("$ROOT_DIR/.venv/bin/python" -c \
      'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' \
      2>/dev/null || true)"
  fi
  if [[ -x "$ROOT_DIR/.venv/bin/python" && \
        "$existing_python" == "$desired_python" && \
        -x "$ROOT_DIR/.venv/bin/pip" && \
        -x "$ROOT_DIR/.venv/bin/nico" && \
        -x "$ROOT_DIR/.venv/bin/uvicorn" && \
        -x "$ROOT_DIR/.venv/bin/alembic" && \
        -f "$stamp" && "$(<"$stamp")" == "$expected" ]] && \
     "$ROOT_DIR/.venv/bin/pip" check >/dev/null 2>&1; then
    log "editable backend and CLI are current"
    return
  fi

  ensure_python_environment
  record_python_environment
}

frontend_environment_fingerprint() {
  require_command node
  local node_major
  node_major="$(node -p 'process.versions.node.split(".")[0]')"
  python3 - "$ROOT_DIR/frontend/package.json" \
    "$ROOT_DIR/frontend/package-lock.json" "$node_major" <<'PY'
import hashlib
from pathlib import Path
import sys

digest = hashlib.sha256()
for filename in sys.argv[1:3]:
    digest.update(Path(filename).read_bytes())
digest.update(sys.argv[3].encode())
print(digest.hexdigest())
PY
}

record_frontend_environment() {
  frontend_environment_fingerprint \
    > "$ROOT_DIR/frontend/node_modules/.nico-dependency-fingerprint"
}

sync_frontend_environment() {
  require_command npm
  local stamp="$ROOT_DIR/frontend/node_modules/.nico-dependency-fingerprint"
  local expected
  expected="$(frontend_environment_fingerprint)"
  if [[ -d "$ROOT_DIR/frontend/node_modules" && \
        -x "$ROOT_DIR/frontend/node_modules/.bin/vite" && \
        -f "$stamp" && "$(<"$stamp")" == "$expected" ]]; then
    log "frontend dependencies are current"
    return
  fi

  log "synchronizing frontend dependencies"
  ensure_frontend_environment
  record_frontend_environment
}

install_dev_cli() {
  local command_path="$DEV_BIN_DIR/nico"
  [[ ! -e "$command_path" || -L "$command_path" ]] || \
    die "refusing to replace non-symlink command: $command_path"
  install -d -m 755 "$DEV_BIN_DIR"
  ln -sfn "$ROOT_DIR/scripts/nico-dev" "$command_path"
  log "installed development CLI: $command_path -> $ROOT_DIR/scripts/nico-dev"
  case ":$PATH:" in
    *":$DEV_BIN_DIR:"*) ;;
    *) log "add $DEV_BIN_DIR to PATH to run 'nico' directly" ;;
  esac
}

sync_source_environment() {
  if [[ "$DRY_RUN" == true ]]; then
    print_command python3 -m venv "$ROOT_DIR/.venv"
    print_command "$ROOT_DIR/.venv/bin/pip" install --quiet -e "$ROOT_DIR/backend[dev]"
    print_command install -d -m 755 "$DEV_BIN_DIR"
    print_command ln -sfn "$ROOT_DIR/scripts/nico-dev" "$DEV_BIN_DIR/nico"
    print_command test -d "$ROOT_DIR/frontend/node_modules" || \
      print_command npm --prefix "$ROOT_DIR/frontend" ci
    prepare_development_home
    return
  fi

  log "synchronizing editable backend and CLI"
  sync_python_environment
  install_dev_cli
  prepare_development_home
  sync_frontend_environment
}

infra_up() {
  local compose_args=("${COMPOSE[@]}")
  local services=(postgres redis minio minio-init)
  if web_search_local_enabled; then
    compose_args+=(--profile web-search-local)
    services+=(searxng)
  fi
  if [[ "$DRY_RUN" == true ]]; then
    print_command "${compose_args[@]}" up --detach --no-build "${services[@]}"
    return
  fi

  require_command docker
  require_command curl
  ensure_env_file
  log "starting local infrastructure without application image builds"
  "${compose_args[@]}" up --detach --no-build "${services[@]}"
  wait_for_service_health postgres
  wait_for_service_health redis
  wait_for_service_health minio
  wait_for_completed_service minio-init
  if web_search_local_enabled; then
    wait_for_service_health searxng
  fi
}

infra_down() {
  local compose_args=("${COMPOSE[@]}")
  local services=(postgres redis minio)
  if web_search_local_enabled; then
    compose_args+=(--profile web-search-local)
    services+=(searxng)
  fi
  if [[ "$DRY_RUN" == true ]]; then
    print_command "${compose_args[@]}" stop "${services[@]}"
    print_command "${compose_args[@]}" rm --force minio-init
    return
  fi

  require_command docker
  log "stopping local development infrastructure; volumes are preserved"
  "${compose_args[@]}" stop "${services[@]}"
  "${compose_args[@]}" rm --force minio-init >/dev/null
}

require_local_environment() {
  [[ -x "$ROOT_DIR/.venv/bin/python" && -x "$ROOT_DIR/.venv/bin/uvicorn" && \
     -x "$ROOT_DIR/.venv/bin/nico" ]] || die "backend development environment is incomplete"
  [[ -d "$ROOT_DIR/frontend/node_modules" ]] || die "frontend dependencies are incomplete"
  require_command npm
}

run_migrations() {
  if [[ "$DRY_RUN" == true ]]; then
    print_command bash -c "cd '$ROOT_DIR/backend' && '$ROOT_DIR/.venv/bin/alembic' -c alembic.ini upgrade head"
    return
  fi

  log "applying database migrations"
  (
    cd "$ROOT_DIR/backend"
    "$ROOT_DIR/.venv/bin/alembic" -c alembic.ini upgrade head
  )
}

PIDS=()
WORKER_PID=""
WORKER_INDEX=-1
STARTED_PID=""
SUPERVISOR_ACQUIRED=false

spawn_process() {
  local label="$1"
  shift
  if [[ "$DRY_RUN" == true ]]; then
    print_command "$@"
    STARTED_PID=""
    return
  fi
  log "starting $label"
  "$ROOT_DIR/.venv/bin/python" -c \
    'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "$@" &
  STARTED_PID="$!"
}

start_process() {
  spawn_process "$@"
  [[ -z "$STARTED_PID" ]] || PIDS+=("$STARTED_PID")
}

start_worker() {
  spawn_process "Worker" "$ROOT_DIR/scripts/nico-dev-worker"
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  WORKER_PID="$STARTED_PID"
  if ((WORKER_INDEX < 0)); then
    WORKER_INDEX="${#PIDS[@]}"
    PIDS+=("$WORKER_PID")
  else
    PIDS[$WORKER_INDEX]="$WORKER_PID"
  fi
}

stop_processes() {
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi
  if [[ "$SUPERVISOR_ACQUIRED" == true ]]; then
    [[ -z "${NICO_WORKER_HEALTH_MARKER:-}" ]] || rm -f -- "$NICO_WORKER_HEALTH_MARKER"
    rm -f -- "$DEV_SUPERVISOR_PID" "$DEV_RESTART_REQUEST" "$DEV_RESTART_RESPONSE"
    SUPERVISOR_ACQUIRED=false
  fi
  if ((${#PIDS[@]} == 0)); then
    return
  fi
  trap - EXIT INT TERM
  log "stopping local source processes"
  local groups=()
  local pid
  for pid in "${PIDS[@]}"; do
    groups+=("-$pid")
  done
  kill -- "${groups[@]}" 2>/dev/null || true

  local attempt alive
  for ((attempt = 1; attempt <= 50; attempt += 1)); do
    alive=false
    for pid in "${PIDS[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        alive=true
        break
      fi
    done
    [[ "$alive" == false ]] && break
    sleep 0.1
  done

  kill -KILL -- "${groups[@]}" 2>/dev/null || true
  wait "${PIDS[@]}" 2>/dev/null || true
}

acquire_supervisor() {
  if [[ -f "$DEV_SUPERVISOR_PID" ]]; then
    local existing_pid=""
    read -r existing_pid < "$DEV_SUPERVISOR_PID" || true
    if [[ "$existing_pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$existing_pid" 2>/dev/null; then
      die "another make run supervisor is already active (PID $existing_pid)"
    fi
    rm -f -- "$DEV_SUPERVISOR_PID"
  fi
  if ! (umask 077; set -o noclobber; printf '%s\n' "$$" > "$DEV_SUPERVISOR_PID") \
    2>/dev/null; then
    die "another make run supervisor acquired the development environment"
  fi
  chmod 600 "$DEV_SUPERVISOR_PID"
  SUPERVISOR_ACQUIRED=true
}

wait_for_worker() {
  local attempts="${1:-60}"
  for ((attempt = 1; attempt <= attempts; attempt += 1)); do
    if [[ -s "$NICO_WORKER_HEALTH_MARKER" ]]; then
      log "Worker is ready"
      return
    fi
    if [[ -n "$WORKER_PID" ]] && ! kill -0 "$WORKER_PID" 2>/dev/null; then
      local status=0
      wait "$WORKER_PID" || status=$?
      die "Worker stopped before becoming ready (status $status)"
    fi
    sleep 2
  done
  die "Worker did not become ready: $NICO_WORKER_HEALTH_MARKER"
}

restart_worker() {
  local previous_pid="$WORKER_PID"
  log "restarting source Worker to apply Provider credentials"
  rm -f -- "$NICO_WORKER_HEALTH_MARKER"
  kill -TERM -- "-$previous_pid" 2>/dev/null || true
  wait "$previous_pid" 2>/dev/null || true
  start_worker
  wait_for_worker
}

read_restart_id() {
  "$ROOT_DIR/.venv/bin/python" - "$DEV_RESTART_REQUEST" <<'PY'
import json
import pathlib
import sys
import uuid

path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
restart_id = payload.get("restart_id") if isinstance(payload, dict) else None
print(uuid.UUID(restart_id))
PY
}

write_restart_response() {
  local restart_id="$1"
  "$ROOT_DIR/.venv/bin/python" - "$DEV_RESTART_RESPONSE" "$restart_id" <<'PY'
import json
import os
import pathlib
import sys
import tempfile

path = pathlib.Path(sys.argv[1])
payload = json.dumps({"ok": True, "restart_id": sys.argv[2]}, separators=(",", ":"))
descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
temporary = pathlib.Path(temporary_name)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
finally:
    temporary.unlink(missing_ok=True)
PY
}

process_worker_restart_request() {
  [[ -f "$DEV_RESTART_REQUEST" ]] || return 1
  local restart_id
  restart_id="$(read_restart_id)" || die "development Worker restart request is invalid"
  rm -f -- "$DEV_RESTART_REQUEST"
  restart_worker
  write_restart_response "$restart_id"
  return 0
}

wait_for_source_failure() {
  local pid status
  while true; do
    if process_worker_restart_request; then
      continue
    fi
    for pid in "${PIDS[@]}"; do
      if ! kill -0 "$pid" 2>/dev/null; then
        status=0
        wait "$pid" || status=$?
        if ((status == 0)); then
          die "a local source process stopped unexpectedly"
        fi
        die "a local source process failed with status $status"
      fi
    done
    sleep 0.5
  done
}

bootstrap_development_profile() {
  local api_base="http://localhost:$LOCAL_API_PORT"
  local state_file="$DEV_HOME/state/setup-state.json"
  if [[ "$DRY_RUN" == true ]]; then
    print_command curl --request POST "$api_base/api/v1/tenants/bootstrap"
    print_command curl --request POST "$api_base/api/v1/projects"
    print_command "$ROOT_DIR/.venv/bin/nico" --config-file "$DEV_CONFIG_FILE" \
      config set development --api-url "$api_base" --tenant-id DEVELOPMENT_TENANT_ID \
      --actor-id development-operator --service-command "$DEV_SERVICE_COMMAND" \
      --install-root "$DEV_HOME"
    return
  fi

  local tenant="" project=""
  if [[ -f "$state_file" ]]; then
    read -r tenant project < <(
      "$ROOT_DIR/.venv/bin/python" - "$state_file" <<'PY'
import json
import sys
import uuid

try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
    print(uuid.UUID(payload["tenant_id"]), uuid.UUID(payload["project_id"]))
except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
PY
    ) || true
    if [[ -n "$tenant" && -n "$project" ]] && ! curl --fail --silent --show-error \
      --header "X-Tenant-ID: $tenant" --header "X-Actor-ID: development-operator" \
      "$api_base/api/v1/projects/$project" >/dev/null 2>&1; then
      tenant=""
      project=""
    fi
  fi

  if [[ -z "$tenant" || -z "$project" ]]; then
    local suffix tenant_json project_json
    suffix="$(date -u +%Y%m%d%H%M%S)-$$"
    tenant_json="$(curl --fail-with-body --silent --show-error \
      --request POST --header 'Content-Type: application/json' \
      --header 'X-Actor-ID: development-operator' \
      --data "{\"name\":\"Nico Development\",\"slug\":\"nico-development-$suffix\"}" \
      "$api_base/api/v1/tenants/bootstrap")"
    tenant="$("$ROOT_DIR/.venv/bin/python" -c \
      'import json,sys; print(json.load(sys.stdin)["id"])' <<< "$tenant_json")"
    project_json="$(curl --fail-with-body --silent --show-error \
      --request POST --header 'Content-Type: application/json' \
      --header "X-Tenant-ID: $tenant" --header 'X-Actor-ID: development-operator' \
      --data '{"name":"Nico Development","description":"Source project created by make run","metadata":{"source":"make-run"}}' \
      "$api_base/api/v1/projects")"
    project="$("$ROOT_DIR/.venv/bin/python" -c \
      'import json,sys; print(json.load(sys.stdin)["id"])' <<< "$project_json")"
    "$ROOT_DIR/.venv/bin/python" - "$state_file" "$tenant" "$project" <<'PY'
import json
import os
import pathlib
import sys
import tempfile

path = pathlib.Path(sys.argv[1])
payload = {"tenant_id": sys.argv[2], "project_id": sys.argv[3]}
descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
temporary = pathlib.Path(temporary_name)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
finally:
    temporary.unlink(missing_ok=True)
PY
  fi

  "$ROOT_DIR/.venv/bin/nico" --config-file "$DEV_CONFIG_FILE" config set development \
    --api-url "$api_base" --tenant-id "$tenant" --actor-id development-operator \
    --service-command "$DEV_SERVICE_COMMAND" --install-root "$DEV_HOME" >/dev/null
  "$ROOT_DIR/.venv/bin/nico" --config-file "$DEV_CONFIG_FILE" config use development \
    >/dev/null
  log "development CLI profile is ready (tenant $tenant, project $project)"
}

stop_from_signal() {
  local status="$1"
  stop_processes
  exit "$status"
}

run_stack() {
  sync_source_environment
  if [[ "$DRY_RUN" == false ]]; then
    require_local_environment
    export NICO_ENV_PYTHON="$ROOT_DIR/.venv/bin/python"
  fi
  load_local_environment
  if [[ "$DRY_RUN" == false ]]; then
    mkdir -p "$NICO_WORKSPACE_ROOT" "$(dirname "$NICO_WORKER_HEALTH_MARKER")"
    chmod 700 "$NICO_WORKSPACE_ROOT"
  fi

  trap stop_processes EXIT
  trap 'stop_from_signal 130' INT
  trap 'stop_from_signal 143' TERM
  if [[ "$DRY_RUN" == false ]]; then
    acquire_supervisor
    rm -f -- "$NICO_WORKER_HEALTH_MARKER"
    rm -f -- "$DEV_RESTART_REQUEST" "$DEV_RESTART_RESPONSE"
  fi

  infra_up
  if [[ "$DRY_RUN" == false ]]; then
    stop_containerized_app_services
  fi
  run_migrations

  start_process "Sandbox Runner on :$LOCAL_SANDBOX_PORT" \
    "$ROOT_DIR/.venv/bin/uvicorn" \
    nico_agent.sandbox.api:create_sandbox_runner_app \
    --factory --app-dir "$ROOT_DIR/backend/src" --host 127.0.0.1 \
    --port "$LOCAL_SANDBOX_PORT" --reload \
    --reload-dir "$ROOT_DIR/backend/src"
  start_process "API on :$LOCAL_API_PORT" \
    "$ROOT_DIR/.venv/bin/uvicorn" nico_agent.main:app \
    --app-dir "$ROOT_DIR/backend/src" --host 127.0.0.1 \
    --port "$LOCAL_API_PORT" --reload \
    --reload-dir "$ROOT_DIR/backend/src" \
    --log-config "$ROOT_DIR/backend/logging.json"
  start_worker
  start_process "Web on :$LOCAL_WEB_PORT" npm --prefix "$ROOT_DIR/frontend" run dev

  if [[ "$DRY_RUN" == true ]]; then
    bootstrap_development_profile
    return
  fi

  wait_for_url "http://127.0.0.1:$LOCAL_SANDBOX_PORT/healthz" "Sandbox Runner"
  wait_for_url "http://127.0.0.1:$LOCAL_API_PORT/api/v1/health/ready" "API"
  bootstrap_development_profile
  wait_for_url "http://127.0.0.1:$LOCAL_WEB_PORT/" "Web"
  wait_for_worker
  log "source stack is ready; run 'nico chat' in another terminal"
  log "Ctrl-C stops source processes; 'make infra-down' also stops the data services"

  wait_for_source_failure
}

case "$ACTION" in
  setup) setup ;;
  infra-up)
    infra_up
    ;;
  run) run_stack ;;
  infra-down) infra_down ;;
esac
