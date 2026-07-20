#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command curl
require_command docker
require_command python3
load_env_file

export NICO_MODEL_SECRET_GOAL_G="${NICO_MODEL_SECRET_GOAL_G:-goal-g-fake-token}"
export NICO_MODEL_TRUSTED_PRIVATE_HOSTS='["fake-model"]'
export NICO_MODEL_ALLOW_HTTP_TRUSTED_HOSTS=true
export NICO_MODEL_ENDPOINT_WRITES_ENABLED=true
export NICO_WORKER_LEASE_SECONDS=5
export NICO_WORKER_HEARTBEAT_SECONDS=1
export NICO_WORKER_POLL_INTERVAL_SECONDS=0.2
export NICO_NATIVE_POST_TOOL_DELAY_SECONDS=30
export NICO_TOOL_APPROVAL_REQUIRED_RISKS='[]'

log "starting Goal H fault-injection stack"
"${COMPOSE[@]}" --profile goal-h up --detach --build --force-recreate fake-model api worker
wait_for_service_health fake-model
wait_for_service_health api

API_BASE="http://localhost:${API_PORT:-18000}"
TMP_DIR="$(mktemp -d)"
RUN_KEY="$(date -u +%Y%m%d%H%M%S)-$$"

archive_and_cleanup() {
  if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
    mkdir -p "$NICO_EVIDENCE_DIR/e2e-goal-h"
    cp "$TMP_DIR"/* "$NICO_EVIDENCE_DIR/e2e-goal-h/" 2>/dev/null || true
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

POLICY='{"allow":["file.write@1.0.0","python.execute@1.0.0"],"permissions":["filesystem.write","code.python.execute"],"tools":{"python.execute@1.0.0":{"wall_time_seconds":5,"memory_bytes":67108864,"pids_limit":8,"output_bytes":4096}}}'

request POST /api/v1/tenants/bootstrap \
  "{\"name\":\"Goal H E2E $RUN_KEY\",\"slug\":\"goal-h-$RUN_KEY\",\"settings\":{\"tool_policy\":$POLICY}}" \
  "$TMP_DIR/tenant.json"
TENANT_ID="$(json_path "$TMP_DIR/tenant.json" id)"
HEADERS=(--header "X-Tenant-ID: $TENANT_ID" --header "X-Actor-ID: goal-h-verifier")

request POST /api/v1/model-endpoints \
  '{"stable_key":"goal-h-model","display_name":"Goal H fake model","base_url":"http://fake-model:8100/v1","credential_ref":"env:NICO_MODEL_SECRET_GOAL_G","allowed_models":["goal-h-fake"],"capabilities":{"streaming":true,"tools":true}}' \
  "$TMP_DIR/model-endpoint.json" "${HEADERS[@]}"
ENDPOINT_ID="$(json_path "$TMP_DIR/model-endpoint.json" id)"

request POST /api/v1/projects \
  "{\"name\":\"ReAct Recovery $RUN_KEY\"}" \
  "$TMP_DIR/project.json" "${HEADERS[@]}"
PROJECT_ID="$(json_path "$TMP_DIR/project.json" id)"
request POST /api/v1/agents \
  "{\"name\":\"goal-h-agent-$RUN_KEY\",\"display_name\":\"Goal H ReAct Agent\"}" \
  "$TMP_DIR/agent.json" "${HEADERS[@]}"
AGENT_ID="$(json_path "$TMP_DIR/agent.json" id)"

VERSION_BODY="$(python3 - "$ENDPOINT_ID" "$POLICY" <<'PY'
import json
import sys

print(json.dumps({
    "role": "fault-recovery-verifier",
    "mandate": "Use the exact authorized tools, observe their results, and finish concisely.",
    "boundaries": ["Never bypass the Tool Gateway", "Never repeat a successful side effect"],
    "runtime_provider": "nico_native",
    "execution_mode": "react",
    "model_endpoint_id": sys.argv[1],
    "model_name": "goal-h-fake",
    "model_config": {"temperature": 0, "max_output_tokens": 128},
    "tool_policy": json.loads(sys.argv[2]),
    "budgets": {"max_iterations": 4, "max_tool_calls": 2},
}, separators=(",", ":")))
PY
)"
request POST "/api/v1/agents/$AGENT_ID/versions" "$VERSION_BODY" \
  "$TMP_DIR/agent-version.json" "${HEADERS[@]}"
VERSION_ID="$(json_path "$TMP_DIR/agent-version.json" id)"
request POST "/api/v1/agents/$AGENT_ID/versions/$VERSION_ID/publish" \
  '{"expected_revision":1}' "$TMP_DIR/agent-ready.json" "${HEADERS[@]}"

request POST /api/v1/tasks \
  "{\"project_id\":\"$PROJECT_ID\",\"title\":\"Prove crash-safe ReAct recovery\",\"input\":{\"run_key\":\"$RUN_KEY\"},\"acceptance\":{\"tools\":[\"file.write\",\"python.execute\"]},\"assignee_agent_id\":\"$AGENT_ID\",\"priority\":100}" \
  "$TMP_DIR/task.json" "${HEADERS[@]}"
TASK_ID="$(json_path "$TMP_DIR/task.json" id)"
request POST "/api/v1/tasks/$TASK_ID/runs" \
  '{"max_steps":4,"token_budget":2000,"timeout_seconds":120}' \
  "$TMP_DIR/run.json" "${HEADERS[@]}"
RUN_ID="$(json_path "$TMP_DIR/run.json" id)"

INITIAL_WORKER_ID="$("${COMPOSE[@]}" ps --quiet worker)"
log "waiting for file.write to commit before killing worker $INITIAL_WORKER_ID"
for attempt in {1..120}; do
  request GET "/api/v1/runs/$RUN_ID/tool-calls" '' "$TMP_DIR/tool-calls-before-kill.json" \
    "${HEADERS[@]}"
  if python3 - "$TMP_DIR/tool-calls-before-kill.json" <<'PY'
import json
import sys

calls = json.load(open(sys.argv[1]))
raise SystemExit(0 if any(c["tool_name"] == "file.write" and c["status"] == "succeeded" for c in calls) else 1)
PY
  then
    break
  fi
  [[ "$attempt" -eq 120 ]] && die "file.write did not reach succeeded before fault injection"
  sleep 0.25
done

"${COMPOSE[@]}" kill --signal SIGKILL worker
docker inspect --format '{{json .State}}' "$INITIAL_WORKER_ID" \
  >"$TMP_DIR/killed-worker-state.json"
log "worker killed after side effect; waiting for the five-second lease to expire"
sleep 7

export NICO_NATIVE_POST_TOOL_DELAY_SECONDS=0
"${COMPOSE[@]}" --profile goal-h up --detach --build --force-recreate worker
RECOVERY_WORKER_ID="$("${COMPOSE[@]}" ps --quiet worker)"
[[ -n "$RECOVERY_WORKER_ID" && "$RECOVERY_WORKER_ID" != "$INITIAL_WORKER_ID" ]] \
  || die "replacement worker was not created"

log "waiting for recovered Run $RUN_ID"
for attempt in {1..240}; do
  request GET "/api/v1/runs/$RUN_ID" '' "$TMP_DIR/run-final.json" "${HEADERS[@]}"
  run_status="$(json_path "$TMP_DIR/run-final.json" status)"
  [[ "$run_status" == "completed" ]] && break
  if [[ "$run_status" =~ ^(failed|cancelled|timed_out)$ ]]; then
    cat "$TMP_DIR/run-final.json" >&2
    "${COMPOSE[@]}" logs --no-color --tail=120 worker >&2
    die "Goal H recovered Run entered $run_status"
  fi
  [[ "$attempt" -eq 240 ]] && die "Goal H recovered Run did not complete"
  sleep 0.5
done

request GET "/api/v1/runs/$RUN_ID/runtime" '' "$TMP_DIR/runtime.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/model-calls" '' "$TMP_DIR/model-calls.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/contexts" '' "$TMP_DIR/contexts.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/tool-calls" '' "$TMP_DIR/tool-calls.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/events" '' "$TMP_DIR/events.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/trajectory" '' "$TMP_DIR/trajectory.json" "${HEADERS[@]}"

"${COMPOSE[@]}" exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At \
  -c "SELECT COALESCE(json_agg(row_to_json(x) ORDER BY x.sequence), '[]'::json) FROM (SELECT sequence, step_key, step_type, iteration, parent_step_id, context_snapshot_id, model_call_id, status FROM run_steps WHERE run_id = '$RUN_ID' ORDER BY sequence) x" \
  >"$TMP_DIR/run-steps.json"
"${COMPOSE[@]}" exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At \
  -c "SELECT count(*) FROM audit_records WHERE resource_type = 'tool_call' AND resource_id IN (SELECT id FROM tool_calls WHERE run_id = '$RUN_ID')" \
  >"$TMP_DIR/tool-audit-count.txt"
"${COMPOSE[@]}" exec -T worker python - "$TENANT_ID" "$RUN_ID" <<'PY' \
  >"$TMP_DIR/workspace-proof.txt"
import pathlib
import sys

path = pathlib.Path("/var/lib/nico/workspaces") / sys.argv[1] / sys.argv[2] / "goal-h/recovery-proof.txt"
print(path.read_text(), end="")
PY

python3 - "$TMP_DIR" "$INITIAL_WORKER_ID" "$RECOVERY_WORKER_ID" <<'PY'
import json
import pathlib
import sys
from datetime import datetime

root = pathlib.Path(sys.argv[1])
load = lambda name: json.loads((root / name).read_text())
run = load("run-final.json")
runtime = load("runtime.json")
calls = load("model-calls.json")
contexts = load("contexts.json")
tools = load("tool-calls.json")
events = load("events.json")
steps = load("run-steps.json")

assert sys.argv[2] != sys.argv[3]
assert run["status"] == "completed"
assert run["result"] == {"content": "Nico ReAct recovered safely after two tool observations."}
assert run["checkpoint_schema_version"] == 2
assert run["checkpoint_revision"] >= 5
assert len(run["checkpoint_hash"]) == 64
assert runtime["execution_mode"] == "react" and runtime["loop_state"] == "completed"
assert runtime["checkpoint_schema_version"] == 2
assert runtime["checkpoint_hash"] == runtime["checkpoint"]["checkpoint_hash"]
assert [item["call_key"] for item in calls] == ["model:1", "model:2", "model:3"]
assert all(item["status"] == "completed" for item in calls)
assert all(
    datetime.fromisoformat(item["ended_at"]) >= datetime.fromisoformat(item["started_at"])
    for item in calls
)
assert [item["version"] for item in contexts] == [1, 2, 3]
assert [item["tool_name"] for item in tools] == ["file.write", "python.execute"]
assert all(item["status"] == "succeeded" and len(item["attempts"]) == 1 for item in tools)
assert len({item["idempotency_key"] for item in tools}) == 2
assert (root / "workspace-proof.txt").read_text() == "written exactly once before worker recovery\n"

tool_steps = [item for item in steps if item["step_type"] == "tool"]
assert len(tool_steps) == 2
assert all(item["context_snapshot_id"] and item["model_call_id"] for item in tool_steps)
assert all(item["status"] == "completed" for item in steps)
context_by_iteration = {item["version"]: item["id"] for item in contexts}
call_by_iteration = {
    int(item["call_key"].removeprefix("model:")): item["id"] for item in calls
}
assert all(
    item["context_snapshot_id"] == context_by_iteration[item["iteration"]]
    and item["model_call_id"] == call_by_iteration[item["iteration"]]
    for item in steps
)
assert int((root / "tool-audit-count.txt").read_text()) >= 4

provider_sequences = [
    item["payload"]["provider_sequence"]
    for item in events
    if "provider_sequence" in item.get("payload", {})
]
assert provider_sequences == list(range(1, len(provider_sequences) + 1))
types = {item["event_type"] for item in events}
assert {"RuntimeToolCallStarted", "RuntimeToolCallCompleted", "ToolCallSucceeded"} <= types

serialized = "\n".join(path.read_text(errors="replace") for path in root.iterdir())
assert "goal-g-fake-token" not in serialized
PY

log "PASS Goal H ReAct crash recovery and replay protection"
