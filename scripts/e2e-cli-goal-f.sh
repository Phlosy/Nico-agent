#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command curl
require_command script
require_command timeout
ensure_python_environment
load_env_file

export NICO_MODEL_SECRET_GOAL_G="${NICO_MODEL_SECRET_GOAL_G:-goal-g-fake-token}"
export NICO_MODEL_TRUSTED_PRIVATE_HOSTS='["fake-model"]'
export NICO_MODEL_ALLOW_HTTP_TRUSTED_HOSTS=true
export NICO_MODEL_ENDPOINT_WRITES_ENABLED=true
export NICO_WORKER_LEASE_SECONDS=5
export NICO_WORKER_HEARTBEAT_SECONDS=1
export NICO_WORKER_POLL_INTERVAL_SECONDS=0.2
export NICO_TOOL_APPROVAL_REQUIRED_RISKS='["medium","high"]'
export NICO_TOOL_APPROVAL_TTL_SECONDS=900

log "starting the CLI Goal F approval stack"
"${COMPOSE[@]}" --profile goal-h up --detach --build --force-recreate \
  fake-model api worker
wait_for_service_health fake-model
wait_for_service_health api

api_base="http://localhost:${API_PORT:-18000}"
tmp_dir="$(mktemp -d)"
run_key="$(date -u +%Y%m%d%H%M%S)-$$"
config_file="$tmp_dir/config.toml"
cli="$ROOT_DIR/.venv/bin/nico"
python="$ROOT_DIR/.venv/bin/python"
approval_pid=""
approval_fifo=""

