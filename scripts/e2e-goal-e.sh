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

archive_and_cleanup() {
  if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
    mkdir -p "$NICO_EVIDENCE_DIR/e2e-goal-e"
    cp "$TMP_DIR"/*.json "$NICO_EVIDENCE_DIR/e2e-goal-e/" 2>/dev/null || true
  fi
  rm -rf "$TMP_DIR"
}
trap archive_and_cleanup EXIT

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

POLICY='{"allow":["file.write@1.0.0","file.read@1.0.0","report.write@1.0.0","python.execute@1.0.0"],"permissions":["filesystem.write","filesystem.read","report.write","code.python.execute"],"tools":{"python.execute@1.0.0":{"wall_time_seconds":5,"memory_bytes":67108864,"pids_limit":8,"output_bytes":4096}}}'

log "creating a Goal E tool-enabled Run through the public API"
request POST /api/v1/tenants/bootstrap \
  "{\"name\":\"Goal E E2E $RUN_KEY\",\"slug\":\"goal-e-$RUN_KEY\",\"settings\":{\"tool_policy\":$POLICY}}" \
  "$TMP_DIR/tenant.json"
TENANT_ID="$(json_field "$TMP_DIR/tenant.json" id)"
HEADERS=(--header "X-Tenant-ID: $TENANT_ID" --header "X-Actor-ID: goal-e-e2e")

request POST /api/v1/projects \
  "{\"name\":\"Tool Gateway Project $RUN_KEY\",\"metadata\":{\"suite\":\"goal-e\"}}" \
  "$TMP_DIR/project.json" "${HEADERS[@]}"
PROJECT_ID="$(json_field "$TMP_DIR/project.json" id)"

request POST /api/v1/agents \
  "{\"name\":\"goal-e-agent-$RUN_KEY\",\"display_name\":\"Goal E Tool Agent\"}" \
  "$TMP_DIR/agent.json" "${HEADERS[@]}"
AGENT_ID="$(json_field "$TMP_DIR/agent.json" id)"

VERSION_BODY="$(python3 - "$POLICY" <<'PY'
import json
import sys

policy = json.loads(sys.argv[1])
print(json.dumps({
    "role": "tool-gateway-acceptance",
    "mandate": "Use only the exact platform tools granted for this Run",
    "boundaries": ["No native tools", "No host access", "No external network"],
    "tool_policy": policy,
    "run_config": {
        "runtime_provider": "mock",
        "mock": {
            "steps": ["gateway-tools"],
            "tool_calls": [
                {
                    "call_id": "write-result",
                    "name": "file.write",
                    "version": "1.0.0",
                    "arguments": {"path": "e2e/result.txt", "content": "goal-e-gateway"},
                },
                {
                    "call_id": "read-result",
                    "name": "file.read",
                    "version": "1.0.0",
                    "arguments": {"path": "e2e/result.txt"},
                },
                {
                    "call_id": "write-report",
                    "name": "report.write",
                    "version": "1.0.0",
                    "arguments": {
                        "path": "reports/goal-e.json",
                        "format": "json",
                        "content": {"status": "verified", "boundary": "tool-gateway"},
                    },
                },
                {
                    "call_id": "sandbox-python",
                    "name": "python.execute",
                    "version": "1.0.0",
                    "arguments": {
                        "code": "import os\nresult = {'uid': os.getuid(), 'sum': input['left'] + input['right']}\n",
                        "input": {"left": 19, "right": 23},
                    },
                },
            ],
        },
    },
}, separators=(",", ":")))
PY
)"
request POST "/api/v1/agents/$AGENT_ID/versions" "$VERSION_BODY" \
  "$TMP_DIR/version.json" "${HEADERS[@]}"
VERSION_ID="$(json_field "$TMP_DIR/version.json" id)"
request POST "/api/v1/agents/$AGENT_ID/versions/$VERSION_ID/publish" \
  '{"expected_revision":1}' "$TMP_DIR/agent-ready.json" "${HEADERS[@]}"

request POST /api/v1/tasks \
  "{\"project_id\":\"$PROJECT_ID\",\"title\":\"Goal E gateway execution\",\"input\":{\"run_key\":\"$RUN_KEY\"},\"acceptance\":{\"tool_calls\":4},\"assignee_agent_id\":\"$AGENT_ID\",\"priority\":100}" \
  "$TMP_DIR/task.json" "${HEADERS[@]}"
TASK_ID="$(json_field "$TMP_DIR/task.json" id)"
request POST "/api/v1/tasks/$TASK_ID/runs" \
  '{"max_steps":16,"token_budget":2000,"timeout_seconds":60}' \
  "$TMP_DIR/run.json" "${HEADERS[@]}"
RUN_ID="$(json_field "$TMP_DIR/run.json" id)"

log "waiting for the Compose worker and Sandbox Runner to complete Run $RUN_ID"
for attempt in {1..120}; do
  request GET "/api/v1/runs/$RUN_ID" '' "$TMP_DIR/run-final.json" "${HEADERS[@]}"
  run_status="$(json_field "$TMP_DIR/run-final.json" status)"
  if [[ "$run_status" == "completed" ]]; then
    break
  fi
  if [[ "$run_status" == "failed" || "$run_status" == "cancelled" || "$run_status" == "timed_out" ]]; then
    die "Goal E E2E Run entered terminal status: $run_status"
  fi
  [[ "$attempt" -eq 120 ]] && die "Goal E E2E Run did not complete"
  sleep 0.5
done

request GET "/api/v1/runs/$RUN_ID/runtime" '' "$TMP_DIR/runtime.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/trajectory" '' "$TMP_DIR/trajectory.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/events" '' "$TMP_DIR/events.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/tool-calls" '' "$TMP_DIR/tool-calls.json" "${HEADERS[@]}"
request GET /api/v1/tool-definitions '' "$TMP_DIR/tool-definitions.json" "${HEADERS[@]}"
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
calls = load("tool-calls.json")
definitions = load("tool-definitions.json")
audit = load("audit.json")
openapi = load("openapi.json")

assert run["status"] == "completed"
assert runtime["status"] == "completed"
assert runtime["provider_name"] == "mock"
assert [call["tool_name"] for call in calls] == [
    "file.write", "file.read", "report.write", "python.execute"
]
assert all(call["status"] == "succeeded" for call in calls)
assert all(len(call["attempts"]) == 1 for call in calls)
assert calls[1]["result"]["content"] == "goal-e-gateway"
assert calls[3]["result"]["result"] == {"uid": 65534, "sum": 42}
assert all("execution_lease_token" not in call for call in calls)
assert {(item["name"], item["version"]) for item in definitions} == {
    ("file.write", "1.0.0"),
    ("file.read", "1.0.0"),
    ("report.write", "1.0.0"),
    ("python.execute", "1.0.0"),
}
trajectory_types = [event["type"] for event in trajectory["events"]]
assert trajectory_types.count("tool.call.started") == 4
assert trajectory_types.count("tool.call.completed") == 4
event_types = {event["event_type"] for event in events}
assert {"ToolCallStarted", "ToolCallSucceeded", "RunCompleted"} <= event_types
actions = {record["action"] for record in audit}
assert {"tool.definition.register", "tool.call.start", "tool.call.finish"} <= actions
assert "/api/v1/tool-definitions" in openapi["paths"]
assert "/api/v1/runs/{run_id}/tool-calls" in openapi["paths"]
serialized = json.dumps({"run": run, "runtime": runtime, "calls": calls, "audit": audit})
assert "nico-sandbox-development-token" not in serialized
assert "execution_lease_token" not in serialized
PY

if docker ps -a --filter label=nico.sandbox=true --format '{{.ID}}' | grep -q .; then
  die "Goal E E2E left sandbox containers behind"
fi

"${COMPOSE[@]}" ps
log "PASS Goal E API -> Runtime -> Tool Gateway -> workspace/report/Python sandbox E2E (run=$RUN_ID)"
