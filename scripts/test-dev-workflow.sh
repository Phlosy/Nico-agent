#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  printf '[nico-dev-test] FAIL: %s\n' "$*" >&2
  exit 1
}

assert_contains() {
  local value="$1"
  local expected="$2"
  local label="$3"
  [[ "$value" == *"$expected"* ]] || fail "$label omitted: $expected"
}

assert_not_contains() {
  local value="$1"
  local unexpected="$2"
  local label="$3"
  [[ "$value" != *"$unexpected"* ]] || fail "$label included: $unexpected"
}

HELP="$(make --no-print-directory -C "$ROOT_DIR" help)"
for target in 'make dev-setup' 'make infra-up' 'make run' 'make cli' 'make demo' 'make infra-down' \
  'make dev-images' 'make version' 'make version-set' 'make tag'; do
  assert_contains "$HELP" "$target" "Make help"
done

RUN_OUTPUT="$("$ROOT_DIR/scripts/local-dev.sh" --dry-run run)"
assert_contains "$RUN_OUTPUT" 'pip install' 'automatic editable CLI synchronization'
assert_contains "$RUN_OUTPUT" 'backend\[dev\]' 'automatic editable CLI synchronization'
assert_contains "$RUN_OUTPUT" 'nico-dev' 'local nico command installation'
assert_contains "$RUN_OUTPUT" '--profile web-search-local' \
  'default SearXNG Compose profile'
assert_contains "$RUN_OUTPUT" \
  'up --detach --no-build postgres redis minio minio-init searxng' \
  'local dependency startup'
assert_contains "$RUN_OUTPUT" '/alembic' 'local migrations'
assert_contains "$RUN_OUTPUT" 'upgrade\ head' 'local migrations'
assert_contains "$RUN_OUTPUT" 'nico_agent.main:app' 'local API startup'
assert_contains "$RUN_OUTPUT" 'nico-dev-worker' 'local Worker startup'
assert_contains "$RUN_OUTPUT" 'config set development' 'development profile bootstrap'
assert_contains "$RUN_OUTPUT" '.nico/dev/bin/nico-service' 'development service isolation'
assert_contains "$RUN_OUTPUT" 'nico_agent.sandbox.api:create_sandbox_runner_app' \
  'local Sandbox Runner startup'
assert_contains "$RUN_OUTPUT" 'npm --prefix' 'local Web startup'
assert_not_contains "$RUN_OUTPUT" 'docker build' 'local development path'
assert_not_contains "$RUN_OUTPUT" 'compose build' 'local development path'
assert_not_contains "$RUN_OUTPUT" ' up --build' 'local development path'
assert_not_contains "$RUN_OUTPUT" ' up --detach --build' 'local development path'

SEARXNG_RUN_OUTPUT="$(NICO_DEV_WEB_SEARCH=searxng \
  "$ROOT_DIR/scripts/local-dev.sh" --dry-run run)"
assert_contains "$SEARXNG_RUN_OUTPUT" '--profile web-search-local' \
  'SearXNG Compose profile'
assert_contains "$SEARXNG_RUN_OUTPUT" \
  'up --detach --no-build postgres redis minio minio-init searxng' \
  'SearXNG infrastructure startup'
assert_not_contains "$SEARXNG_RUN_OUTPUT" 'docker build' 'SearXNG local development path'
assert_contains "$(cat "$ROOT_DIR/docker-compose.yml")" \
  '127.0.0.1:${SEARXNG_PORT:-18888}:8080' 'SearXNG loopback binding'
assert_contains "$(cat "$ROOT_DIR/deploy/searxng/settings.yml")" \
  '    - json' 'SearXNG JSON search format'

DEV_IMAGE_OUTPUT="$(make --no-print-directory -C "$ROOT_DIR" -n dev-images \
  DTN_SUB='GPU/amd64 build 2')"
assert_contains "$DEV_IMAGE_OUTPUT" 'DTN_SUB="GPU/amd64 build 2"' \
  'development image subversion forwarding'
