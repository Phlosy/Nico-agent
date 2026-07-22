#!/usr/bin/env bash

set -euo pipefail

resolve_script() {
  local source="${BASH_SOURCE[0]}"
  while [[ -h "$source" ]]; do
    local directory
    directory="$(cd -P "$(dirname "$source")" && pwd)"
    source="$(readlink "$source")"
    [[ "$source" == /* ]] || source="$directory/$source"
  done
  cd -P "$(dirname "$source")" && pwd
}

SCRIPT_DIR="$(resolve_script)"
NICO_HOME="${NICO_HOME:-$(cd "$SCRIPT_DIR/.." && pwd)}"
CURRENT="$NICO_HOME/current"
ENV_FILE="$NICO_HOME/config/deployment.env"

log() {
  printf '[nico-service] %s\n' "$*"
}

die() {
  printf '[nico-service] ERROR: %s\n' "$*" >&2
  exit 1
}

env_value() {
  local key="$1"
  local fallback="${2:-}"
  local line
  line="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -n 1 || true)"
  if [[ -n "$line" ]]; then
    printf '%s\n' "${line#*=}"
  else
    printf '%s\n' "$fallback"
  fi
}

compose_version_supported() {
  local version="${1#v}"
  version="${version%%-*}"
  local major minor patch
  IFS=. read -r major minor patch <<< "$version"
  [[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ && "$patch" =~ ^[0-9]+$ ]] || return 1
  ((major > 2)) || ((major == 2 && (minor > 24 || (minor == 24 && patch >= 4))))
}

require_installation() {
  command -v docker >/dev/null 2>&1 || die "Docker is not installed"
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required"
  local compose_version
  compose_version="$(docker compose version --short 2>/dev/null)" || die \
    "could not determine the Docker Compose version"
  compose_version_supported "$compose_version" || die \
    "Docker Compose 2.24.4 or newer is required (found $compose_version)"
  [[ -d "$CURRENT" ]] || die "no current release found under $NICO_HOME"
  [[ -f "$ENV_FILE" ]] || die "deployment environment not found: $ENV_FILE"
}

compose() {
  docker compose \
    --env-file "$ENV_FILE" \
    --project-directory "$CURRENT" \
    --file "$CURRENT/docker-compose.yml" \
    --file "$CURRENT/deploy/docker-compose.release.yml" \
    "$@"
}

runtime() {
  local value
  value="$(env_value NICO_RUNTIME)"
  case "$value" in
    native | hermes) printf '%s\n' "$value" ;;
    *) die "NICO_RUNTIME must be native or hermes in $ENV_FILE" ;;
  esac
}

wait_for_url() {
  local url="$1"
  local label="$2"
  command -v curl >/dev/null 2>&1 || die "curl is required for readiness checks"
  for _attempt in {1..60}; do
    if curl --fail --silent --show-error --max-time 3 "$url" >/dev/null 2>&1; then
      log "$label is ready"
      return 0
    fi
    sleep 2
  done
  compose logs --no-color --tail=80 api web >&2 || true
  die "$label did not become ready: $url"
}

wait_for_postgres() {
  for _attempt in {1..60}; do
    if compose exec --no-TTY postgres sh -ec \
      'pg_isready --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"' \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  compose logs --no-color --tail=80 postgres >&2 || true
  die "PostgreSQL did not become ready"
}

postgres_accepts_configured_password() {
  compose exec --no-TTY postgres sh -ec '
    host="$(hostname -i)"
    host="${host%% *}"
    PGPASSWORD="$POSTGRES_PASSWORD" psql \
      --host "$host" \
      --username "$POSTGRES_USER" \
      --dbname "$POSTGRES_DB" \
      --set ON_ERROR_STOP=1 \
      --tuples-only --no-align \
      --command "select 1" >/dev/null
  ' >/dev/null 2>&1
}

synchronize_postgres_password() {
  local command
  command="$(cat <<'SH'
psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set ON_ERROR_STOP=1 \
  --set role_password="$POSTGRES_PASSWORD" <<'SQL'
SELECT format('ALTER ROLE %I PASSWORD %L', current_user, :'role_password') \gexec
SQL
SH
)"
  compose exec --no-TTY postgres sh -ec "$command" >/dev/null
}

prepare_postgres() {
  compose up --detach --no-build postgres
  wait_for_postgres
  if postgres_accepts_configured_password; then
    return 0
  fi
  log "repairing PostgreSQL credentials for the retained data volume"
  synchronize_postgres_password || die \
    "could not synchronize PostgreSQL credentials with the retained data volume"
  postgres_accepts_configured_password || die \
    "PostgreSQL rejected the synchronized deployment credentials"
  # A previously crashing API may be in Docker's restart backoff. Recreate it
  # after repairing the role so the next Compose up observes the fix immediately.
  compose rm --force --stop api >/dev/null 2>&1 || true
}

service_host() {
  local host
  host="$(env_value NICO_BIND_ADDRESS 127.0.0.1)"
  case "$host" in
    0.0.0.0) host=127.0.0.1 ;;
    ::) host='[::1]' ;;
    *:*) host="[$host]" ;;
  esac
  printf '%s\n' "$host"
}

up() {
  local selected
  selected="$(runtime)"
  if [[ "$selected" == "native" ]]; then
    compose --profile hermes stop worker-hermes >/dev/null
  else
    compose --profile native stop worker >/dev/null
  fi
  local pull_policy
  pull_policy="$(env_value NICO_PULL_POLICY always)"
  case "$pull_policy" in
    always) compose --profile "$selected" pull ;;
    never) log "using preloaded local images" ;;
    *) die "NICO_PULL_POLICY must be always or never in $ENV_FILE" ;;
  esac
  prepare_postgres
  compose --profile "$selected" up --detach --no-build --remove-orphans
  local host
  host="$(service_host)"
  wait_for_url "http://$host:$(env_value API_PORT 18000)/api/v1/health/ready" "API"
  wait_for_url "http://$host:$(env_value WEB_PORT 18080)/healthz" "Web"
  compose --profile "$selected" ps
}

doctor() {
  local selected
  selected="$(runtime)"
  compose --profile "$selected" config --quiet
  wait_for_url "http://localhost:$(env_value API_PORT 18000)/api/v1/health/ready" "API"
  wait_for_url "http://localhost:$(env_value WEB_PORT 18080)/healthz" "Web"
  compose --profile "$selected" ps
}

provider_secret() {
  local action="${1:-}"
  case "$action" in
    begin | renew | commit | rollback | recover) ;;
    *) die "provider-secret requires begin, renew, commit, rollback, or recover" ;;
  esac
  [[ "$(runtime)" == "native" ]] || die \
    "Provider secret transactions require the native Runtime"
  [[ -x "$CURRENT/venv/bin/python" ]] || die "installed Nico Python runtime is unavailable"
  NICO_HOME="$NICO_HOME" "$CURRENT/venv/bin/python" \
    -m nico_agent.local_secret_transaction "$action"
}

credential_secret() {
  provider_secret "$@"
}

usage() {
  cat <<'EOF'
Usage: nico-service <command>

Commands:
  up              Prepare images and start the configured Runtime profile
  down            Stop containers and preserve data volumes
  restart         Restart the configured Runtime profile
  status          Show service status
  logs [SERVICE]  Follow logs, optionally for one service
  doctor          Validate Compose and check API/Web readiness
  provider-secret begin|renew|commit|rollback|recover
                  Manage one recoverable Native Provider secret transaction
  credential-secret begin|renew|commit|rollback|recover
                  Manage one recoverable Native model/tool credential transaction
  purge --yes     Stop containers and permanently remove data volumes
  version         Show the installed release version
EOF
}

main() {
  local command="${1:-}"
  shift || true
  case "$command" in
    -h | --help | help | "")
      usage
      return 0
      ;;
    version)
      [[ -f "$CURRENT/version.txt" ]] || die "no current release found under $NICO_HOME"
      cat "$CURRENT/version.txt"
      return 0
      ;;
  esac
  require_installation
  case "$command" in
    up) up ;;
    down) compose --profile native --profile hermes down --remove-orphans ;;
    restart)
      compose --profile native --profile hermes down --remove-orphans
      up
      ;;
    status) compose --profile "$(runtime)" ps ;;
    logs) compose --profile "$(runtime)" logs --follow --tail=100 "$@" ;;
    doctor) doctor ;;
    provider-secret) provider_secret "${1:-}" ;;
    credential-secret) credential_secret "${1:-}" ;;
    purge)
      [[ "${1:-}" == "--yes" ]] || die "purge permanently deletes data; pass --yes"
      compose --profile native --profile hermes down --remove-orphans --volumes
      ;;
    *) die "unknown command: $command" ;;
  esac
}

main "$@"
