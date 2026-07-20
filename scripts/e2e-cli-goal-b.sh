#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

ensure_python_environment
"${COMPOSE[@]}" up --detach postgres redis minio minio-init sandbox-runner api worker
# The integration suite deliberately replays the full migration chain. Refresh
# long-lived API/Worker database connections before proving the CLI path.
"${COMPOSE[@]}" restart api worker >/dev/null
wait_for_service_health api
wait_for_url "http://localhost:18000/api/v1/health/ready" "Nico API"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
cli="$ROOT_DIR/.venv/bin/nico"
config_file="$tmp_dir/config.toml"

demo_output="$($ROOT_DIR/scripts/demo.sh)"
printf '%s\n' "$demo_output" | tee "$tmp_dir/demo-output.txt"

run_id="$(awk '/^Run ID:/ {print $3}' <<<"$demo_output")"
tenant_id="$($ROOT_DIR/.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["tenant_id"])' "$ROOT_DIR/.nico/demo-state.json")"
agent_id="$($ROOT_DIR/.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["agent_id"])' "$ROOT_DIR/.nico/demo-state.json")"

[[ -n "$run_id" && -n "$tenant_id" && -n "$agent_id" ]] \
  || die "could not resolve demo IDs for CLI smoke"

"$cli" --config-file "$config_file" --json config set local \
  --api-url http://localhost:18000 \
  --tenant-id "$tenant_id" \
  --actor-id cli-goal-b >"$tmp_dir/config-set.json"
"$cli" --config-file "$config_file" --json config use local >"$tmp_dir/config-use.json"
[[ "$(stat -c '%a' "$config_file")" == "600" ]] || die "CLI config is not mode 0600"

"$cli" --config-file "$config_file" --json health >"$tmp_dir/health.json"
"$cli" --config-file "$config_file" --json version --server >"$tmp_dir/version.json"
"$cli" --config-file "$config_file" --json doctor >"$tmp_dir/doctor.json"
"$cli" --config-file "$config_file" --json project list >"$tmp_dir/projects.json"
"$cli" --config-file "$config_file" --json agent list >"$tmp_dir/agents.json"
"$cli" --config-file "$config_file" --json agent versions "$agent_id" \
  >"$tmp_dir/agent-versions.json"
"$cli" --config-file "$config_file" --json run get "$run_id" >"$tmp_dir/run.json"
task_id="$($ROOT_DIR/.venv/bin/python -c 'import json,sys; print(json.load(open(sys.argv[1]))["task_id"])' "$tmp_dir/run.json")"
"$cli" --config-file "$config_file" --json task get "$task_id" >"$tmp_dir/task.json"
"$cli" --config-file "$config_file" --json run runtime "$run_id" \
  >"$tmp_dir/runtime.json"
"$cli" --config-file "$config_file" --json run events "$run_id" \
  >"$tmp_dir/events.json"

set +e
"$cli" --config-file "$tmp_dir/unconfigured.toml" --json agent list \
  >"$tmp_dir/missing-tenant.stdout" 2>"$tmp_dir/missing-tenant.json"
missing_tenant_status=$?
set -e
[[ "$missing_tenant_status" -eq 2 ]] || die "missing tenant did not return exit code 2"

NO_COLOR=1 "$cli" --config-file "$config_file" health >"$tmp_dir/no-color.txt"
if grep -q $'\033' "$tmp_dir/no-color.txt"; then
  die "NO_COLOR output contains ANSI control sequences"
fi

"$ROOT_DIR/.venv/bin/python" - "$tmp_dir" "$run_id" "$tenant_id" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
run_id = sys.argv[2]
tenant_id = sys.argv[3]

def read(name):
    return json.loads((root / name).read_text())

assert read("health.json")["status"] == "ready"
assert read("version.json") == {
    "client": "0.2.0",
    "server": "0.2.0",
    "service": "Nico Agent Platform",
}
doctor = read("doctor.json")
assert all(item["status"] == "pass" for item in doctor)
assert read("config-set.json")["tenant_id"] == tenant_id
assert read("projects.json")
assert read("agents.json")
assert read("agent-versions.json")[0]["status"] == "published"
assert read("run.json")["id"] == run_id
assert read("run.json")["status"] == "completed"
assert read("task.json")["id"] == read("run.json")["task_id"]
assert read("runtime.json")["run_id"] == run_id
assert read("runtime.json")["status"] == "completed"
events = read("events.json")
assert events and events[-1]["event_type"] == "RunCompleted"
assert read("missing-tenant.json")["error"]["code"] == "TENANT_CONTEXT_REQUIRED"
assert not (root / "missing-tenant.stdout").read_text()
PY

if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
  mkdir -p "$NICO_EVIDENCE_DIR/cli-e2e"
  cp "$tmp_dir"/*.json "$tmp_dir"/*.txt "$NICO_EVIDENCE_DIR/cli-e2e/"
fi

printf 'PASS CLI Goal B real API smoke run_id=%s tenant_id=%s\n' "$run_id" "$tenant_id"