assert_contains "$(DTN_SUB='GPU/amd64 build 2' "$ROOT_DIR/scripts/version.sh" development)" \
  '-gpu-amd64-build-2' 'sanitized development image subversion'

TEMPORARY="$(mktemp -d "${TMPDIR:-/tmp}/nico-dev-test.XXXXXX")"
trap 'rm -rf "$TEMPORARY"' EXIT

ENV_OUTPUT="$(bash -c '
  source "$1/scripts/lib.sh"
  export NICO_ENV_PYTHON="$1/.venv/bin/python"
  load_env_values "$1/.env.example"
  printf "%s\n%s\n" \
    "$NICO_MODEL_TRUSTED_PRIVATE_HOSTS" \
    "$NICO_TOOL_APPROVAL_REQUIRED_RISKS"
' _ "$ROOT_DIR")"
assert_contains "$ENV_OUTPUT" '["host.docker.internal"]' 'dotenv JSON preservation'
assert_contains "$ENV_OUTPUT" '["medium","high"]' 'dotenv JSON preservation'

LITERAL_MARKER="$TEMPORARY/evaluated"
{
  printf 'QUOTED="new password"\r\n'
  printf 'INLINE=hello # ignored\r\n'
  printf 'LITERAL=$(touch %s)\r\n' "$LITERAL_MARKER"
  printf 'LAST=value'
} > "$TEMPORARY/literal.env"
LITERAL_OUTPUT="$(bash -c '
  source "$1/scripts/lib.sh"
  export NICO_ENV_PYTHON="$1/.venv/bin/python"
  load_env_values "$2"
  printf "%s|%s|%s|%s\n" "$QUOTED" "$INLINE" "$LITERAL" "$LAST"
' _ "$ROOT_DIR" "$TEMPORARY/literal.env")"
assert_contains "$LITERAL_OUTPUT" 'new password|hello|$(touch ' 'dotenv literal loading'
assert_contains "$LITERAL_OUTPUT" '|value' 'dotenv final line loading'
[[ ! -e "$LITERAL_MARKER" ]] || fail 'dotenv loader executed a value as shell code'
printf 'BAD KEY=value\n' > "$TEMPORARY/invalid.env"
if bash -c '
  source "$1/scripts/lib.sh"
  export NICO_ENV_PYTHON="$1/.venv/bin/python"
  load_env_values "$2"
' _ "$ROOT_DIR" "$TEMPORARY/invalid.env" >/dev/null 2>&1; then
  fail 'dotenv loader accepted an invalid key'
fi

FAKE_BIN="$TEMPORARY/fake-bin"
FAKE_DOCKER_LOG="$TEMPORARY/docker.log"
mkdir -p "$FAKE_BIN"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'printf "%s\n" "$*" >> "$FAKE_DOCKER_LOG"' \
  'if [[ "${1:-}" == "compose" ]]; then' \
  '  if [[ " $* " == *" ps --services "* ]]; then' \
  '    [[ -z "${FAKE_COMPOSE_RUNNING:-}" ]] || printf "%s\n" "$FAKE_COMPOSE_RUNNING"' \
  '  elif [[ " $* " == *" ps --quiet "* || " $* " == *" ps --all --quiet "* ]]; then' \
  '    printf "%s-id\n" "${*: -1}"' \
  '  fi' \
  'elif [[ "${1:-}" == "inspect" ]]; then' \
  '  if [[ "${*: -1}" == "minio-init-id" && "$*" == *"State.ExitCode"* ]]; then' \
  '    printf "%s\n" "${FAKE_MINIO_STATE:-exited 0}"' \
  '  else' \
  '    printf "healthy\n"' \
  '  fi' \
  'fi' > "$FAKE_BIN/docker"
chmod 755 "$FAKE_BIN/docker"

FAKE_DOCKER_LOG="$FAKE_DOCKER_LOG" PATH="$FAKE_BIN:$PATH" \
  "$ROOT_DIR/scripts/local-dev.sh" infra-up >/dev/null
