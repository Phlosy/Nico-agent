#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command script
require_command timeout
ensure_python_environment
load_env_file

log "building the CLI Goal E API/Worker baseline"
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
  --api-url "$api_base" --tenant-id "$tenant_id" --actor-id cli-goal-e \
  >"$tmp_dir/config-set.json"
"$cli" --config-file "$config_file" --json config use local >"$tmp_dir/config-use.json"

"$cli" --config-file "$config_file" --json chat \
  --project "$project_id" --agent "$agent_id" \
  "建立附件测试会话" >"$tmp_dir/first-turn.json"
conversation_id="$($python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["conversation"]["id"])' \
  "$tmp_dir/first-turn.json")"

printf 'blue-gold-white attachment bytes\n' >"$tmp_dir/notes.txt"
log "uploading local bytes through /attach and consuming them in the next Turn"
printf '/attach %s\r请读取附件引用并继续\r/exit\r' "$tmp_dir/notes.txt" | timeout 30 script -qefc \
  "TERM=dumb $cli --config-file $config_file --no-color chat --resume $conversation_id" \
  "$tmp_dir/attach-session.txt" >"$tmp_dir/attach-stdout.txt"

"$cli" --config-file "$config_file" --json conversation history "$conversation_id" \
  >"$tmp_dir/history.json"
artifact_id="$($python -c \
  'import json,sys; rows=json.load(open(sys.argv[1])); print(rows[-1]["artifact_refs"][0]["artifact_id"])' \
  "$tmp_dir/history.json")"

log "compacting durable history through a Worker-owned summary Run"
printf '/compact\r/exit\r' | timeout 30 script -qefc \
  "TERM=dumb $cli --config-file $config_file --no-color chat --resume $conversation_id" \
  "$tmp_dir/compact-session.txt" >"$tmp_dir/compact-stdout.txt"
"$cli" --config-file "$config_file" --json conversation get "$conversation_id" \
  >"$tmp_dir/conversation.json"

download_path="$tmp_dir/downloaded.txt"
log "downloading only the Artifact referenced by this Conversation"
printf '/download %s %s\r/exit\r' "$artifact_id" "$download_path" | timeout 20 script -qefc \
  "TERM=dumb $cli --config-file $config_file --no-color chat --resume $conversation_id" \
  "$tmp_dir/download-session.txt" >"$tmp_dir/download-stdout.txt"

"$python" - "$tmp_dir" "$artifact_id" <<'PY'
import json
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1])
artifact_id = sys.argv[2]
history = json.loads((root / "history.json").read_text())
conversation = json.loads((root / "conversation.json").read_text())
reference = history[-1]["artifact_refs"][0]

assert reference["artifact_id"] == artifact_id
assert reference["name"] == "notes.txt"
assert reference["summary"].startswith("blue-gold-white attachment bytes")
assert conversation["summary"]
assert conversation["summary_through_sequence"] == history[-1]["sequence"]
assert len(conversation["summary_input_hash"]) == 64
assert (root / "downloaded.txt").read_bytes() == b"blue-gold-white attachment bytes\n"
assert stat.S_IMODE((root / "downloaded.txt").stat().st_mode) == 0o600

for name, marker in (
    ("attach-session.txt", "Attachment staged for the next Turn"),
    ("compact-session.txt", "Conversation compacted"),
    ("download-session.txt", "Artifact downloaded"),
):
    text = (root / name).read_text(errors="replace")
    assert marker in text, (name, marker)

for path in root.glob("*.json"):
    assert "\x1b" not in path.read_text()
PY

printf 'PASS CLI Goal E attachment/summary/compact/download artifact_id=%s\n' "$artifact_id"
