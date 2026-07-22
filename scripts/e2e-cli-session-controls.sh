#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command curl
require_command script
require_command timeout
ensure_python_environment
load_env_file

export NICO_WORKER_POLL_INTERVAL_SECONDS=0.2
export NICO_WORKER_LEASE_SECONDS=8
export NICO_WORKER_HEARTBEAT_SECONDS=2

log "starting the session-controls API and two Workers"
"${COMPOSE[@]}" up --detach --build --force-recreate \
  postgres redis minio minio-init sandbox-runner api worker
wait_for_service_health api
wait_for_service_health worker

tmp_dir="$(mktemp -d)"
run_key="$(date -u +%Y%m%d%H%M%S)-$$"
secondary_worker="nico-session-controls-worker-$$"
"${COMPOSE[@]}" run --detach --no-deps --name "$secondary_worker" \
  -e "NICO_WORKER_ID=session-controls-secondary-$$" worker >/dev/null

archive_and_cleanup() {
  docker rm --force "$secondary_worker" >/dev/null 2>&1 || true
  if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
    mkdir -p "$NICO_EVIDENCE_DIR/cli-session-controls"
    cp "$tmp_dir"/* "$NICO_EVIDENCE_DIR/cli-session-controls/" 2>/dev/null || true
  fi
  rm -rf "$tmp_dir"
}
trap archive_and_cleanup EXIT

api_base="http://localhost:${API_PORT:-18000}"
cli="$ROOT_DIR/.venv/bin/nico"
python="$ROOT_DIR/.venv/bin/python"
config_file="$tmp_dir/config.toml"

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

request POST /api/v1/tenants/bootstrap \
  "{\"name\":\"Session controls $run_key\",\"slug\":\"session-controls-$run_key\"}" \
  "$tmp_dir/tenant.json"
tenant_id="$(json_path "$tmp_dir/tenant.json" id)"
headers=(--header "X-Tenant-ID: $tenant_id" --header "X-Actor-ID: session-controls")

request POST /api/v1/projects "{\"name\":\"Session controls $run_key\"}" \
  "$tmp_dir/project.json" "${headers[@]}"
project_id="$(json_path "$tmp_dir/project.json" id)"

create_agent_version() {
  local prefix="$1" failure="$2" delay="$3"
  request POST /api/v1/agents \
    "{\"name\":\"$prefix-$run_key\",\"display_name\":\"Session Controls $prefix\"}" \
    "$tmp_dir/$prefix-agent.json" "${headers[@]}"
  local agent_id
  agent_id="$(json_path "$tmp_dir/$prefix-agent.json" id)"
  local version_body
  version_body="$($python - "$delay" "$failure" <<'PY'
import json
import sys

delay = float(sys.argv[1])
failure = sys.argv[2] == "true"
print(json.dumps({
    "role": "session-controls-verifier",
    "mandate": "Exercise durable chat queue controls.",
    "runtime_provider": "mock",
    "execution_mode": "direct",
    "model_name": "session-controls-mock",
    "run_config": {
        "mock": {
            "steps": ["PRIVATE_MODEL_DELTA_CANARY", "hold", "finalize"],
            "delay_seconds": delay,
            "fail": failure,
            "error_code": "SESSION_CONTROLS_EXPECTED_FAILURE",
            "output": {"answer": "Session controls answer"},
        }
    },
}, separators=(",", ":")))
PY
)"
  request POST "/api/v1/agents/$agent_id/versions" "$version_body" \
    "$tmp_dir/$prefix-version.json" "${headers[@]}"
  local version_id
  version_id="$(json_path "$tmp_dir/$prefix-version.json" id)"
  request POST "/api/v1/agents/$agent_id/versions/$version_id/publish" \
    '{"expected_revision":1}' "$tmp_dir/$prefix-ready.json" "${headers[@]}"
}

create_agent_version queue false 4
queue_agent_id="$(json_path "$tmp_dir/queue-agent.json" id)"
create_agent_version failure true 1
failure_agent_id="$(json_path "$tmp_dir/failure-agent.json" id)"

"$cli" --config-file "$config_file" --json config set local \
  --api-url "$api_base" --tenant-id "$tenant_id" --actor-id session-controls \
  >"$tmp_dir/config-set.json"
"$cli" --config-file "$config_file" --json config use local \
  >"$tmp_dir/config-use.json"

log "queuing two messages and changing next-Run permissions while the head is active"
{
  sleep 2
  printf '\033[30;1R'
  sleep 0.2
  printf 'first durable message\r'
  sleep 2
  printf '\033[30;1R'
  sleep 0.2
  printf 'second durable message\r'
  sleep 0.5
  printf '\033[30;1R'
  sleep 0.2
  printf '/permissions auto-medium\r'
  sleep 0.5
  printf '\033[30;1R'
  sleep 0.2
  printf 'y\r'
  sleep 0.8
  printf '\033[30;1R'
  sleep 0.2
  printf '/permissions auto-all\r'
  sleep 0.5
  printf '\033[30;1R'
  sleep 0.2
  printf 'y\r'
  sleep 0.8
  printf '\033[30;1R'
  sleep 0.2
  printf 'third durable message\r'
  sleep 0.8
  printf '\033[30;1R'
  sleep 0.2
  printf '/status\r'
  sleep 0.8
  printf '\033[30;1R'
  sleep 0.2
  printf '/exit\r'
} | timeout 45 script -qefc \
  "stty rows 30 cols 100 && TERM=xterm-256color $cli --config-file $config_file --no-color chat --project $project_id --agent $queue_agent_id --title 'Session queue $run_key'" \
  "$tmp_dir/queue-session.txt" >"$tmp_dir/queue-stdout.txt"

request GET "/api/v1/conversations?project_id=$project_id&agent_id=$queue_agent_id&status=active&limit=10" \
  '' "$tmp_dir/queue-conversations.json" "${headers[@]}"
queue_conversation_id="$(json_path "$tmp_dir/queue-conversations.json" 0 id)"
request GET "/api/v1/conversations/$queue_conversation_id/queue" '' \
  "$tmp_dir/queue-during.json" "${headers[@]}"

"$python" - "$tmp_dir/queue-during.json" <<'PY'
import json
import sys

queue = json.load(open(sys.argv[1]))
assert queue["state"] == "active"
assert queue["active_turn"]["sequence"] == 1
assert [turn["sequence"] for turn in queue["queued_turns"]] == [2, 3]
assert queue["queued_count"] == 2
PY

log "waiting for the durable FIFO to complete under two Workers"
for attempt in $(seq 1 120); do
  request GET "/api/v1/conversations/$queue_conversation_id/turns?limit=10" '' \
    "$tmp_dir/queue-turns.json" "${headers[@]}"
  if "$python" - "$tmp_dir/queue-turns.json" <<'PY'
import json
import sys

turns = json.load(open(sys.argv[1]))
raise SystemExit(0 if len(turns) == 3 and all(t["run_status"] == "completed" for t in turns) else 1)
PY
  then
    break
  fi
  [[ "$attempt" -lt 120 ]] || die "queued Conversation did not complete"
  sleep 0.5
done

request GET "/api/v1/conversations/$queue_conversation_id" '' \
  "$tmp_dir/queue-conversation.json" "${headers[@]}"
request GET "/api/v1/conversations/$queue_conversation_id/queue" '' \
  "$tmp_dir/queue-final.json" "${headers[@]}"
for index in 0 1 2; do
  run_id="$(json_path "$tmp_dir/queue-turns.json" "$index" run_id)"
  request GET "/api/v1/runs/$run_id" '' "$tmp_dir/queue-run-$index.json" "${headers[@]}"
  request GET "/api/v1/runs/$run_id/runtime" '' \
    "$tmp_dir/queue-runtime-$index.json" "${headers[@]}"
done

log "proving failure pause, retry and explicit queue resume survive CLI detach"
{
  printf 'failure head\r'
  sleep 0.5
  printf 'held successor\r'
  printf '/exit\r'
} | timeout 30 script -qefc \
  "TERM=dumb $cli --config-file $config_file --no-color chat --project $project_id --agent $failure_agent_id --title 'Session failure $run_key'" \
  "$tmp_dir/failure-session.txt" >"$tmp_dir/failure-stdout.txt"
request GET "/api/v1/conversations?project_id=$project_id&agent_id=$failure_agent_id&status=active&limit=10" \
  '' "$tmp_dir/failure-conversations.json" "${headers[@]}"
failure_conversation_id="$(json_path "$tmp_dir/failure-conversations.json" 0 id)"

for attempt in $(seq 1 60); do
  request GET "/api/v1/conversations/$failure_conversation_id/queue" '' \
    "$tmp_dir/failure-paused.json" "${headers[@]}"
  if "$python" - "$tmp_dir/failure-paused.json" <<'PY'
import json
import sys

queue = json.load(open(sys.argv[1]))
raise SystemExit(0 if queue["state"] == "paused" and queue["queued_count"] == 1 else 1)
PY
  then
    break
  fi
  [[ "$attempt" -lt 60 ]] || die "failed Conversation did not pause"
  sleep 0.5
done

printf '/retry\r/exit\r' | timeout 30 script -qefc \
  "TERM=dumb $cli --config-file $config_file --no-color chat --resume $failure_conversation_id" \
  "$tmp_dir/failure-retry.txt" >"$tmp_dir/failure-retry-stdout.txt"
for attempt in $(seq 1 60); do
  request GET "/api/v1/conversations/$failure_conversation_id/queue" '' \
    "$tmp_dir/failure-retry-terminal.json" "${headers[@]}"
  if "$python" - "$tmp_dir/failure-retry-terminal.json" <<'PY'
import json
import sys

queue = json.load(open(sys.argv[1]))
pause_turn = queue.get("pause_turn") or {}
terminal = pause_turn.get("run_status") in {"failed", "cancelled", "timed_out"}
raise SystemExit(0 if queue["state"] == "paused" and queue["queued_count"] == 1 and terminal else 1)
PY
  then
    break
  fi
  [[ "$attempt" -lt 60 ]] || die "retried pause Turn did not reach a terminal state"
  sleep 0.5
done
printf '/queue resume\r/exit\r' | timeout 30 script -qefc \
  "TERM=dumb $cli --config-file $config_file --no-color chat --resume $failure_conversation_id" \
  "$tmp_dir/failure-resume.txt" >"$tmp_dir/failure-resume-stdout.txt"

for attempt in $(seq 1 60); do
  request GET "/api/v1/conversations/$failure_conversation_id/queue" '' \
    "$tmp_dir/failure-final.json" "${headers[@]}"
  if "$python" - "$tmp_dir/failure-final.json" <<'PY'
import json
import sys

queue = json.load(open(sys.argv[1]))
raise SystemExit(0 if queue["state"] == "paused" and queue["queued_count"] == 0 else 1)
PY
  then
    break
  fi
  [[ "$attempt" -lt 60 ]] || die "resumed successor did not reach its expected failure"
  sleep 0.5
done

request GET '/api/v1/audit?limit=500' '' "$tmp_dir/audit.json" "${headers[@]}"

"$python" - "$tmp_dir" <<'PY'
import json
import pathlib
import re
import sys
from datetime import datetime

root = pathlib.Path(sys.argv[1])
load = lambda name: json.loads((root / name).read_text())
turns = load("queue-turns.json")
conversation = load("queue-conversation.json")
queue = load("queue-final.json")
runs = [load(f"queue-run-{index}.json") for index in range(3)]
runtimes = [load(f"queue-runtime-{index}.json") for index in range(3)]
failure_paused = load("failure-paused.json")
failure_final = load("failure-final.json")

assert [turn["sequence"] for turn in turns] == [1, 2, 3]
assert all(turn["assistant_output"]["answer"] == "Session controls answer" for turn in turns)
assert conversation["approval_mode"] == "auto-all"
assert queue["state"] == "active" and queue["queued_count"] == 0
assert queue["active_turn"] is None and queue["head_turn"] is None

timestamps = [(datetime.fromisoformat(run["started_at"]), datetime.fromisoformat(run["ended_at"])) for run in runs]
assert timestamps[0][1] <= timestamps[1][0]
assert timestamps[1][1] <= timestamps[2][0]
assert runtimes[0]["execution_manifest"]["tool_approval_policy"]["mode"] == "ask"
assert [runtime["execution_manifest"]["tool_approval_policy"]["mode"] for runtime in runtimes[1:]] == ["auto-all", "auto-all"]
second_context = runtimes[1]["execution_manifest"]["conversation_context"]
third_context = runtimes[2]["execution_manifest"]["conversation_context"]
assert turns[0]["id"] in second_context["selected_turn_ids"]
assert {turns[0]["id"], turns[1]["id"]} <= set(third_context["selected_turn_ids"])

assert failure_paused["pause_reason"] == "run_failed"
assert failure_paused["queued_count"] == 1
assert failure_final["pause_reason"] == "run_failed"
assert failure_final["queued_count"] == 0

terminal = (root / "queue-session.txt").read_text(errors="replace")
terminal = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", terminal).replace("\r", "")
assert "session-controls-mock" in terminal
assert "ask → auto-all" in terminal or "a→aa" in terminal
assert "queued 2" in terminal or "q:2" in terminal
assert "PRIVATE_MODEL_DELTA_CANARY" not in terminal
assert "RuntimeModelOutputDelta" not in terminal
assert "RuntimeSessionCreated" not in terminal

serialized = "\n".join(path.read_text(errors="replace") for path in root.glob("*.txt"))
assert "PRIVATE_MODEL_DELTA_CANARY" not in serialized
PY

printf 'PASS CLI session controls queue=%s paused=%s\n' \
  "$queue_conversation_id" "$failure_conversation_id"
