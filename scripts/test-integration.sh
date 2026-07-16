#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command docker
require_command curl
load_env_file
ensure_python_environment

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

export NICO_ENVIRONMENT=test
export NICO_DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER:-nico}:${POSTGRES_PASSWORD:-nico-change-me}@localhost:${POSTGRES_PORT:-15432}/${POSTGRES_DB:-nico_agent}"
export NICO_REDIS_URL="redis://localhost:${REDIS_PORT:-16379}/0"
export NICO_MINIO_URL="http://localhost:${MINIO_API_PORT:-19010}"
export RUN_INTEGRATION=1

log "applying Alembic migrations"
"$ROOT_DIR/.venv/bin/alembic" -c "$ROOT_DIR/backend/alembic.ini" upgrade head
log "running real dependency integration tests"
"$ROOT_DIR/.venv/bin/pytest" "$ROOT_DIR/backend/tests/integration"
log "integration suite passed"