assert_contains "$(cat "$FAKE_DOCKER_LOG")" \
  'up --detach --no-build postgres redis minio minio-init' \
  'live local dependency startup'

: > "$FAKE_DOCKER_LOG"
NICO_DEV_WEB_SEARCH=searxng FAKE_DOCKER_LOG="$FAKE_DOCKER_LOG" \
  PATH="$FAKE_BIN:$PATH" "$ROOT_DIR/scripts/local-dev.sh" infra-up >/dev/null
assert_contains "$(cat "$FAKE_DOCKER_LOG")" '--profile web-search-local' \
  'live SearXNG profile startup'
assert_contains "$(cat "$FAKE_DOCKER_LOG")" \
  'up --detach --no-build postgres redis minio minio-init searxng' \
  'live SearXNG infrastructure startup'

FAKE_COMPOSE_RUNNING=$'postgres\nredis\nminio' \
FAKE_DOCKER_LOG="$FAKE_DOCKER_LOG" PATH="$FAKE_BIN:$PATH" \
  "$ROOT_DIR/scripts/local-dev.sh" infra-up >/dev/null

: > "$FAKE_DOCKER_LOG"
FAKE_DOCKER_LOG="$FAKE_DOCKER_LOG" PATH="$FAKE_BIN:$PATH" \
  "$ROOT_DIR/scripts/local-dev.sh" infra-down >/dev/null
INFRA_DOWN_COMMANDS="$(cat "$FAKE_DOCKER_LOG")"
assert_contains "$INFRA_DOWN_COMMANDS" 'stop postgres redis minio' \
  'live local dependency shutdown'
assert_contains "$INFRA_DOWN_COMMANDS" 'rm --force minio-init' \
  'live MinIO initializer cleanup'
assert_not_contains "$INFRA_DOWN_COMMANDS" ' down ' 'volume-preserving dependency shutdown'
assert_not_contains "$INFRA_DOWN_COMMANDS" '--volumes' 'volume-preserving dependency shutdown'

: > "$FAKE_DOCKER_LOG"
EXPECTED_DEV_TAG="$($ROOT_DIR/scripts/version.sh development)"
FAKE_DOCKER_LOG="$FAKE_DOCKER_LOG" \
  make --no-print-directory -C "$ROOT_DIR" dev-images DOCKER="$FAKE_BIN/docker" >/dev/null
[[ "$(grep -c '^build --file ' "$FAKE_DOCKER_LOG")" -eq 3 ]] || \
  fail 'make dev-images did not issue exactly three image builds'
for image in backend hermes web; do
  assert_contains "$(cat "$FAKE_DOCKER_LOG")" \
    "--tag nico-agent-$image:$EXPECTED_DEV_TAG" "development $image image tag"
done

FAKE_COMPOSE_RUNNING=api FAKE_DOCKER_LOG="$FAKE_DOCKER_LOG" \
  PATH="$FAKE_BIN:$PATH" "$ROOT_DIR/scripts/local-dev.sh" infra-up >/dev/null
if FAKE_MINIO_STATE='exited 1' FAKE_DOCKER_LOG="$FAKE_DOCKER_LOG" \
  PATH="$FAKE_BIN:$PATH" "$ROOT_DIR/scripts/local-dev.sh" infra-up \
  >/dev/null 2>&1; then
  fail 'local dependency startup accepted a failed MinIO initializer'
fi

LIVE_ROOT="$TEMPORARY/live-project"
LIVE_BIN="$TEMPORARY/live-bin"
FAKE_PROCESS_LOG="$TEMPORARY/processes.log"
FAKE_MIGRATION_LOG="$TEMPORARY/migrations.log"
mkdir -p \
  "$LIVE_ROOT/scripts" \
  "$LIVE_ROOT/backend/src/nico_agent" \
  "$LIVE_ROOT/frontend/node_modules/.bin" \
  "$LIVE_ROOT/.venv/bin" \
  "$LIVE_BIN" \
  "$TEMPORARY/dev-bin"
