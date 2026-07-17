#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command curl
require_command python3
load_env_file
"$ROOT_DIR/scripts/dev.sh" --detach

API_BASE="http://localhost:${API_PORT:-18000}"
TMP_DIR="$(mktemp -d)"
RUN_KEY="$(date -u +%Y%m%d%H%M%S)-$$"
trap 'rm -rf "$TMP_DIR"' EXIT

json_field() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$1" "$2"
}

request() {
  local method="$1"
  local path="$2"
  local body="$3"
  local output="$4"
  shift 4
  local curl_args=(
    --fail-with-body --silent --show-error --request "$method"
    --header "Content-Type: application/json"
  )
  if [[ -n "$body" ]]; then
    curl_args+=(--data "$body")
  fi
  curl "${curl_args[@]}" "$@" --output "$output" "$API_BASE$path"
}

log "creating a Goal D Run through the public API"
request POST /api/v1/tenants/bootstrap \
  "{\"name\":\"Goal D E2E $RUN_KEY\",\"slug\":\"goal-d-$RUN_KEY\"}" \
  "$TMP_DIR/tenant.json"
TENANT_ID="$(json_field "$TMP_DIR/tenant.json" id)"
HEADERS=(--header "X-Tenant-ID: $TENANT_ID" --header "X-Actor-ID: goal-d-e2e")

request POST /api/v1/projects \
  "{\"name\":\"Runtime Project $RUN_KEY\",\"metadata\":{\"suite\":\"goal-d\"}}" \
  "$TMP_DIR/project.json" "${HEADERS[@]}"
PROJECT_ID="$(json_field "$TMP_DIR/project.json" id)"

request POST /api/v1/agents \
  "{\"name\":\"goal-d-agent-$RUN_KEY\",\"display_name\":\"Goal D Runtime Agent\"}" \
  "$TMP_DIR/agent.json" "${HEADERS[@]}"
AGENT_ID="$(json_field "$TMP_DIR/agent.json" id)"

request POST "/api/v1/agents/$AGENT_ID/versions" \
  '{"role":"runtime-acceptance","mandate":"Complete the deterministic task","boundaries":["No external mutation"],"run_config":{"runtime_provider":"mock","mock":{"steps":["plan","execute"],"output":{"result":"goal-d-ok"}}}}' \
  "$TMP_DIR/version.json" "${HEADERS[@]}"
VERSION_ID="$(json_field "$TMP_DIR/version.json" id)"
request POST "/api/v1/agents/$AGENT_ID/versions/$VERSION_ID/publish" \
  '{"expected_revision":1}' "$TMP_DIR/agent-ready.json" "${HEADERS[@]}"

request POST /api/v1/tasks \
  "{\"project_id\":\"$PROJECT_ID\",\"title\":\"Goal D worker execution\",\"input\":{\"run_key\":\"$RUN_KEY\"},\"acceptance\":{\"result\":\"goal-d-ok\"},\"assignee_agent_id\":\"$AGENT_ID\",\"priority\":100}" \
  "$TMP_DIR/task.json" "${HEADERS[@]}"
TASK_ID="$(json_field "$TMP_DIR/task.json" id)"
request POST "/api/v1/tasks/$TASK_ID/runs" \
  '{"max_steps":8,"token_budget":1000,"timeout_seconds":30}' \
  "$TMP_DIR/run.json" "${HEADERS[@]}"
RUN_ID="$(json_field "$TMP_DIR/run.json" id)"

log "waiting for the Compose worker to claim and complete Run $RUN_ID"
for attempt in {1..60}; do
  request GET "/api/v1/runs/$RUN_ID" '' "$TMP_DIR/run-final.json" "${HEADERS[@]}"
  run_status="$(json_field "$TMP_DIR/run-final.json" status)"
  if [[ "$run_status" == "completed" ]]; then
    break
  fi
  if [[ "$run_status" == "failed" || "$run_status" == "cancelled" || "$run_status" == "timed_out" ]]; then
    die "Goal D E2E Run entered terminal status: $run_status"
  fi
  [[ "$attempt" -eq 60 ]] && die "Goal D E2E Run did not complete"
  sleep 0.5
done

request GET "/api/v1/runs/$RUN_ID/runtime" '' "$TMP_DIR/runtime.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/trajectory" '' "$TMP_DIR/trajectory.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/events" '' "$TMP_DIR/events.json" "${HEADERS[@]}"
request GET /api/v1/audit '' "$TMP_DIR/audit.json" "${HEADERS[@]}"
request GET /openapi.json '' "$TMP_DIR/openapi.json"

python3 - "$TMP_DIR" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])

def load(name):
    return json.loads((root / name).read_text())

run = load("run-final.json")
runtime = load("runtime.json")
trajectory = load("trajectory.json")
events = load("events.json")
audit = load("audit.json")
openapi = load("openapi.json")

assert run["status"] == "completed"
assert run["result"] == {"result": "goal-d-ok"}
assert runtime["status"] == "completed"
assert runtime["provider_name"] == "mock"
assert runtime["last_event_sequence"] == len(trajectory["events"])
assert trajectory["status"] == "completed"
assert trajectory["events"][-1]["type"] == "run.completed"
assert {event["event_type"] for event in events} >= {
    "RunPlanningStarted", "RuntimeRunStarted", "RuntimeStepStarted",
    "RuntimeStepCompleted", "RuntimeRunCompleted", "RunCompleted",
}
assert {record["action"] for record in audit} >= {
    "runtime.claim", "runtime.session.bind", "runtime.event.record", "runtime.complete",
}
assert "/api/v1/runs/{run_id}/runtime" in openapi["paths"]
assert "/api/v1/runs/{run_id}/trajectory" in openapi["paths"]
PY

"${COMPOSE[@]}" ps
log "PASS Goal D API -> PostgreSQL lease -> Mock Provider -> trajectory E2E (run=$RUN_ID)"
