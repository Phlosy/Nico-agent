#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command docker
load_env_file
ensure_python_environment

DB_NAME="nico_agent_guided_e2e_${GITHUB_RUN_ID:-$$}_${GITHUB_RUN_ATTEMPT:-1}"
TMP_DIR="$(mktemp -d)"
CANARY="guided-setup-e2e-canary"

cleanup() {
  "${COMPOSE[@]}" exec -T postgres dropdb --if-exists --force \
    -U "${POSTGRES_USER:-nico}" "$DB_NAME" >/dev/null 2>&1 || true
  "${COMPOSE[@]}" --profile guided-setup-e2e stop fake-web >/dev/null 2>&1 || true
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

export NICO_MODEL_SECRET_GOAL_G="$CANARY"

log "starting isolated guided-setup fixtures and PostgreSQL"
"${COMPOSE[@]}" --profile guided-setup-e2e up --detach --build \
  postgres fake-model fake-web
wait_for_service_health postgres
wait_for_service_health fake-model
wait_for_service_health fake-web

cleanup_database() {
  "${COMPOSE[@]}" exec -T postgres dropdb --if-exists --force \
    -U "${POSTGRES_USER:-nico}" "$DB_NAME" >/dev/null 2>&1 || true
}
cleanup_database
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

log "applying migrations to the isolated guided-setup database"
"$ROOT_DIR/.venv/bin/alembic" -c "$ROOT_DIR/backend/alembic.ini" upgrade head

log "running readiness, provider-only, capability, proof, and recovery acceptance"
"$ROOT_DIR/.venv/bin/pytest" -q \
  "$ROOT_DIR/backend/tests/integration/test_guided_setup_api.py" \
  "$ROOT_DIR/backend/tests/integration/test_agent_capability_activation.py" \
  "$ROOT_DIR/backend/tests/integration/test_guided_setup_proof.py" \
  "$ROOT_DIR/backend/tests/integration/test_web_agent_e2e.py" \
  "$ROOT_DIR/backend/tests/integration/test_web_onboarding.py::test_provider_only_web_activation_does_not_publish_agent_version" \
  | tee "$TMP_DIR/pytest.log"

"${COMPOSE[@]}" logs --no-color fake-model fake-web > "$TMP_DIR/fixtures.log"
"${COMPOSE[@]}" exec -T postgres pg_dump \
  -U "${POSTGRES_USER:-nico}" -d "$DB_NAME" --data-only > "$TMP_DIR/database.sql"
if grep -R -F "$CANARY" "$TMP_DIR" >/dev/null; then
  die "guided-setup canary leaked into output, logs, or persisted database facts"
fi

log "PASS guided setup full, partial, idempotent, isolated, and recoverable paths"
