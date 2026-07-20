#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command curl
require_command python3
load_env_file

export NICO_WORKER_LEASE_SECONDS=10
export NICO_WORKER_HEARTBEAT_SECONDS=1

log "starting default Native-first stack without the Hermes profile"
"${COMPOSE[@]}" --profile goal-l stop worker-hermes-contract worker-hermes >/dev/null 2>&1 || true
"${COMPOSE[@]}" up --detach --build --force-recreate api worker
wait_for_service_health api

API_BASE="http://localhost:${API_PORT:-18000}"
TMP_DIR="$(mktemp -d)"
RUN_KEY="$(date -u +%Y%m%d%H%M%S)-$$"

archive_and_cleanup() {
  if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
    mkdir -p "$NICO_EVIDENCE_DIR/e2e-goal-l"
    cp "$TMP_DIR"/* "$NICO_EVIDENCE_DIR/e2e-goal-l/" 2>/dev/null || true
    "${COMPOSE[@]}" logs --no-color worker worker-hermes-contract \
      >"$NICO_EVIDENCE_DIR/e2e-goal-l/worker.log" 2>&1 || true
  fi
  rm -rf "$TMP_DIR"
}
trap archive_and_cleanup EXIT

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

wait_for_terminal() {
  local run_id="$1" output="$2"
  shift 2
  for attempt in {1..160}; do
    request GET "/api/v1/runs/$run_id" '' "$output" "$@"
    local status
    status="$(json_path "$output" status)"
    [[ "$status" =~ ^(completed|failed|cancelled|timed_out)$ ]] && return 0
    sleep 0.25
  done
  die "Run $run_id did not become terminal"
}

request POST /api/v1/tenants/bootstrap \
  "{\"name\":\"Goal L E2E $RUN_KEY\",\"slug\":\"goal-l-$RUN_KEY\"}" \
  "$TMP_DIR/tenant.json"
TENANT_ID="$(json_path "$TMP_DIR/tenant.json" id)"
HEADERS=(--header "X-Tenant-ID: $TENANT_ID" --header "X-Actor-ID: goal-l-verifier")

request POST /api/v1/projects \
  "{\"name\":\"Adapter Project $RUN_KEY\"}" \
  "$TMP_DIR/project.json" "${HEADERS[@]}"
PROJECT_ID="$(json_path "$TMP_DIR/project.json" id)"
request POST /api/v1/agents \
  "{\"name\":\"goal-l-agent-$RUN_KEY\",\"display_name\":\"Goal L Hermes Adapter\"}" \
  "$TMP_DIR/agent.json" "${HEADERS[@]}"
AGENT_ID="$(json_path "$TMP_DIR/agent.json" id)"
request POST "/api/v1/agents/$AGENT_ID/versions" \
  '{"role":"adapter-contract","mandate":"Complete the adapter contract task.","boundaries":["Never expose secrets"],"runtime_provider":"hermes","execution_mode":"direct"}' \
  "$TMP_DIR/version.json" "${HEADERS[@]}"
VERSION_ID="$(json_path "$TMP_DIR/version.json" id)"
request POST "/api/v1/agents/$AGENT_ID/versions/$VERSION_ID/publish" \
  '{"expected_revision":1}' "$TMP_DIR/agent-ready.json" "${HEADERS[@]}"

log "proving a disabled Hermes adapter fails closed without Native fallback"
request POST /api/v1/tasks \
  "{\"project_id\":\"$PROJECT_ID\",\"title\":\"Hermes disabled contract\",\"input\":{\"mode\":\"disabled\"},\"acceptance\":{\"non_empty\":true},\"assignee_agent_id\":\"$AGENT_ID\",\"priority\":100}" \
  "$TMP_DIR/disabled-task.json" "${HEADERS[@]}"
DISABLED_TASK_ID="$(json_path "$TMP_DIR/disabled-task.json" id)"
request POST "/api/v1/tasks/$DISABLED_TASK_ID/runs" \
  '{"max_steps":2,"token_budget":500,"timeout_seconds":30}' \
  "$TMP_DIR/disabled-run.json" "${HEADERS[@]}"
DISABLED_RUN_ID="$(json_path "$TMP_DIR/disabled-run.json" id)"
wait_for_terminal "$DISABLED_RUN_ID" "$TMP_DIR/disabled-run-final.json" "${HEADERS[@]}"

python3 - "$TMP_DIR/disabled-run-final.json" <<'PY'
import json
import sys

run = json.load(open(sys.argv[1]))
assert run["status"] == "failed", run
assert run["error"]["code"] == "RUNTIME_PROVIDER_NOT_FOUND", run
assert "hermes" in run["error"]["message"].lower(), run
PY

log "switching to the explicit hermetic Hermes contract worker"
"${COMPOSE[@]}" stop worker
"${COMPOSE[@]}" --profile goal-l up --detach --build --force-recreate \
  api worker-hermes-contract
wait_for_service_health api

request POST /api/v1/tasks \
  "{\"project_id\":\"$PROJECT_ID\",\"title\":\"Hermes v2 contract\",\"input\":{\"mode\":\"enabled\"},\"acceptance\":{\"non_empty\":true},\"assignee_agent_id\":\"$AGENT_ID\",\"priority\":100}" \
  "$TMP_DIR/enabled-task.json" "${HEADERS[@]}"
ENABLED_TASK_ID="$(json_path "$TMP_DIR/enabled-task.json" id)"
request POST "/api/v1/tasks/$ENABLED_TASK_ID/runs" \
  '{"max_steps":2,"token_budget":500,"timeout_seconds":30}' \
  "$TMP_DIR/enabled-run.json" "${HEADERS[@]}"
ENABLED_RUN_ID="$(json_path "$TMP_DIR/enabled-run.json" id)"
wait_for_terminal "$ENABLED_RUN_ID" "$TMP_DIR/enabled-run-final.json" "${HEADERS[@]}"

request GET "/api/v1/runs/$ENABLED_RUN_ID/runtime" '' \
  "$TMP_DIR/runtime.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$ENABLED_RUN_ID/trajectory" '' \
  "$TMP_DIR/trajectory.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$ENABLED_RUN_ID/events" '' \
  "$TMP_DIR/events.json" "${HEADERS[@]}"

python3 - "$TMP_DIR" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
load = lambda name: json.loads((root / name).read_text())
run = load("enabled-run-final.json")
runtime = load("runtime.json")
trajectory = load("trajectory.json")
events = load("events.json")

assert run["status"] == "completed", run
assert run["result"] == {"message": "Hermes adapter contract complete"}, run
assert runtime["provider_name"] == "hermes"
assert runtime["provider_version"] == "0.18.2"
assert runtime["protocol_version"] == "2.0"
assert runtime["provider_resolution_source"] == "agent_version"
assert runtime["legacy_resolver_used"] is False
assert runtime["provider_compatibility"]["implementation"] == "adapter"
assert "planning" not in runtime["capabilities"]
assert "coordination" not in runtime["capabilities"]
assert trajectory["provider"] == "hermes"
assert trajectory["status"] == "completed"
assert any(item["event_type"] == "RuntimeProviderResolved" for item in events)

serialized = "\n".join(path.read_text(errors="replace") for path in root.iterdir())
for forbidden in ("lease_token", "mcp_token", "authorization: bearer"):
    assert forbidden not in serialized.lower()
PY

log "PASS Goal L disabled fail-closed and optional Hermes protocol v2 parity"