archive_and_cleanup() {
  if [[ -n "$approval_pid" ]] && kill -0 "$approval_pid" 2>/dev/null; then
    kill "$approval_pid" 2>/dev/null || true
  fi
  if [[ -n "$approval_fifo" && -p "$approval_fifo" ]]; then
    unlink "$approval_fifo"
  fi
  if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
    mkdir -p "$NICO_EVIDENCE_DIR/cli-e2e"
    cp "$tmp_dir"/* "$NICO_EVIDENCE_DIR/cli-e2e/" 2>/dev/null || true
  fi
  rm -rf "$tmp_dir"
}
trap archive_and_cleanup EXIT

request() {
  local method="$1" path="$2" body="$3" output="$4"
  shift 4
  local args=(--fail-with-body --silent --show-error --request "$method")
  if [[ -n "$body" ]]; then
    args+=(--header "Content-Type: application/json" --data "$body")
  fi
  curl "${args[@]}" "$@" --output "$output" "$api_base$path"
}

json_path() {
  "$python" - "$@" <<'PY'
import json
import sys

value = json.load(open(sys.argv[1]))
for key in sys.argv[2:]:
    value = value[int(key)] if isinstance(value, list) else value[key]
print(value)
PY
}

wait_for_transcript() {
  local transcript="$1" pattern="$2" expected="$3" description="$4"
  for attempt in $(seq 1 180); do
    local count=0
    if [[ -f "$transcript" ]]; then
      count="$(grep -aocF "$pattern" "$transcript" || true)"
    fi
    if [[ "$count" -ge "$expected" ]]; then
      return
    fi
    [[ "$attempt" -lt 180 ]] || die "timed out waiting for $description"
    sleep 0.5
  done
}

policy='{"allow":["file.write@1.0.0","python.execute@1.0.0"],"permissions":["filesystem.write","code.python.execute"],"tools":{"python.execute@1.0.0":{"wall_time_seconds":5,"memory_bytes":67108864,"pids_limit":8,"output_bytes":4096}}}'

request POST /api/v1/tenants/bootstrap \
  "{\"name\":\"CLI Goal F $run_key\",\"slug\":\"cli-goal-f-$run_key\",\"settings\":{\"tool_policy\":$policy}}" \
  "$tmp_dir/tenant.json"
tenant_id="$(json_path "$tmp_dir/tenant.json" id)"
headers=(--header "X-Tenant-ID: $tenant_id" --header "X-Actor-ID: cli-goal-f")

request POST /api/v1/model-endpoints \
  '{"stable_key":"cli-goal-f","display_name":"CLI Goal F fake model","base_url":"http://fake-model:8100/v1","credential_ref":"env:NICO_MODEL_SECRET_GOAL_G","allowed_models":["goal-h-fake"],"capabilities":{"streaming":true,"tools":true}}' \
  "$tmp_dir/model-endpoint.json" "${headers[@]}"
endpoint_id="$(json_path "$tmp_dir/model-endpoint.json" id)"
request POST /api/v1/projects "{\"name\":\"CLI approvals $run_key\"}" \
  "$tmp_dir/project.json" "${headers[@]}"
project_id="$(json_path "$tmp_dir/project.json" id)"
request POST /api/v1/agents \
  "{\"name\":\"cli-goal-f-$run_key\",\"display_name\":\"CLI Approval Agent\"}" \
  "$tmp_dir/agent.json" "${headers[@]}"
agent_id="$(json_path "$tmp_dir/agent.json" id)"

version_body="$($python - "$endpoint_id" "$policy" <<'PY'
import json
import sys

print(json.dumps({
    "role": "approval-verifier",
    "mandate": "Use authorized platform tools and wait for every required approval.",
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
request POST "/api/v1/agents/$agent_id/versions" "$version_body" \
  "$tmp_dir/agent-version.json" "${headers[@]}"
version_id="$(json_path "$tmp_dir/agent-version.json" id)"
request POST "/api/v1/agents/$agent_id/versions/$version_id/publish" \
  '{"expected_revision":1}' "$tmp_dir/agent-ready.json" "${headers[@]}"

"$cli" --config-file "$config_file" --json config set local \
  --api-url "$api_base" --tenant-id "$tenant_id" --actor-id cli-goal-f \
  >"$tmp_dir/config-set.json"
"$cli" --config-file "$config_file" --json config use local \
  >"$tmp_dir/config-use.json"

log "starting non-interactively, disconnecting at approval, then resuming in a PTY"
"$cli" --config-file "$config_file" --json chat \
  --project "$project_id" --agent "$agent_id" \
  "执行需要审批的两个工具并给出结论" >"$tmp_dir/reconnect-start.json"
conversation_id="$(json_path "$tmp_dir/reconnect-start.json" conversation id)"
approval_fifo="$tmp_dir/approval-input"
mkfifo "$approval_fifo"
timeout 90 script -qefc \
  "TERM=xterm-256color $cli --config-file $config_file --no-color chat --resume $conversation_id" \
  "$tmp_dir/approval-session.txt" <"$approval_fifo" >"$tmp_dir/approval-stdout.txt" &
approval_pid="$!"
exec 3>"$approval_fifo"
wait_for_transcript \
  "$tmp_dir/approval-session.txt" "Sensitive tool approval" 1 "the first approval prompt"
printf '1\r' >&3
wait_for_transcript \
  "$tmp_dir/approval-session.txt" "Sensitive tool approval" 2 "the second approval prompt"
printf '2\r' >&3
wait_for_transcript \
  "$tmp_dir/approval-session.txt" \
  "Nico ReAct recovered safely after two tool observations." 1 "the final answer"
printf '/exit\r' >&3
exec 3>&-
wait "$approval_pid"
approval_pid=""
unlink "$approval_fifo"
approval_fifo=""

request GET "/api/v1/conversations/$conversation_id/turns?limit=10" '' \
  "$tmp_dir/turns.json" "${headers[@]}"
run_id="$(json_path "$tmp_dir/turns.json" 0 run_id)"
request GET "/api/v1/runs/$run_id" '' "$tmp_dir/run.json" "${headers[@]}"
request GET "/api/v1/runs/$run_id/tool-calls" '' "$tmp_dir/tool-calls.json" \
  "${headers[@]}"
request GET "/api/v1/runs/$run_id/events" '' "$tmp_dir/events.json" "${headers[@]}"
request GET "/api/v1/tool-approval-requests?run_id=$run_id" '' \
  "$tmp_dir/approvals.json" "${headers[@]}"
request GET '/api/v1/audit?limit=500' '' "$tmp_dir/audit.json" "${headers[@]}"

"$python" - "$tmp_dir" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
load = lambda name: json.loads((root / name).read_text())
run = load("run.json")
start = load("reconnect-start.json")
turns = load("turns.json")
calls = load("tool-calls.json")
events = load("events.json")
approvals = load("approvals.json")
audit = load("audit.json")

assert run["status"] == "completed"
assert start["turn"]["status"] == "waiting_for_approval"
assert start["approval_required"]["status"] == "requested"
assert turns[0]["status"] == "completed"
assert [item["tool_name"] for item in calls] == ["file.write", "python.execute"]
assert all(item["status"] == "succeeded" and len(item["attempts"]) == 1 for item in calls)
assert all(item["status"] == "approved" for item in approvals)
assert {item["tool_name"]: item["allowed_scope"] for item in approvals} == {
    "file.write": "once",
    "python.execute": "run",
}
assert all(item["decided_by"] == "cli-goal-f" for item in approvals)

event_types = [item["event_type"] for item in events]
assert event_types.count("ApprovalRequested") == 2
assert event_types.count("ToolApprovalApproved") == 2
approval_ids = {item["id"] for item in approvals}
actions = {
    item["action"]
    for item in audit
    if item["resource_type"] == "tool_approval_request"
    and item["resource_id"] in approval_ids
}
assert {"tool.approval.request", "tool.approval.approved"} <= actions

terminal = (root / "approval-session.txt").read_text(errors="replace")
assert terminal.count("Sensitive tool approval") >= 2
assert "Allow once" in terminal and "Allow for this Run" in terminal
assert "Nico ReAct recovered safely after two tool observations." in terminal
serialized = "\n".join(path.read_text(errors="replace") for path in root.iterdir())
assert "goal-g-fake-token" not in serialized
PY

printf 'PASS CLI Goal F durable interactive approvals run_id=%s\n' "$run_id"
