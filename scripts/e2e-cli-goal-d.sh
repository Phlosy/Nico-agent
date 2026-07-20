#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command script
require_command timeout
ensure_python_environment
load_env_file

log "building the Goal D API/Worker baseline"
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
python="$ROOT_DIR/.venv/bin/python"
config_file="$tmp_dir/config.toml"
api_base="http://localhost:${API_PORT:-18000}"

"$ROOT_DIR/scripts/demo.sh" >"$tmp_dir/demo.txt"
tenant_id="$($python -c 'import json; print(json.load(open(".nico/demo-state.json"))["tenant_id"])')"
project_id="$($python -c 'import json; print(json.load(open(".nico/demo-state.json"))["project_id"])')"
agent_id="$($python -c 'import json; print(json.load(open(".nico/demo-state.json"))["agent_id"])')"

"$cli" --config-file "$config_file" --json config set local \
  --api-url "$api_base" --tenant-id "$tenant_id" --actor-id cli-goal-d \
  >"$tmp_dir/config-set.json"
"$cli" --config-file "$config_file" --json config use local >"$tmp_dir/config-use.json"

log "proving attached exec and command-local JSON"
"$cli" --config-file "$config_file" exec "Goal D attached execution" \
  --project "$project_id" --agent "$agent_id" --json >"$tmp_dir/exec-attached.json"

printf '{"prompt":"Goal D detached execution","project_id":"%s","agent_id":"%s","title":"CLI Goal D detached"}\n' \
  "$project_id" "$agent_id" >"$tmp_dir/task-input.json"
"$cli" --config-file "$config_file" exec --input "$tmp_dir/task-input.json" \
  --output "$tmp_dir/result.json" --detach --json >"$tmp_dir/exec-detached.json"
run_id="$($python -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' "$tmp_dir/exec-detached.json")"
conversation_id="$($python -c 'import json,sys; print(json.load(open(sys.argv[1]))["conversation_id"])' "$tmp_dir/exec-detached.json")"

log "reattaching to the detached Run and resuming from an Event cursor"
"$cli" --config-file "$config_file" run watch "$run_id" --json >"$tmp_dir/watch.json"
first_sequence="$($python -c 'import json,sys; print(json.load(open(sys.argv[1]))["events"][0]["sequence"])' "$tmp_dir/watch.json")"
"$cli" --config-file "$config_file" run watch "$run_id" --after "$first_sequence" --json \
  >"$tmp_dir/watch-after.json"

log "capturing non-TTY, colour TTY, and interactive slash inspection views"
"$cli" --config-file "$config_file" --no-color exec "Goal D non TTY" \
  --project "$project_id" --agent "$agent_id" >"$tmp_dir/non-tty.txt"

env -u NO_COLOR TERM=xterm-256color script -qefc \
  "$cli --config-file $config_file exec Goal-D-TTY --project $project_id --agent $agent_id" \
  "$tmp_dir/tty.txt" >"$tmp_dir/tty-stdout.txt"

printf '/help\r/inspect\r/exit\r' | timeout 20 script -qefc \
  "TERM=dumb $cli --config-file $config_file --no-color chat --resume $conversation_id" \
  "$tmp_dir/slash.txt" >"$tmp_dir/slash-stdout.txt"

"$python" - "$tmp_dir" "$first_sequence" <<'PY'
import json
import pathlib
import re
import stat
import sys

root = pathlib.Path(sys.argv[1])
first_sequence = int(sys.argv[2])
load = lambda name: json.loads((root / name).read_text())

attached = load("exec-attached.json")
detached = load("exec-detached.json")
result = load("result.json")
watch = load("watch.json")
resumed = load("watch-after.json")

assert attached["detached"] is False
assert attached["turn"]["status"] == attached["run"]["status"] == "completed"
assert any(event["type"] == "RunCompleted" for event in attached["events"])
assert detached["detached"] is True
assert detached["run_id"] == result["run_id"] == watch["run"]["id"]
assert watch["run"]["status"] == "completed"
assert all(event["sequence"] > first_sequence for event in resumed["events"])
assert stat.S_IMODE((root / "result.json").stat().st_mode) == 0o600

for path in root.glob("*.json"):
    assert "\x1b" not in path.read_text()

non_tty = (root / "non-tty.txt").read_text()
assert "Nico Agent" in non_tty and "Agent" in non_tty and "Runtime" in non_tty
assert "\x1b" not in non_tty

tty = (root / "tty.txt").read_text(errors="replace")
tty_plain = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", tty)
assert "Nico Agent" in tty_plain and "▄██████▄" in tty_plain and "\x1b[" in tty

slash = (root / "slash.txt").read_text(errors="replace")
for marker in (
    "Nico Slash Commands",
    "/retry",
    "Plan Revisions",
    "Run Steps",
    "Tool Calls",
    "Artifacts",
    "Usage",
):
    assert marker in slash, marker
PY

printf 'PASS CLI Goal D exec/detach/watch/slash/Rich/coin-cat run_id=%s\n' "$run_id"
