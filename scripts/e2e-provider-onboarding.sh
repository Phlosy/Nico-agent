#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command curl
require_command python3
load_env_file
ensure_python_environment

CANARY="provider-e2e-canary-token"
export NICO_MODEL_SECRET_GOAL_G="$CANARY"
export NICO_MODEL_TRUSTED_PRIVATE_HOSTS='["fake-model"]'
export NICO_MODEL_ALLOW_HTTP_TRUSTED_HOSTS=true
export NICO_MODEL_ENDPOINT_WRITES_ENABLED=true
export NICO_PROVIDER_E2E_ALLOW_HTTP=true
export NICO_PROVIDER_E2E_BASE_URL=http://fake-model:8100

TMP_DIR="$(mktemp -d)"
API_BASE="http://localhost:${API_PORT:-18000}"
CLI="$ROOT_DIR/.venv/bin/nico"

cleanup() {
  if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
    mkdir -p "$NICO_EVIDENCE_DIR/provider-onboarding"
    cp "$TMP_DIR"/* "$NICO_EVIDENCE_DIR/provider-onboarding/" 2>/dev/null || true
  fi
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

json_path() {
  python3 - "$@" <<'PY'
import json
import sys

value = json.load(open(sys.argv[1]))
for key in sys.argv[2:]:
    value = value[int(key)] if isinstance(value, list) else value[key]
print(value)
PY
}

request() {
  local method="$1" path="$2" body="$3" output="$4"
  shift 4
  local args=(--fail-with-body --silent --show-error --request "$method")
  if [[ -n "$body" ]]; then
    args+=(--header "Content-Type: application/json" --data "$body")
  fi
  curl "${args[@]}" "$@" --output "$output" "$API_BASE$path"
}

log "starting deterministic Provider model, API, and Native Worker"
"${COMPOSE[@]}" --profile goal-g up --detach --build fake-model api worker
wait_for_service_health fake-model
wait_for_service_health api
wait_for_service_health worker

request GET /api/v1/provider-catalog '' "$TMP_DIR/catalog.json"
python3 - "$TMP_DIR/catalog.json" <<'PY'
import json
import sys

catalog = json.load(open(sys.argv[1]))
assert catalog["schema_version"] == 1
assert len(catalog["providers"]) == 10
assert {item["key"] for item in catalog["providers"]} >= {
    "openai", "anthropic", "google-gemini", "openrouter", "xai",
    "deepseek", "alibaba-bailian", "moonshot-kimi", "zhipu-glm", "minimax",
}
PY

providers=(openai anthropic google-gemini)
models=(fake-openai fake-anthropic fake-gemini)

for round in 1 2; do
  for index in 0 1 2; do
    provider="${providers[$index]}"
    model="${models[$index]}"
    key="$(date -u +%Y%m%d%H%M%S)-$$-$round-$index"
    prefix="$TMP_DIR/$round-$provider"
    request POST /api/v1/tenants/bootstrap \
      "{\"name\":\"Provider $provider $key\",\"slug\":\"provider-$provider-$key\"}" \
      "$prefix-tenant.json"
    tenant_id="$(json_path "$prefix-tenant.json" id)"
    headers=(--header "X-Tenant-ID: $tenant_id" --header "X-Actor-ID: provider-e2e")
    request POST /api/v1/projects \
      "{\"name\":\"Provider Project $key\"}" "$prefix-project.json" "${headers[@]}"
    project_id="$(json_path "$prefix-project.json" id)"
    config_file="$prefix-config.toml"
    "$CLI" --config-file "$config_file" --json config set e2e \
      --api-url "$API_BASE" --tenant-id "$tenant_id" --actor-id provider-e2e \
      > "$prefix-config-output.json"
    "$CLI" --config-file "$config_file" --json config use e2e \
      > "$prefix-config-use.json"
    "$CLI" --config-file "$config_file" --json provider add "$provider" \
      --credential-ref env:NICO_MODEL_SECRET_GOAL_G \
      --model "$model" --project "$project_id" \
      --starter-name "e2e-$provider-$round" \
      --starter-display-name "E2E $provider $round" --yes \
      > "$prefix-activation.json"
    agent_id="$(json_path "$prefix-activation.json" activation agent_id)"
    "$CLI" --config-file "$config_file" --json provider test "$provider" \
      > "$prefix-test.json"
    "$CLI" --config-file "$config_file" --json provider list \
      > "$prefix-list.json"
    "$CLI" --config-file "$config_file" --json chat \
      "Confirm the deterministic Provider route." \
      --project "$project_id" --agent "$agent_id" \
      > "$prefix-chat.json"
    python3 - "$prefix-activation.json" "$prefix-test.json" "$prefix-chat.json" <<'PY'
import json
import sys

activation, probe, chat = (json.load(open(path)) for path in sys.argv[1:])
assert activation["status"] == "ready"
assert probe["status"] == "succeeded" and probe["verified_at"]
assert chat["turn"]["run_status"] == "completed"
assert chat["turn"]["assistant_output"]["content"]
PY
  done
done

log "checking stable bad-auth behavior"
key="$(date -u +%Y%m%d%H%M%S)-$$-bad-auth"
request POST /api/v1/tenants/bootstrap \
  "{\"name\":\"Provider bad auth $key\",\"slug\":\"provider-bad-auth-$key\"}" \
  "$TMP_DIR/bad-tenant.json"
tenant_id="$(json_path "$TMP_DIR/bad-tenant.json" id)"
headers=(--header "X-Tenant-ID: $tenant_id" --header "X-Actor-ID: provider-e2e")
request POST /api/v1/projects "{\"name\":\"Bad Auth $key\"}" \
  "$TMP_DIR/bad-project.json" "${headers[@]}"
project_id="$(json_path "$TMP_DIR/bad-project.json" id)"
bad_config="$TMP_DIR/bad-config.toml"
"$CLI" --config-file "$bad_config" --json config set e2e \
  --api-url "$API_BASE" --tenant-id "$tenant_id" > /dev/null
"$CLI" --config-file "$bad_config" --json config use e2e > /dev/null
if "$CLI" --config-file "$bad_config" --json provider add openai \
  --credential-ref env:NICO_MODEL_SECRET_E2E_MISSING \
  --model fake-openai --project "$project_id" \
  --starter-name bad-auth --starter-display-name "Bad Auth" --yes \
  > "$TMP_DIR/bad-auth.stdout" 2> "$TMP_DIR/bad-auth.stderr"; then
  die "Provider onboarding accepted an unavailable credential"
fi
grep -q 'PROVIDER_AUTH_FAILED' "$TMP_DIR/bad-auth.stderr" || die \
  "bad Provider credential did not return the stable authentication error"

"${COMPOSE[@]}" logs --no-color api worker fake-model > "$TMP_DIR/compose.log"
"${COMPOSE[@]}" exec -T postgres pg_dump \
  -U "${POSTGRES_USER:-nico}" -d "${POSTGRES_DB:-nico_agent}" --data-only \
  > "$TMP_DIR/database.sql"
if grep -R -F "$CANARY" "$TMP_DIR" >/dev/null; then
  die "Provider canary leaked into CLI output, logs, config, or persisted database facts"
fi

log "PASS deterministic Provider setup, activation, test, chat, and redaction"