cp "$ROOT_DIR/scripts/local-dev.sh" "$ROOT_DIR/scripts/lib.sh" \
  "$ROOT_DIR/scripts/nico-dev" "$ROOT_DIR/scripts/nico-dev-service" \
  "$ROOT_DIR/scripts/nico-dev-worker" "$ROOT_DIR/scripts/version.sh" "$LIVE_ROOT/scripts/"
cp "$ROOT_DIR/.env.example" "$LIVE_ROOT/.env"
sed -i '/^NICO_MODEL_ENDPOINT_WRITES_ENABLED=/d' "$LIVE_ROOT/.env"
cp "$ROOT_DIR/backend/pyproject.toml" "$LIVE_ROOT/backend/pyproject.toml"
cp "$ROOT_DIR/frontend/package.json" "$ROOT_DIR/frontend/package-lock.json" \
  "$LIVE_ROOT/frontend/"
touch "$LIVE_ROOT/backend/src/nico_agent/__init__.py"
touch "$LIVE_ROOT/docker-compose.yml"
touch "$LIVE_ROOT/frontend/node_modules/.bin/vite"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'if [[ "${1:-}" == "-m" && "${2:-}" == "nico_agent.local_defaults" ]]; then' \
  '  printf '\''{"skill_policy":{"allowed_skill_ids":[],"enabled":false,"scopes":[]},"tool_policy":{"allow":[],"permissions":[],"secret_refs":{},"tools":{}}}\n'\''' \
  '  exit 0' \
  'fi' \
  'if [[ "${1:-}" == "-m" && "${2:-}" == "nico_agent.worker" ]]; then' \
  '  printf "ready\n" > "$NICO_WORKER_HEALTH_MARKER"' \
  '  printf "%s\n" "$$" >> "$FAKE_PROCESS_LOG"' \
  '  exec sleep 30' \
  'fi' \
  'exec "$REAL_PYTHON" "$@"' > "$LIVE_ROOT/.venv/bin/python"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "%s\n" "$*" >> "$FAKE_PIP_LOG"' > "$LIVE_ROOT/.venv/bin/pip"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "%s|%s|%s|%s|%s\n" "$NICO_API_URL" "$NICO_BUILD_VERSION" "$NICO_CONFIG_FILE" "$NICO_PROFILE" "$*"' \
  > "$LIVE_ROOT/.venv/bin/nico"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "%s|%s\n" "${NICO_MODEL_ENDPOINT_WRITES_ENABLED:-unset}" "${NICO_WEB_PROVIDER_WRITES_ENABLED:-unset}" >> "$FAKE_POLICY_LOG"' \
  'printf "%s\n" "$$" >> "$FAKE_PROCESS_LOG"' \
  'exec sleep 30' > "$LIVE_ROOT/.venv/bin/uvicorn"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "%s\n" "$*" >> "$FAKE_MIGRATION_LOG"' > "$LIVE_ROOT/.venv/bin/alembic"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [[ " $* " == *" ci "* ]]; then printf "%s\n" "$*" >> "$FAKE_NPM_LOG"; exit 0; fi' \
  'if [[ -n "${FAKE_NPM_EXIT:-}" ]]; then exit "$FAKE_NPM_EXIT"; fi' \
  'printf "%s\n" "$$" >> "$FAKE_PROCESS_LOG"' \
  'exec sleep 30' > "$LIVE_BIN/npm"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -euo pipefail' \
  'if [[ "$*" == *"/api/v1/tenants/bootstrap"* ]]; then' \
  '  printf '\''{"id":"11111111-1111-4111-8111-111111111111"}\n'\''' \
  'elif [[ "$*" == *"/api/v1/projects"* && "$*" == *"--request POST"* ]]; then' \
  '  printf '\''{"id":"22222222-2222-4222-8222-222222222222"}\n'\''' \
  'fi' > "$LIVE_BIN/curl"
