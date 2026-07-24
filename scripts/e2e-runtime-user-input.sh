#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command curl
require_command script
ensure_python_environment

tmp_dir="$(mktemp -d)"
fixture_pid=""
cleanup() {
  if [[ -n "$fixture_pid" ]]; then
    kill "$fixture_pid" >/dev/null 2>&1 || true
    wait "$fixture_pid" >/dev/null 2>&1 || true
  fi
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

port="${NICO_USER_INPUT_E2E_PORT:-18777}"
tenant_id="11111111-1111-4111-8111-111111111111"
conversation_id="22222222-2222-4222-8222-222222222222"
base_url="http://127.0.0.1:$port"

"$ROOT_DIR/.venv/bin/python" \
  -m nico_agent.testing.user_input_e2e_server --port "$port" \
  >"$tmp_dir/server.log" 2>&1 &
fixture_pid="$!"

for attempt in {1..50}; do
  if curl -fsS "$base_url/__state" >/dev/null 2>&1; then
    break
  fi
  [[ "$attempt" -eq 50 ]] && die "UserInput PTY fixture did not start"
  sleep 0.1
done

cli_command="env NO_COLOR=1 TERM=xterm-256color $ROOT_DIR/.venv/bin/nico --no-color --api-url $base_url --tenant-id $tenant_id chat --resume $conversation_id"

set +e
timeout 4 script -qefc "$cli_command" "$tmp_dir/first-cli.log" </dev/null >/dev/null
first_status="$?"
set -e
[[ "$first_status" -eq 0 || "$first_status" -eq 124 ]] \
  || die "first CLI detach failed with status $first_status"
rg -q 'Agent question' "$tmp_dir/first-cli.log" \
  || die "first CLI did not restore the pending Agent question"
if rg -q 'authoritative result after one durable answer' "$tmp_dir/first-cli.log"; then
  die "first CLI produced a final response before an answer"
fi

{
  printf 'database\r'
  sleep 2
  printf '/exit\r'
} | timeout 25 script -qefc "$cli_command" "$tmp_dir/restarted-cli.log" >/dev/null

rg -q 'Agent question' "$tmp_dir/restarted-cli.log" \
  || die "restarted CLI did not restore the durable Agent question"
[[ "$(rg -c 'authoritative result after one durable answer' "$tmp_dir/restarted-cli.log")" -eq 1 ]] \
  || die "restarted CLI did not render exactly one authoritative final response"

state="$(curl -fsS "$base_url/__state")"
"$ROOT_DIR/.venv/bin/python" - "$state" <<'PY'
import json
import sys

state = json.loads(sys.argv[1])
assert state["answered"] is True
assert state["answer_count"] == 1
assert state["turn_create_count"] == 0
assert state["final_read_count"] >= 1
PY

if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  mkdir -p "$NICO_EVIDENCE_DIR"
  cp "$tmp_dir/first-cli.log" "$NICO_EVIDENCE_DIR/pty-first-cli.log"
  cp "$tmp_dir/restarted-cli.log" "$NICO_EVIDENCE_DIR/pty-restarted-cli.log"
  printf '%s\n' "$state" >"$NICO_EVIDENCE_DIR/pty-state.json"
fi

log "PASS UserInput PTY restart, answer ownership, resume, and single-final proof"
