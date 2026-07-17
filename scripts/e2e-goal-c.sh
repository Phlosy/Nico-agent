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
    --fail-with-body
    --silent
    --show-error
    --request "$method"
    --header "Content-Type: application/json"
  )
  if [[ -n "$body" ]]; then
    curl_args+=(--data "$body")
  fi
  curl "${curl_args[@]}" "$@" --output "$output" "$API_BASE$path"
}

log "bootstrapping isolated Goal C tenants"
request POST /api/v1/tenants/bootstrap \
  "{\"name\":\"Goal C E2E $RUN_KEY\",\"slug\":\"goal-c-$RUN_KEY\"}" \
  "$TMP_DIR/tenant.json"
TENANT_ID="$(json_field "$TMP_DIR/tenant.json" id)"
TENANT_HEADERS=(--header "X-Tenant-ID: $TENANT_ID" --header "X-Actor-ID: goal-c-e2e")

request POST /api/v1/tenants/bootstrap \
  "{\"name\":\"Goal C Isolated $RUN_KEY\",\"slug\":\"goal-c-isolated-$RUN_KEY\"}" \
  "$TMP_DIR/isolated-tenant.json"
ISOLATED_TENANT_ID="$(json_field "$TMP_DIR/isolated-tenant.json" id)"

log "creating the Project, Agent and immutable AgentVersion"
request POST /api/v1/projects \
  "{\"name\":\"E2E Project $RUN_KEY\",\"description\":\"Goal C acceptance\",\"metadata\":{\"suite\":\"goal-c\"}}" \
  "$TMP_DIR/project.json" "${TENANT_HEADERS[@]}"
PROJECT_ID="$(json_field "$TMP_DIR/project.json" id)"

request POST /api/v1/agents \
  "{\"name\":\"goal-c-agent-$RUN_KEY\",\"display_name\":\"Goal C Agent\",\"description\":\"Generic control-plane E2E agent\"}" \
  "$TMP_DIR/agent-draft.json" "${TENANT_HEADERS[@]}"
AGENT_ID="$(json_field "$TMP_DIR/agent-draft.json" id)"

request POST "/api/v1/agents/$AGENT_ID/versions" \
  '{"role":"researcher","mandate":"Produce a deterministic acceptance result","boundaries":["No external writes"],"long_term_goal":"Demonstrate durable generic agents","model_config":{"provider":"future-runtime"},"budgets":{"token_limit":1000}}' \
  "$TMP_DIR/version.json" "${TENANT_HEADERS[@]}"
VERSION_ID="$(json_field "$TMP_DIR/version.json" id)"

request POST "/api/v1/agents/$AGENT_ID/versions/$VERSION_ID/publish" \
  '{"expected_revision":1}' \
  "$TMP_DIR/agent-ready.json" "${TENANT_HEADERS[@]}"

log "executing the persisted Task, Run and RunStep lifecycle"
request POST /api/v1/tasks \
  "{\"project_id\":\"$PROJECT_ID\",\"title\":\"Goal C durable execution\",\"input\":{\"run_key\":\"$RUN_KEY\"},\"acceptance\":{\"result\":\"ok\"},\"assignee_agent_id\":\"$AGENT_ID\"}" \
  "$TMP_DIR/task-created.json" "${TENANT_HEADERS[@]}"
TASK_ID="$(json_field "$TMP_DIR/task-created.json" id)"

request POST "/api/v1/tasks/$TASK_ID/runs" \
  '{"max_steps":8,"token_budget":1000,"timeout_seconds":60}' \
  "$TMP_DIR/run-pending.json" "${TENANT_HEADERS[@]}"
RUN_ID="$(json_field "$TMP_DIR/run-pending.json" id)"

request POST "/api/v1/runs/$RUN_ID/transition" \
  '{"target":"planning","expected_revision":1}' \
  "$TMP_DIR/run-planning.json" "${TENANT_HEADERS[@]}"
request POST "/api/v1/runs/$RUN_ID/steps" \
  '{"sequence":1,"kind":"acceptance","input":{"expected":"ok"}}' \
  "$TMP_DIR/step-pending.json" "${TENANT_HEADERS[@]}"
STEP_ID="$(json_field "$TMP_DIR/step-pending.json" id)"
request POST "/api/v1/runs/$RUN_ID/steps/$STEP_ID/transition" \
  '{"target":"running","expected_revision":1}' \
  "$TMP_DIR/step-running.json" "${TENANT_HEADERS[@]}"
request POST "/api/v1/runs/$RUN_ID/steps/$STEP_ID/transition" \
  '{"target":"completed","expected_revision":2,"output":{"result":"ok"}}' \
  "$TMP_DIR/step-completed.json" "${TENANT_HEADERS[@]}"
request POST "/api/v1/runs/$RUN_ID/transition" \
  '{"target":"running","expected_revision":2}' \
  "$TMP_DIR/run-running.json" "${TENANT_HEADERS[@]}"
request POST "/api/v1/runs/$RUN_ID/transition" \
  '{"target":"completed","expected_revision":3,"result":{"result":"ok"}}' \
  "$TMP_DIR/run-completed.json" "${TENANT_HEADERS[@]}"

request GET "/api/v1/tasks/$TASK_ID" '' "$TMP_DIR/task-final.json" "${TENANT_HEADERS[@]}"
request GET "/api/v1/agents/$AGENT_ID/versions" '' "$TMP_DIR/versions.json" \
  "${TENANT_HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/events" '' "$TMP_DIR/events.json" \
  "${TENANT_HEADERS[@]}"
request GET /api/v1/audit '' "$TMP_DIR/audit.json" "${TENANT_HEADERS[@]}"
request GET /openapi.json '' "$TMP_DIR/openapi.json"

log "proving a second tenant cannot observe the first tenant's Agent"
isolation_status="$(curl --silent --show-error \
  --header "X-Tenant-ID: $ISOLATED_TENANT_ID" \
  --output "$TMP_DIR/isolation.json" \
  --write-out '%{http_code}' \
  "$API_BASE/api/v1/agents/$AGENT_ID")"

python3 - "$TMP_DIR" "$AGENT_ID" "$VERSION_ID" "$isolation_status" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
agent_id, version_id, isolation_status = sys.argv[2:]

def load(name):
    return json.loads((root / name).read_text())

tenant = load("tenant.json")
agent = load("agent-ready.json")
versions = load("versions.json")
task = load("task-final.json")
run = load("run-completed.json")
step = load("step-completed.json")
events = load("events.json")
audit = load("audit.json")
openapi = load("openapi.json")

assert tenant["status"] == "active"
assert agent["id"] == agent_id
assert agent["status"] == "ready"
assert agent["current_version_id"] == version_id
assert len(versions) == 1 and versions[0]["status"] == "published"
assert task["status"] == "waiting_for_review"
assert run["status"] == "completed" and run["result"] == {"result": "ok"}
assert step["status"] == "completed" and step["output"] == {"result": "ok"}

event_types = {event["event_type"] for event in events}
assert {"RunCreated", "RunPlanningStarted", "StepStarted", "StepCompleted", "RunCompleted"} <= event_types
audit_actions = {record["action"] for record in audit}
assert {"tenant.create", "agent_version.publish", "run.create", "run.transition"} <= audit_actions
assert "/api/v1/agents/{agent_id}/versions" in openapi["paths"]
assert isolation_status == "404"
assert load("isolation.json")["code"] == "RESOURCE_NOT_FOUND"
PY

"${COMPOSE[@]}" ps
log "PASS Goal C core API E2E (tenant=$TENANT_ID run=$RUN_ID)"
