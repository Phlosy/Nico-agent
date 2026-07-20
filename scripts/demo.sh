#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command curl
require_command python3
load_env_file

API_BASE="${NICO_DEMO_API_BASE:-http://localhost:${API_PORT:-18000}}"
CONSOLE_BASE="${NICO_DEMO_CONSOLE_BASE:-http://localhost:${WEB_PORT:-18080}}"
STATE_DIR="${NICO_DEMO_STATE_DIR:-$ROOT_DIR/.nico}"
STATE_FILE="$STATE_DIR/demo-state.json"
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
    --connect-timeout 5 --max-time 30
  )
  if [[ -n "$body" ]]; then
    curl_args+=(--data "$body")
  fi
  curl "${curl_args[@]}" "$@" --output "$output" "$API_BASE$path"
}

load_reusable_state() {
  [[ -f "$STATE_FILE" ]] || return 1
  local values=()
  while IFS= read -r value; do
    values[${#values[@]}]="$value"
  done < <(
    python3 - "$STATE_FILE" <<'PY'
import json
import sys

try:
    payload = json.load(open(sys.argv[1]))
    for field in ("tenant_id", "project_id", "agent_id", "agent_version_id"):
        value = payload[field]
        if not isinstance(value, str) or not value:
            raise ValueError(field)
        print(value)
except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
PY
  )
  [[ "${#values[@]}" -eq 4 ]] || return 1

  TENANT_ID="${values[0]}"
  PROJECT_ID="${values[1]}"
  AGENT_ID="${values[2]}"
  AGENT_VERSION_ID="${values[3]}"
  local headers=(--header "X-Tenant-ID: $TENANT_ID" --header "X-Actor-ID: nico-demo")

  request GET "/api/v1/projects/$PROJECT_ID" '' "$TMP_DIR/reused-project.json" \
    "${headers[@]}" >/dev/null 2>&1 || return 1
  request GET "/api/v1/agents/$AGENT_ID" '' "$TMP_DIR/reused-agent.json" \
    "${headers[@]}" >/dev/null 2>&1 || return 1
  python3 - "$TMP_DIR/reused-agent.json" "$AGENT_VERSION_ID" <<'PY' || return 1
import json
import sys

agent = json.load(open(sys.argv[1]))
if agent.get("current_version_id") != sys.argv[2] or agent.get("status") != "ready":
    raise SystemExit(1)
PY
}

create_demo_resources() {
  log "creating reusable Demo Tenant, Project and Agent"
  request POST /api/v1/tenants/bootstrap \
    "{\"name\":\"Nico Demo $RUN_KEY\",\"slug\":\"nico-demo-$RUN_KEY\"}" \
    "$TMP_DIR/tenant.json"
  TENANT_ID="$(json_field "$TMP_DIR/tenant.json" id)"
  local headers=(--header "X-Tenant-ID: $TENANT_ID" --header "X-Actor-ID: nico-demo")

  request POST /api/v1/projects \
    '{"name":"Nico Demo","description":"Reusable resources created by scripts/demo.sh","metadata":{"source":"scripts/demo.sh"}}' \
    "$TMP_DIR/project.json" "${headers[@]}"
  PROJECT_ID="$(json_field "$TMP_DIR/project.json" id)"

  request POST /api/v1/agents \
    '{"name":"nico-demo-agent","display_name":"Nico Demo Agent","description":"Deterministic Agent for the local first-run experience"}' \
    "$TMP_DIR/agent.json" "${headers[@]}"
  AGENT_ID="$(json_field "$TMP_DIR/agent.json" id)"

  request POST "/api/v1/agents/$AGENT_ID/versions" \
    '{"role":"demo-assistant","mandate":"Complete the local Nico first-run task with a deterministic result","boundaries":["Do not access external systems","Do not call tools"],"run_config":{"runtime_provider":"mock","mock":{"steps":["validate task","produce result"],"output":{"message":"Nico completed a recoverable, auditable demo Run.","summary":"AgentVersion -> Task -> Run -> Worker -> Result","runtime":"mock"}}}}' \
    "$TMP_DIR/version.json" "${headers[@]}"
  AGENT_VERSION_ID="$(json_field "$TMP_DIR/version.json" id)"

  request POST "/api/v1/agents/$AGENT_ID/versions/$AGENT_VERSION_ID/publish" \
    '{"expected_revision":1}' "$TMP_DIR/published-agent.json" "${headers[@]}"

  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  python3 - "$STATE_FILE.tmp" "$TENANT_ID" "$PROJECT_ID" "$AGENT_ID" "$AGENT_VERSION_ID" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
payload = dict(zip(
    ("tenant_id", "project_id", "agent_id", "agent_version_id"),
    sys.argv[2:],
    strict=True,
))
path.write_text(json.dumps(payload, indent=2) + "\n")
PY
  chmod 600 "$STATE_FILE.tmp"
  mv "$STATE_FILE.tmp" "$STATE_FILE"
}

log "waiting for Nico API at $API_BASE"
wait_for_url "$API_BASE/api/v1/health/ready" "Nico API" 60 2

if load_reusable_state; then
  log "reusing Demo Tenant, Project and published AgentVersion from $STATE_FILE"
else
  create_demo_resources
fi

HEADERS=(--header "X-Tenant-ID: $TENANT_ID" --header "X-Actor-ID: nico-demo")

log "creating a Task and Run"
request POST /api/v1/tasks \
  "{\"project_id\":\"$PROJECT_ID\",\"title\":\"Nico first Run $RUN_KEY\",\"input\":{\"request\":\"Show a deterministic Nico result\"},\"acceptance\":{\"runtime\":\"mock\"},\"assignee_agent_id\":\"$AGENT_ID\",\"priority\":10}" \
  "$TMP_DIR/task.json" "${HEADERS[@]}"
TASK_ID="$(json_field "$TMP_DIR/task.json" id)"

request POST "/api/v1/tasks/$TASK_ID/runs" \
  '{"max_steps":8,"token_budget":1000,"timeout_seconds":30}' \
  "$TMP_DIR/run.json" "${HEADERS[@]}"
RUN_ID="$(json_field "$TMP_DIR/run.json" id)"

log "waiting for Worker to complete Run $RUN_ID"
RUN_STATUS="pending"
for attempt in {1..120}; do
  request GET "/api/v1/runs/$RUN_ID" '' "$TMP_DIR/run-final.json" "${HEADERS[@]}"
  RUN_STATUS="$(json_field "$TMP_DIR/run-final.json" status)"
  if [[ "$RUN_STATUS" == "completed" ]]; then
    break
  fi
  if [[ "$RUN_STATUS" == "failed" || "$RUN_STATUS" == "cancelled" || "$RUN_STATUS" == "timed_out" ]]; then
    python3 -m json.tool "$TMP_DIR/run-final.json" >&2
    die "Demo Run entered terminal status: $RUN_STATUS"
  fi
  [[ "$attempt" -eq 120 ]] && die "Demo Run did not complete within 60 seconds"
  sleep 0.5
done

request GET "/api/v1/runs/$RUN_ID/runtime" '' "$TMP_DIR/runtime.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/events" '' "$TMP_DIR/events.json" "${HEADERS[@]}"

python3 - "$TMP_DIR/run-final.json" "$TMP_DIR/runtime.json" "$TMP_DIR/events.json" <<'PY'
import json
import sys

run = json.load(open(sys.argv[1]))
runtime = json.load(open(sys.argv[2]))
events = json.load(open(sys.argv[3]))
print("\nNico Demo completed")
print("-------------------")
print(f"Run ID:    {run['id']}")
print(f"Status:    {run['status']}")
print(f"Runtime:   {runtime['provider_name']} {runtime['provider_version']}")
print(f"Events:    {len(events)}")
print("Result:")
print(json.dumps(run["result"], ensure_ascii=False, indent=2))
print("Artifact:  not produced by this Mock Runtime demo")
PY

printf '\nConsole:    %s\n' "$CONSOLE_BASE"
printf 'Run API:    %s/api/v1/runs/%s\n' "$API_BASE" "$RUN_ID"
printf 'Events API: %s/api/v1/runs/%s/events\n' "$API_BASE" "$RUN_ID"
printf 'Swagger:    %s/docs\n' "$API_BASE"
printf 'Tenant ID:  %s\n' "$TENANT_ID"
