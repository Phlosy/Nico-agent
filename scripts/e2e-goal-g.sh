#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command curl
require_command python3
load_env_file

export NICO_MODEL_SECRET_GOAL_G="${NICO_MODEL_SECRET_GOAL_G:-goal-g-fake-token}"
export NICO_MODEL_TRUSTED_PRIVATE_HOSTS='["fake-model"]'
export NICO_MODEL_ALLOW_HTTP_TRUSTED_HOSTS=true
export NICO_MODEL_ENDPOINT_WRITES_ENABLED=true

log "starting the hermetic Goal G model, API and native worker"
"${COMPOSE[@]}" --profile goal-g up --detach --build fake-model api worker
wait_for_service_health fake-model
wait_for_service_health api

API_BASE="http://localhost:${API_PORT:-18000}"
TMP_DIR="$(mktemp -d)"
RUN_KEY="$(date -u +%Y%m%d%H%M%S)-$$"

archive_and_cleanup() {
  if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
    mkdir -p "$NICO_EVIDENCE_DIR/e2e-goal-g"
    cp "$TMP_DIR"/* "$NICO_EVIDENCE_DIR/e2e-goal-g/" 2>/dev/null || true
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

log "creating a default nico_native Run through the public API"
request POST /api/v1/tenants/bootstrap \
  "{\"name\":\"Goal G E2E $RUN_KEY\",\"slug\":\"goal-g-$RUN_KEY\"}" \
  "$TMP_DIR/tenant.json"
TENANT_ID="$(json_path "$TMP_DIR/tenant.json" id)"
HEADERS=(--header "X-Tenant-ID: $TENANT_ID" --header "X-Actor-ID: goal-g-verifier")

request POST /api/v1/model-endpoints \
  "{\"stable_key\":\"goal-g-model\",\"display_name\":\"Goal G fake model\",\"base_url\":\"http://fake-model:8100/v1\",\"credential_ref\":\"env:NICO_MODEL_SECRET_GOAL_G\",\"allowed_models\":[\"goal-g-fake\"],\"capabilities\":{\"streaming\":true}}" \
  "$TMP_DIR/model-endpoint.json" "${HEADERS[@]}"
ENDPOINT_ID="$(json_path "$TMP_DIR/model-endpoint.json" id)"

request POST /api/v1/projects \
  "{\"name\":\"Native Project $RUN_KEY\"}" \
  "$TMP_DIR/project.json" "${HEADERS[@]}"
PROJECT_ID="$(json_path "$TMP_DIR/project.json" id)"
request POST /api/v1/agents \
  "{\"name\":\"goal-g-agent-$RUN_KEY\",\"display_name\":\"Goal G Native Agent\"}" \
  "$TMP_DIR/agent.json" "${HEADERS[@]}"
AGENT_ID="$(json_path "$TMP_DIR/agent.json" id)"

VERSION_BODY="$(python3 - "$ENDPOINT_ID" <<'PY'
import json
import sys

print(json.dumps({
    "role": "native-acceptance",
    "mandate": "Return a concise proof that the Nico native runtime executed.",
    "boundaries": ["Do not expose credentials", "Direct mode cannot call tools"],
    "model_endpoint_id": sys.argv[1],
    "model_name": "goal-g-fake",
    "model_config": {"temperature": 0, "max_output_tokens": 64},
}, separators=(",", ":")))
PY
)"
request POST "/api/v1/agents/$AGENT_ID/versions" "$VERSION_BODY" \
  "$TMP_DIR/agent-version.json" "${HEADERS[@]}"
VERSION_ID="$(json_path "$TMP_DIR/agent-version.json" id)"
request POST "/api/v1/agents/$AGENT_ID/versions/$VERSION_ID/publish" \
  '{"expected_revision":1}' "$TMP_DIR/agent-ready.json" "${HEADERS[@]}"

request POST /api/v1/tasks \
  "{\"project_id\":\"$PROJECT_ID\",\"title\":\"Prove native direct execution\",\"input\":{\"run_key\":\"$RUN_KEY\"},\"acceptance\":{\"non_empty\":true},\"assignee_agent_id\":\"$AGENT_ID\",\"priority\":100}" \
  "$TMP_DIR/task.json" "${HEADERS[@]}"
TASK_ID="$(json_path "$TMP_DIR/task.json" id)"
request POST "/api/v1/tasks/$TASK_ID/runs" \
  '{"max_steps":4,"token_budget":2000,"timeout_seconds":60}' \
  "$TMP_DIR/run.json" "${HEADERS[@]}"
RUN_ID="$(json_path "$TMP_DIR/run.json" id)"

log "waiting for native Run $RUN_ID"
for attempt in {1..120}; do
  request GET "/api/v1/runs/$RUN_ID" '' "$TMP_DIR/run-final.json" "${HEADERS[@]}"
  run_status="$(json_path "$TMP_DIR/run-final.json" status)"
  [[ "$run_status" == "completed" ]] && break
  if [[ "$run_status" =~ ^(failed|cancelled|timed_out)$ ]]; then
    cat "$TMP_DIR/run-final.json" >&2
    die "Goal G native Run entered $run_status"
  fi
  [[ "$attempt" -eq 120 ]] && die "Goal G native Run did not complete"
  sleep 0.5
done

request GET "/api/v1/runs/$RUN_ID/runtime" '' "$TMP_DIR/runtime.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/model-calls" '' "$TMP_DIR/model-calls.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/contexts" '' "$TMP_DIR/contexts.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/events" '' "$TMP_DIR/events.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/trajectory" '' "$TMP_DIR/trajectory.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/events/stream" '' "$TMP_DIR/events.sse" \
  "${HEADERS[@]}" --header "Last-Event-ID: 0" --max-time 10

python3 - "$TMP_DIR" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
load = lambda name: json.loads((root / name).read_text())
run = load("run-final.json")
runtime = load("runtime.json")
calls = load("model-calls.json")
contexts = load("contexts.json")
events = load("events.json")
trajectory = load("trajectory.json")

assert run["status"] == "completed"
assert run["result"] == {"content": "Nico native runtime is ready."}
assert runtime["provider_name"] == "nico_native"
assert runtime["protocol_version"] == "2.0"
assert runtime["execution_mode"] == "direct"
assert len(calls) == 1 and calls[0]["status"] == "completed"
assert calls[0]["usage_status"] == "exact"
assert calls[0]["provider_request_id"] == "nico-goal-g-fake-request"
assert len(contexts) == 1 and len(contexts[0]["content_hash"]) == 64
types = {item["event_type"] for item in events}
assert {"RuntimeContextSnapshotCreated", "RuntimeModelCallStarted", "RuntimeModelCallCompleted"} <= types
assert trajectory["provider"] == "nico_native"
assert "event: RuntimeModelOutputDelta" in (root / "events.sse").read_text()

serialized = "\n".join(path.read_text(errors="replace") for path in root.iterdir())
assert "goal-g-fake-token" not in serialized
PY

log "PASS Goal G hermetic native direct execution"
