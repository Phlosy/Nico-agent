#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command docker
require_command curl
load_env_file
ensure_python_environment

DB_NAME="nico_agent_integration_${GITHUB_RUN_ID:-$$}_${GITHUB_RUN_ATTEMPT:-1}"

cleanup() {
  "${COMPOSE[@]}" exec -T postgres dropdb --if-exists --force \
    -U "${POSTGRES_USER:-nico}" "$DB_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

log "starting real PostgreSQL, Redis and MinIO dependencies"
"${COMPOSE[@]}" up --detach postgres redis minio minio-init
wait_for_url "http://localhost:${MINIO_API_PORT:-19010}/minio/health/ready" "MinIO"

for attempt in {1..30}; do
  if "${COMPOSE[@]}" exec -T postgres pg_isready \
    -U "${POSTGRES_USER:-nico}" -d "${POSTGRES_DB:-nico_agent}" >/dev/null 2>&1; then
    break
  fi
  [[ "$attempt" -eq 30 ]] && die "PostgreSQL did not become ready"
  sleep 2
done

for attempt in {1..30}; do
  if "${COMPOSE[@]}" exec -T redis redis-cli ping 2>/dev/null | grep -q PONG; then
    break
  fi
  [[ "$attempt" -eq 30 ]] && die "Redis did not become ready"
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
export NICO_REDIS_URL="redis://localhost:${REDIS_PORT:-16379}/0"
export NICO_MINIO_URL="http://localhost:${MINIO_API_PORT:-19010}"
export RUN_INTEGRATION=1

log "applying Alembic migrations"
"$ROOT_DIR/.venv/bin/alembic" -c "$ROOT_DIR/backend/alembic.ini" upgrade head
log "proving the extension migration can roll back and reapply"
"$ROOT_DIR/.venv/bin/alembic" -c "$ROOT_DIR/backend/alembic.ini" downgrade 20260721_0024
"$ROOT_DIR/.venv/bin/alembic" -c "$ROOT_DIR/backend/alembic.ini" upgrade head
log "running real dependency integration tests"
"$ROOT_DIR/.venv/bin/pytest" "$ROOT_DIR/backend/tests/integration"
log "running provider-neutral conversation continuity evaluation"
"$ROOT_DIR/scripts/e2e-conversation-continuity.sh"
log "integration suite passed"