chmod 755 \
  "$LIVE_ROOT/.venv/bin/python" \
  "$LIVE_ROOT/.venv/bin/pip" \
  "$LIVE_ROOT/.venv/bin/nico" \
  "$LIVE_ROOT/.venv/bin/uvicorn" \
  "$LIVE_ROOT/.venv/bin/alembic" \
  "$LIVE_ROOT/frontend/node_modules/.bin/vite" \
  "$LIVE_BIN/npm" \
  "$LIVE_BIN/curl"

: > "$FAKE_PROCESS_LOG"
: > "$TEMPORARY/policy.log"
: > "$TEMPORARY/pip.log"
: > "$TEMPORARY/npm.log"
if REAL_PYTHON="$ROOT_DIR/.venv/bin/python" \
  FAKE_PIP_LOG="$TEMPORARY/pip.log" \
  FAKE_NPM_LOG="$TEMPORARY/npm.log" \
  FAKE_PROCESS_LOG="$FAKE_PROCESS_LOG" \
  FAKE_POLICY_LOG="$TEMPORARY/policy.log" \
  FAKE_MIGRATION_LOG="$FAKE_MIGRATION_LOG" \
  FAKE_DOCKER_LOG="$FAKE_DOCKER_LOG" \
  FAKE_COMPOSE_RUNNING=$'api\nweb' \
  FAKE_NPM_EXIT=7 NICO_BIN_DIR="$TEMPORARY/dev-bin" PATH="$LIVE_BIN:$FAKE_BIN:$PATH" \
  "$LIVE_ROOT/scripts/local-dev.sh" run >/dev/null 2>&1; then
  fail 'local source supervisor accepted a child-process failure'
fi
assert_contains "$(cat "$FAKE_MIGRATION_LOG")" \
  '-c alembic.ini upgrade head' 'live local migration execution'
assert_contains "$(cat "$FAKE_DOCKER_LOG")" 'stop api web' \
  'automatic transition from containerized apps to source services'
assert_contains "$(cat "$TEMPORARY/pip.log")" \
  '-e' 'live editable CLI synchronization'
assert_not_contains "$(cat "$TEMPORARY/policy.log")" 'unset' \
  'source Provider activation policy default'
assert_not_contains "$(cat "$TEMPORARY/policy.log")" 'false' \
  'source Provider activation policy default'
assert_contains "$(cat "$TEMPORARY/policy.log")" 'true|true' \
  'source model and Web activation policy defaults'
[[ "$(readlink "$TEMPORARY/dev-bin/nico")" == "$LIVE_ROOT/scripts/nico-dev" ]] || \
  fail 'make run did not install the local nico command link'
LINKED_CLI_OUTPUT="$(env -u NICO_API_URL -u NICO_BUILD_VERSION \
  REAL_PYTHON="$ROOT_DIR/.venv/bin/python" \
  "$TEMPORARY/dev-bin/nico" --version)"
assert_contains "$LINKED_CLI_OUTPUT" 'http://localhost:8000|' \
  'linked development CLI API default'
assert_contains "$LINKED_CLI_OUTPUT" "$LIVE_ROOT/.nico/dev/config/cli.toml|development|" \
  'linked development CLI profile isolation'
assert_contains "$LINKED_CLI_OUTPUT" '|--version' 'linked development CLI invocation'
[[ -f "$LIVE_ROOT/.nico/dev/bin/nico-service" && \
   ! -L "$LIVE_ROOT/.nico/dev/bin/nico-service" ]] || \
  fail 'make run did not install an attested development service command'
while IFS= read -r pid; do
  if kill -0 "$pid" 2>/dev/null; then
    fail "local source supervisor left process $pid running after failure"
  fi
done < "$FAKE_PROCESS_LOG"

