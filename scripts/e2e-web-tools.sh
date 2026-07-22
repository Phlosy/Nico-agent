#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command docker
load_env_file
ensure_python_environment

DB_NAME="nico_agent_web_e2e_${GITHUB_RUN_ID:-$$}_${GITHUB_RUN_ATTEMPT:-1}"

cleanup() {
  "${COMPOSE[@]}" exec -T postgres dropdb --if-exists --force \
    -U "${POSTGRES_USER:-nico}" "$DB_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

log "starting PostgreSQL for the offline Web tools E2E"
"${COMPOSE[@]}" up --detach postgres
for attempt in {1..30}; do
  if "${COMPOSE[@]}" exec -T postgres pg_isready \
    -U "${POSTGRES_USER:-nico}" -d "${POSTGRES_DB:-nico_agent}" >/dev/null 2>&1; then
    break
  fi
  [[ "$attempt" -eq 30 ]] && die "PostgreSQL did not become ready"
  sleep 2
done

cleanup
"${COMPOSE[@]}" exec -T postgres createdb -U "${POSTGRES_USER:-nico}" "$DB_NAME"

export NICO_ENVIRONMENT=test
unset NICO_DATABASE_URL
export NICO_DATABASE_HOST=localhost
export NICO_DATABASE_PORT="${POSTGRES_PORT:-15432}"
export NICO_DATABASE_NAME="$DB_NAME"
export NICO_DATABASE_USER="${POSTGRES_USER:-nico}"
export NICO_DATABASE_PASSWORD="${POSTGRES_PASSWORD:-nico-change-me}"
export RUN_INTEGRATION=1
export PYTHONPATH="$ROOT_DIR/backend"

log "applying migrations to the isolated Web tools database"
"$ROOT_DIR/.venv/bin/alembic" -c "$ROOT_DIR/backend/alembic.ini" upgrade head
log "running configure, approval, Search, Fetch, and citation acceptance"
"$ROOT_DIR/.venv/bin/pytest" -q \
  "$ROOT_DIR/backend/tests/integration/test_web_agent_e2e.py"
log "offline Web tools E2E passed"
