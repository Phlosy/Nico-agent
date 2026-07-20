#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command curl
require_command timeout
ensure_python_environment
load_env_file

log "building Goal C API and Worker and applying the Conversation migration"
"${COMPOSE[@]}" up --detach --build postgres redis minio minio-init sandbox-runner api worker
wait_for_service_health api
wait_for_url "http://localhost:${API_PORT:-18000}/api/v1/health/ready" "Nico API"

tmp_dir="$(mktemp -d)"
archive_and_cleanup() {
  if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
    mkdir -p "$NICO_EVIDENCE_DIR/cli-e2e"
    cp "$tmp_dir"/*.json "$tmp_dir"/*.txt "$NICO_EVIDENCE_DIR/cli-e2e/" 2>/dev/null || true
  fi
  rm -rf "$tmp_dir"
}
trap archive_and_cleanup EXIT

cli="$ROOT_DIR/.venv/bin/nico"
config_file="$tmp_dir/config.toml"
api_base="http://localhost:${API_PORT:-18000}"
run_key="$(date -u +%Y%m%d%H%M%S)-$$"

demo_output="$($ROOT_DIR/scripts/demo.sh)"
printf '%s\n' "$demo_output" >"$tmp_dir/demo-output.txt"
tenant_id="$($ROOT_DIR/.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["tenant_id"])' "$ROOT_DIR/.nico/demo-state.json")"
project_id="$($ROOT_DIR/.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["project_id"])' "$ROOT_DIR/.nico/demo-state.json")"
agent_id="$($ROOT_DIR/.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["agent_id"])' "$ROOT_DIR/.nico/demo-state.json")"

"$cli" --config-file "$config_file" --json config set local \
  --api-url "$api_base" \
  --tenant-id "$tenant_id" \
  --actor-id cli-goal-c >"$tmp_dir/config-set.json"
"$cli" --config-file "$config_file" --json config use local >"$tmp_dir/config-use.json"
[[ "$(stat -c '%a' "$config_file")" == "600" ]] || die "CLI config is not mode 0600"

log "running a new chat and a second turn through --continue"
"$cli" --config-file "$config_file" --json chat \
  "First durable conversation turn" \
  --project "$project_id" \
  --agent "$agent_id" \
  --title "CLI Goal C $run_key" >"$tmp_dir/chat-first.json"
conversation_id="$($ROOT_DIR/.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["conversation"]["id"])' "$tmp_dir/chat-first.json")"

"$cli" --config-file "$config_file" --json chat \
  "Second durable conversation turn" \
  --continue \
  --project "$project_id" \
  --agent "$agent_id" >"$tmp_dir/chat-second.json"
"$cli" --config-file "$config_file" --json chat \
  --resume "$conversation_id" --read-only >"$tmp_dir/chat-resume.json"
"$cli" --config-file "$config_file" --json conversation history \
  "$conversation_id" >"$tmp_dir/history.json"
"$cli" --config-file "$config_file" --json conversation list \
  --project "$project_id" --agent "$agent_id" --status active >"$tmp_dir/conversations.json"

request() {
  local method="$1" path="$2" body="$3" output="$4"
  curl --fail-with-body --silent --show-error \
    --request "$method" \
    --header "Content-Type: application/json" \
    --header "X-Tenant-ID: $tenant_id" \
    --header "X-Actor-ID: cli-goal-c" \
    --data "$body" \
    --output "$output" \
    "$api_base$path"
}

log "proving Ctrl+C requests authoritative server-side cancellation"
request POST /api/v1/agents \
  "{\"name\":\"cli-goal-c-slow-$run_key\",\"display_name\":\"Slow cancellation Agent\"}" \
  "$tmp_dir/slow-agent.json"
slow_agent_id="$($ROOT_DIR/.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$tmp_dir/slow-agent.json")"
request POST "/api/v1/agents/$slow_agent_id/versions" \
  '{"role":"slow-chat","mandate":"Exercise cancellation","runtime_provider":"mock","run_config":{"mock":{"steps":["wait-one","wait-two","wait-three"],"delay_seconds":5,"output":{"answer":"should be cancelled"}}}}' \
  "$tmp_dir/slow-version.json"
slow_version_id="$($ROOT_DIR/.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$tmp_dir/slow-version.json")"
request POST "/api/v1/agents/$slow_agent_id/versions/$slow_version_id/publish" \
  '{"expected_revision":1}' "$tmp_dir/slow-published.json"

set +e
timeout --preserve-status --signal=INT --kill-after=8 2 \
  "$cli" --config-file "$config_file" --json chat \
  "Cancel this active Run" \
  --project "$project_id" \
  --agent "$slow_agent_id" \
  --title "CLI Goal C cancellation $run_key" \
  >"$tmp_dir/chat-cancelled.json" 2>"$tmp_dir/chat-cancelled.stderr.txt"
cancel_status=$?
set -e
[[ "$cancel_status" -eq 0 ]] || die "Ctrl+C chat exited with status $cancel_status"

"$ROOT_DIR/.venv/bin/python" - "$tmp_dir" "$conversation_id" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
conversation_id = sys.argv[2]
load = lambda name: json.loads((root / name).read_text())

first = load("chat-first.json")
second = load("chat-second.json")
resumed = load("chat-resume.json")
history = load("history.json")
conversations = load("conversations.json")
cancelled = load("chat-cancelled.json")

assert first["conversation"]["id"] == conversation_id
assert second["conversation"]["id"] == conversation_id
assert first["turn"]["status"] == first["turn"]["run_status"] == "completed"
assert second["turn"]["status"] == second["turn"]["run_status"] == "completed"
assert first["turn"]["run_id"] != second["turn"]["run_id"]
assert any(event["type"] == "RunCompleted" for event in first["events"])
assert any(event["type"] == "RunCompleted" for event in second["events"])
assert resumed["conversation"]["id"] == conversation_id
assert [turn["sequence"] for turn in resumed["turns"]] == [1, 2]
assert [turn["sequence"] for turn in history] == [1, 2]
assert conversations[0]["id"] == conversation_id
assert cancelled["turn"]["status"] == "cancelled"
assert cancelled["turn"]["run_status"] == "cancelled"
assert not (root / "chat-cancelled.stderr.txt").read_text()

for path in root.glob("*.json"):
    assert "\x1b" not in path.read_text()
PY

printf 'PASS CLI Goal C conversation chat/resume/continue/history/SSE/Ctrl+C conversation_id=%s\n' \
  "$conversation_id"