: > "$FAKE_PROCESS_LOG"
EDITABLE_INSTALLS_BEFORE_RESTART="$(grep -c -- ' -e ' "$TEMPORARY/pip.log")"
REAL_PYTHON="$ROOT_DIR/.venv/bin/python" \
FAKE_PIP_LOG="$TEMPORARY/pip.log" \
FAKE_NPM_LOG="$TEMPORARY/npm.log" \
FAKE_PROCESS_LOG="$FAKE_PROCESS_LOG" \
FAKE_POLICY_LOG="$TEMPORARY/policy.log" \
FAKE_MIGRATION_LOG="$FAKE_MIGRATION_LOG" \
FAKE_DOCKER_LOG="$FAKE_DOCKER_LOG" \
PATH="$LIVE_BIN:$FAKE_BIN:$PATH" \
NICO_BIN_DIR="$TEMPORARY/dev-bin" \
  "$LIVE_ROOT/scripts/local-dev.sh" run >/dev/null 2>&1 &
SUPERVISOR_PID=$!
for _attempt in {1..50}; do
  [[ "$(wc -l < "$FAKE_PROCESS_LOG")" -ge 4 ]] && break
  sleep 0.1
done
[[ "$(wc -l < "$FAKE_PROCESS_LOG")" -ge 4 ]] || \
  fail 'local source supervisor did not start all processes'
[[ "$(grep -c -- ' -e ' "$TEMPORARY/pip.log")" == "$EDITABLE_INSTALLS_BEFORE_RESTART" ]] || \
  fail 'local source restart repeated an unchanged editable install'

RESTART_ID='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
printf '{"restart_id":"%s"}' "$RESTART_ID" \
  > "$LIVE_ROOT/.nico/dev/run/worker-restart-request.json"
chmod 600 "$LIVE_ROOT/.nico/dev/run/worker-restart-request.json"
for _attempt in {1..50}; do
  [[ -f "$LIVE_ROOT/.nico/dev/run/worker-restart-response.json" ]] && break
  sleep 0.1
done
[[ -f "$LIVE_ROOT/.nico/dev/run/worker-restart-response.json" ]] || \
  fail 'local source supervisor did not acknowledge a Worker credential restart'
RESTART_RESPONSE="$(cat "$LIVE_ROOT/.nico/dev/run/worker-restart-response.json")"
assert_contains "$RESTART_RESPONSE" "$RESTART_ID" 'Worker credential restart response'
[[ "$(wc -l < "$FAKE_PROCESS_LOG")" -ge 5 ]] || \
  fail 'local source supervisor did not replace the Worker after a credential update'

if REAL_PYTHON="$ROOT_DIR/.venv/bin/python" \
  FAKE_PIP_LOG="$TEMPORARY/pip.log" \
  FAKE_NPM_LOG="$TEMPORARY/npm.log" \
  FAKE_PROCESS_LOG="$FAKE_PROCESS_LOG" \
  FAKE_POLICY_LOG="$TEMPORARY/policy.log" \
  FAKE_MIGRATION_LOG="$FAKE_MIGRATION_LOG" \
  FAKE_DOCKER_LOG="$FAKE_DOCKER_LOG" \
  PATH="$LIVE_BIN:$FAKE_BIN:$PATH" \
  NICO_BIN_DIR="$TEMPORARY/dev-bin" \
  "$LIVE_ROOT/scripts/local-dev.sh" run > "$TEMPORARY/concurrent-run.log" 2>&1; then
  fail 'local source supervisor allowed a concurrent make run'
fi
assert_contains "$(cat "$TEMPORARY/concurrent-run.log")" 'already active' \
  'concurrent make run guard'
kill -0 "$SUPERVISOR_PID" || fail 'concurrent make run disturbed the active supervisor'

kill -TERM "$SUPERVISOR_PID"
set +e
wait "$SUPERVISOR_PID"
SUPERVISOR_STATUS=$?
set -e
[[ "$SUPERVISOR_STATUS" -eq 143 ]] || \
  fail "local source supervisor returned $SUPERVISOR_STATUS after SIGTERM"
