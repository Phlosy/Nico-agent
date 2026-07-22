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

request_status() {
  local method="$1" path="$2" body="$3" output="$4"
  shift 4
  local args=(--silent --show-error --request "$method")
  if [[ -n "$body" ]]; then
    args+=(--header "Content-Type: application/json" --data "$body")
  fi
  curl "${args[@]}" "$@" --output "$output" --write-out '%{http_code}' "$API_BASE$path"
}

log "starting deterministic Provider model, API, and Native Worker"
"${COMPOSE[@]}" --profile goal-g up --detach --build fake-model api worker
wait_for_service_health fake-model
wait_for_service_health api
wait_for_service_health worker
for attempt in {1..30}; do
  if "${COMPOSE[@]}" exec -T worker python -c \
    'import socket; socket.create_connection(("fake-model", 8100), timeout=2).close()' \
    >/dev/null 2>&1; then
    break
  fi
  [[ "$attempt" -eq 30 ]] && die "Worker could not resolve and connect to fake-model"
  sleep 1
done

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
    if "$CLI" --config-file "$config_file" --json provider add "$provider" \
      --credential-ref env:NICO_MODEL_SECRET_GOAL_G \
      --project "$project_id" --starter-name "discovery-$provider-$round" \
      --starter-display-name "Discovery $provider $round" --yes \
      > "$prefix-discovery.stdout" 2> "$prefix-discovery.stderr"; then
      die "non-interactive Provider discovery accepted a missing model choice"
    fi
    grep -q 'PROVIDER_MODEL_REQUIRED' "$prefix-discovery.stderr" || die \
      "Provider discovery did not return the stable explicit-model requirement"
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

    if [[ "$round" == 1 ]]; then
      agent_revision="$(json_path "$prefix-activation.json" activation agent_revision)"
      "$CLI" --config-file "$config_file" --json provider configure "$provider" \
        --credential-ref env:NICO_MODEL_SECRET_GOAL_G \
        --model "$model" --project "$project_id" --agent "$agent_id" \
        --agent-revision "$agent_revision" --yes \
        > "$prefix-rotation.json"
      python3 - "$prefix-activation.json" "$prefix-rotation.json" <<'PY'
import json
import sys

initial, rotated = (json.load(open(path)) for path in sys.argv[1:])
assert rotated["activation"]["agent_id"] == initial["activation"]["agent_id"]
assert rotated["activation"]["agent_version"] > initial["activation"]["agent_version"]
assert rotated["activation"]["endpoint_reused"] is True
PY
    fi

    if [[ "$round" == 1 && "$provider" == "openai" ]]; then
      log "checking active Run maintenance refusal"
      run_id="$(json_path "$prefix-chat.json" turn run_id)"
      "${COMPOSE[@]}" stop worker >/dev/null
      "${COMPOSE[@]}" exec -T postgres psql \
        -U "${POSTGRES_USER:-nico}" -d "${POSTGRES_DB:-nico_agent}" \
        -c "UPDATE runs SET status = 'pending' WHERE id = '$run_id'::uuid" >/dev/null
      attempt_id="$(python3 -c 'import uuid; print(uuid.uuid4())')"
      if printf '%s' "{\"attempt_id\":\"$attempt_id\",\"lease_seconds\":300}" | \
        "${COMPOSE[@]}" exec -T api python -m nico_agent.local_control acquire \
        > "$TMP_DIR/active-run-maintenance.json" 2>/dev/null; then
        maintenance_token="$(json_path "$TMP_DIR/active-run-maintenance.json" token)"
        printf '%s' "{\"attempt_id\":\"$attempt_id\",\"token\":\"$maintenance_token\"}" | \
          "${COMPOSE[@]}" exec -T api python -m nico_agent.local_control release \
          >/dev/null 2>&1 || true
        die "Provider maintenance started while a Run was active"
      fi
      grep -q 'RUNTIME_MAINTENANCE_ACTIVE_RUNS' \
        "$TMP_DIR/active-run-maintenance.json" || die \
        "active Run maintenance refusal did not use the stable error code"
      "${COMPOSE[@]}" exec -T postgres psql \
        -U "${POSTGRES_USER:-nico}" -d "${POSTGRES_DB:-nico_agent}" \
        -c "UPDATE runs SET status = 'completed' WHERE id = '$run_id'::uuid" >/dev/null
      "${COMPOSE[@]}" --profile goal-g up --detach worker >/dev/null
      wait_for_service_health worker

      log "checking failed activation preserves the current route"
      probe_id="$(json_path "$prefix-test.json" id)"
      candidate_hash="$(json_path "$prefix-test.json" candidate_hash)"
      current_revision="$(json_path "$prefix-rotation.json" activation agent_revision)"
      preview_body="{\"probe_id\":\"$probe_id\",\"candidate_hash\":\"$candidate_hash\",\"target\":{\"project_id\":\"$project_id\",\"agent_id\":\"$agent_id\",\"expected_agent_revision\":$current_revision}}"
      request POST /api/v1/provider-activation/preview "$preview_body" \
        "$TMP_DIR/failure-preview.json" "${headers[@]}"
      failure_body="{\"probe_id\":\"$probe_id\",\"candidate_hash\":\"$candidate_hash\",\"preview_hash\":\"$(printf '0%.0s' {1..64})\",\"target\":{\"project_id\":\"$project_id\",\"agent_id\":\"$agent_id\",\"expected_agent_revision\":$current_revision}}"
      status="$(request_status POST /api/v1/provider-activation "$failure_body" \
        "$TMP_DIR/failure-activation.json" "${headers[@]}")"
      [[ "$status" == 409 ]] || die "stale Provider activation was not rejected"
      "$CLI" --config-file "$config_file" --json chat \
        "Confirm the previous route survived failed activation." \
        --project "$project_id" --agent "$agent_id" \
        > "$TMP_DIR/post-failure-chat.json"
      [[ "$(json_path "$TMP_DIR/post-failure-chat.json" turn run_status)" == "completed" ]] || \
        die "failed Provider activation damaged the previous route"
    fi
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