while IFS= read -r pid; do
  if kill -0 "$pid" 2>/dev/null; then
    fail "local source supervisor left process $pid running after SIGTERM"
  fi
done < "$FAKE_PROCESS_LOG"

printf '\n# dependency metadata changed\n' >> "$LIVE_ROOT/backend/pyproject.toml"
printf '\n ' >> "$LIVE_ROOT/frontend/package-lock.json"
EDITABLE_INSTALLS_BEFORE_CHANGE="$(grep -c -- ' -e ' "$TEMPORARY/pip.log")"
NPM_INSTALLS_BEFORE_CHANGE="$(wc -l < "$TEMPORARY/npm.log" | tr -d '[:space:]')"
if REAL_PYTHON="$ROOT_DIR/.venv/bin/python" \
  FAKE_PIP_LOG="$TEMPORARY/pip.log" \
  FAKE_NPM_LOG="$TEMPORARY/npm.log" \
  FAKE_PROCESS_LOG="$FAKE_PROCESS_LOG" \
  FAKE_POLICY_LOG="$TEMPORARY/policy.log" \
  FAKE_MIGRATION_LOG="$FAKE_MIGRATION_LOG" \
  FAKE_DOCKER_LOG="$FAKE_DOCKER_LOG" \
  FAKE_NPM_EXIT=7 NICO_BIN_DIR="$TEMPORARY/dev-bin" PATH="$LIVE_BIN:$FAKE_BIN:$PATH" \
  "$LIVE_ROOT/scripts/local-dev.sh" run >/dev/null 2>&1; then
  fail 'local source supervisor accepted a child-process failure after metadata change'
fi
[[ "$(grep -c -- ' -e ' "$TEMPORARY/pip.log")" -eq \
  "$((EDITABLE_INSTALLS_BEFORE_CHANGE + 1))" ]] || \
  fail 'local source run did not resynchronize changed Python metadata'
[[ "$(wc -l < "$TEMPORARY/npm.log" | tr -d '[:space:]')" -eq \
  "$((NPM_INSTALLS_BEFORE_CHANGE + 1))" ]] || \
  fail 'local source run did not resynchronize changed frontend metadata'

DOWN_OUTPUT="$("$ROOT_DIR/scripts/local-dev.sh" --dry-run infra-down)"
assert_contains "$DOWN_OUTPUT" 'stop postgres redis minio' 'local dependency shutdown'

EXPECTED_VERSION="$(python3 - "$ROOT_DIR/backend/pyproject.toml" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as source:
    print(tomllib.load(source)["project"]["version"])
PY
)"
[[ "$("$ROOT_DIR/scripts/version.sh" current)" == "$EXPECTED_VERSION" ]] || \
  fail 'current version does not come from pyproject.toml'
[[ "$("$ROOT_DIR/scripts/version.sh" tag)" == "v$EXPECTED_VERSION" ]] || \
  fail 'release tag does not match the package version'
DEVELOPMENT_VERSION="$("$ROOT_DIR/scripts/version.sh" development)"
[[ "$DEVELOPMENT_VERSION" =~ ^[a-z0-9][a-z0-9_.-]{0,127}$ && \
   "$DEVELOPMENT_VERSION" == *-* ]] || \
  fail "development version is not branch-commit based: $DEVELOPMENT_VERSION"
[[ "$($ROOT_DIR/scripts/nico-dev --version)" == \
  "nico $DEVELOPMENT_VERSION" ]] || \
  fail 'local nico launcher does not expose the development version'
"$ROOT_DIR/scripts/version.sh" check-tag "v$EXPECTED_VERSION" >/dev/null
if "$ROOT_DIR/scripts/version.sh" check-tag v999.0.0 >/dev/null 2>&1; then
  fail 'version check accepted a mismatched tag'
fi
if "$ROOT_DIR/scripts/version.sh" check-tag "$EXPECTED_VERSION" >/dev/null 2>&1; then
  fail 'version check accepted a tag without the v prefix'
fi

mkdir -p "$TEMPORARY/project/backend" "$TEMPORARY/project/scripts"
cp "$ROOT_DIR/scripts/version.sh" "$TEMPORARY/project/scripts/version.sh"
cp "$ROOT_DIR/backend/pyproject.toml" "$TEMPORARY/project/backend/pyproject.toml"
git -C "$TEMPORARY/project" init --initial-branch=main --quiet
git -C "$TEMPORARY/project" config user.name 'Nico Test'
git -C "$TEMPORARY/project" config user.email 'nico-test@example.invalid'
git -C "$TEMPORARY/project" add backend/pyproject.toml scripts/version.sh
git -C "$TEMPORARY/project" commit --quiet -m 'test fixture'

git -C "$TEMPORARY/project" switch --quiet --create feature/test-tag-guard
if "$TEMPORARY/project/scripts/version.sh" create-tag >/dev/null 2>&1; then
  fail 'create-tag accepted a non-default branch'
fi
git -C "$TEMPORARY/project" switch --quiet main
git -C "$TEMPORARY" init --bare --quiet remote.git
git -C "$TEMPORARY/project" remote add origin "$TEMPORARY/remote.git"
git -C "$TEMPORARY/project" push --quiet --set-upstream origin main

"$TEMPORARY/project/scripts/version.sh" create-tag >/dev/null
[[ "$(git -C "$TEMPORARY/project" cat-file -t "v$EXPECTED_VERSION")" == 'tag' ]] || \
  fail 'create-tag did not create an annotated tag'
[[ "$("$TEMPORARY/project/scripts/version.sh" development)" == \
  "main-v$EXPECTED_VERSION" ]] || fail 'development version omitted the exact annotated tag'
if "$TEMPORARY/project/scripts/version.sh" create-tag >/dev/null 2>&1; then
  fail 'create-tag accepted an existing version tag'
fi
git -C "$TEMPORARY/project" push --quiet origin "v$EXPECTED_VERSION"
git -C "$TEMPORARY/project" tag --delete "v$EXPECTED_VERSION" >/dev/null
if "$TEMPORARY/project/scripts/version.sh" create-tag >/dev/null 2>&1; then
  fail 'create-tag accepted a tag that already exists only on origin'
fi
if git -C "$TEMPORARY/project" rev-parse --verify --quiet \
  "refs/tags/v$EXPECTED_VERSION" >/dev/null; then
  fail 'remote tag collision recreated the local tag'
fi
git -C "$TEMPORARY/project" fetch --quiet origin \
  "refs/tags/v$EXPECTED_VERSION:refs/tags/v$EXPECTED_VERSION"

"$TEMPORARY/project/scripts/version.sh" set 99.0.0 >/dev/null
[[ "$("$TEMPORARY/project/scripts/version.sh" current)" == '99.0.0' ]] || \
  fail 'version-set did not update the single version source'
git -C "$TEMPORARY/project" add backend/pyproject.toml
git -C "$TEMPORARY/project" commit --quiet -m 'advance local main'
[[ "$("$TEMPORARY/project/scripts/version.sh" development)" =~ \
  ^main-v${EXPECTED_VERSION}-1-g[0-9a-f]+$ ]] || \
  fail 'development version omitted commits after the nearest annotated tag'
if "$TEMPORARY/project/scripts/version.sh" create-tag >/dev/null 2>&1; then
  fail 'create-tag accepted a HEAD that differs from origin/main'
fi
if git -C "$TEMPORARY/project" rev-parse --verify --quiet refs/tags/v99.0.0 >/dev/null; then
  fail 'create-tag left a tag after the remote freshness check failed'
fi

"$TEMPORARY/project/scripts/version.sh" set 98.0.0 >/dev/null
if "$TEMPORARY/project/scripts/version.sh" create-tag >/dev/null 2>&1; then
  fail 'create-tag accepted a dirty worktree'
fi

printf '[nico-dev-test] local development and version contracts passed\n'
