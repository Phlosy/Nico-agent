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
export NICO_WORKER_CONCURRENCY=2
export NICO_WORKER_LEASE_SECONDS=6
export NICO_WORKER_HEARTBEAT_SECONDS=1
export NICO_FAKE_MODEL_GOAL_J_DELAY_SECONDS=2

log "starting the hermetic Goal J multi-Agent stack"
"${COMPOSE[@]}" --profile goal-j up --detach --build --force-recreate \
  fake-model api worker
wait_for_service_health fake-model
wait_for_service_health api

API_BASE="http://localhost:${API_PORT:-18000}"
TMP_DIR="$(mktemp -d)"
RUN_KEY="$(date -u +%Y%m%d%H%M%S)-$$"

archive_and_cleanup() {
  if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
    mkdir -p "$NICO_EVIDENCE_DIR/e2e-goal-j"
    cp "$TMP_DIR"/* "$NICO_EVIDENCE_DIR/e2e-goal-j/" 2>/dev/null || true
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

request POST /api/v1/tenants/bootstrap \
  "{\"name\":\"Goal J E2E $RUN_KEY\",\"slug\":\"goal-j-$RUN_KEY\"}" \
  "$TMP_DIR/tenant.json"
TENANT_ID="$(json_path "$TMP_DIR/tenant.json" id)"
HEADERS=(--header "X-Tenant-ID: $TENANT_ID" --header "X-Actor-ID: goal-j-verifier")

request POST /api/v1/model-endpoints \
  '{"stable_key":"goal-j-model","display_name":"Goal J fake model","base_url":"http://fake-model:8100/v1","credential_ref":"env:NICO_MODEL_SECRET_GOAL_G","allowed_models":["goal-j-fake"],"capabilities":{"streaming":true,"tools":true}}' \
  "$TMP_DIR/model-endpoint.json" "${HEADERS[@]}"
ENDPOINT_ID="$(json_path "$TMP_DIR/model-endpoint.json" id)"

request POST /api/v1/projects \
  "{\"name\":\"Multi-Agent Project $RUN_KEY\"}" \
  "$TMP_DIR/project.json" "${HEADERS[@]}"
PROJECT_ID="$(json_path "$TMP_DIR/project.json" id)"

for role in child-a child-b parent; do
  request POST /api/v1/agents \
    "{\"name\":\"goal-j-$role-${RUN_KEY//:/-}\",\"display_name\":\"Goal J $role Agent\"}" \
    "$TMP_DIR/$role-agent.json" "${HEADERS[@]}"
done

for role in child-a child-b; do
  agent_id="$(json_path "$TMP_DIR/$role-agent.json" id)"
  request POST "/api/v1/agents/$agent_id/versions" \
    "{\"role\":\"$role\",\"mandate\":\"Produce one bounded finding and Artifact.\",\"runtime_provider\":\"nico_native\",\"execution_mode\":\"react\",\"model_endpoint_id\":\"$ENDPOINT_ID\",\"model_name\":\"goal-j-fake\",\"coordination_policy\":{\"enabled\":false},\"budgets\":{\"max_iterations\":4,\"max_tool_calls\":0}}" \
    "$TMP_DIR/$role-version.json" "${HEADERS[@]}"
  version_id="$(json_path "$TMP_DIR/$role-version.json" id)"
  request POST "/api/v1/agents/$agent_id/versions/$version_id/publish" \
    '{"expected_revision":1}' "$TMP_DIR/$role-ready.json" "${HEADERS[@]}"
done

CHILD_A_VERSION="$(json_path "$TMP_DIR/child-a-version.json" id)"
CHILD_B_VERSION="$(json_path "$TMP_DIR/child-b-version.json" id)"
POLICY="$(python3 - "$CHILD_A_VERSION" "$CHILD_B_VERSION" <<'PY'
import json
import sys

print(json.dumps({
    "enabled": True,
    "max_depth": 2,
    "max_children": 2,
    "max_parallelism": 2,
    "allowed_agent_version_ids": sys.argv[1:],
    "allowed_secret_refs": [],
}, separators=(",", ":")))
PY
)"
request PATCH /api/v1/tenant/settings \
  "{\"expected_revision\":1,\"settings\":{\"coordination_policy\":$POLICY,\"tool_policy\":{}}}" \
  "$TMP_DIR/tenant-settings.json" "${HEADERS[@]}"

PARENT_AGENT_ID="$(json_path "$TMP_DIR/parent-agent.json" id)"
request POST "/api/v1/agents/$PARENT_AGENT_ID/versions" \
  "{\"role\":\"parent\",\"mandate\":\"Delegate two independent branches and aggregate their referenced evidence.\",\"runtime_provider\":\"nico_native\",\"execution_mode\":\"react\",\"model_endpoint_id\":\"$ENDPOINT_ID\",\"model_name\":\"goal-j-fake\",\"coordination_policy\":$POLICY,\"budgets\":{\"max_iterations\":4,\"max_tool_calls\":0}}" \
  "$TMP_DIR/parent-version.json" "${HEADERS[@]}"
PARENT_VERSION="$(json_path "$TMP_DIR/parent-version.json" id)"
request POST "/api/v1/agents/$PARENT_AGENT_ID/versions/$PARENT_VERSION/publish" \
  '{"expected_revision":1}' "$TMP_DIR/parent-ready.json" "${HEADERS[@]}"

request POST /api/v1/tasks \
  "{\"project_id\":\"$PROJECT_ID\",\"title\":\"Coordinate two evidence branches\",\"input\":{\"target_agent_version_ids\":[\"$CHILD_A_VERSION\",\"$CHILD_B_VERSION\"]},\"acceptance\":{\"non_empty\":true},\"assignee_agent_id\":\"$PARENT_AGENT_ID\",\"priority\":100}" \
  "$TMP_DIR/task.json" "${HEADERS[@]}"
TASK_ID="$(json_path "$TMP_DIR/task.json" id)"
request POST "/api/v1/tasks/$TASK_ID/runs" \
  '{"max_steps":8,"token_budget":1200,"timeout_seconds":90}' \
  "$TMP_DIR/run.json" "${HEADERS[@]}"
RUN_ID="$(json_path "$TMP_DIR/run.json" id)"

log "waiting for Parent $RUN_ID to release its lease while two Child Runs execute"
for attempt in {1..120}; do
  request GET "/api/v1/runs/$RUN_ID" '' "$TMP_DIR/run-waiting.json" "${HEADERS[@]}"
  run_status="$(json_path "$TMP_DIR/run-waiting.json" status)"
  [[ "$run_status" == "waiting_for_subagent" ]] && break
  [[ "$run_status" =~ ^(failed|cancelled|timed_out|completed)$ ]] && \
    die "Goal J Parent entered unexpected state before fault injection: $run_status"
  [[ "$attempt" -eq 120 ]] && die "Goal J Parent did not suspend for Child Runs"
  sleep 0.1
done
request GET "/api/v1/runs/$RUN_ID/children" '' "$TMP_DIR/children-waiting.json" \
  "${HEADERS[@]}"

log "killing the Worker while Child calls are in flight, then starting a new Worker"
"${COMPOSE[@]}" --profile goal-j kill --signal KILL worker >/dev/null
"${COMPOSE[@]}" --profile goal-j up --detach --force-recreate worker >/dev/null

log "waiting for expired leases, Child recovery, Parent wakeup and final aggregation"
for attempt in {1..240}; do
  request GET "/api/v1/runs/$RUN_ID" '' "$TMP_DIR/run-final.json" "${HEADERS[@]}"
  run_status="$(json_path "$TMP_DIR/run-final.json" status)"
  [[ "$run_status" == "completed" ]] && break
  if [[ "$run_status" =~ ^(failed|cancelled|timed_out)$ ]]; then
    cat "$TMP_DIR/run-final.json" >&2
    "${COMPOSE[@]}" logs --no-color --tail=160 worker >&2
    die "Goal J Parent entered $run_status"
  fi
  [[ "$attempt" -eq 240 ]] && die "Goal J Parent did not recover and complete"
  sleep 0.25
done

request GET "/api/v1/runs/$RUN_ID/runtime" '' "$TMP_DIR/runtime.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/delegations" '' "$TMP_DIR/delegations.json" \
  "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/children" '' "$TMP_DIR/children.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/messages" '' "$TMP_DIR/messages.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/artifacts" '' "$TMP_DIR/artifacts.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/model-calls" '' "$TMP_DIR/model-calls.json" \
  "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/events" '' "$TMP_DIR/events.json" "${HEADERS[@]}"
request GET "/api/v1/audit?limit=500" '' "$TMP_DIR/audit.json" "${HEADERS[@]}"

for index in 0 1; do
  artifact_id="$(json_path "$TMP_DIR/artifacts.json" "$index" id)"
  request GET "/api/v1/runs/$RUN_ID/artifacts/$artifact_id/content" '' \
    "$TMP_DIR/artifact-$index.txt" "${HEADERS[@]}"
done

python3 - "$TMP_DIR" "$RUN_ID" "$ENDPOINT_ID" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
run_id = sys.argv[2]
endpoint_id = sys.argv[3]
load = lambda name: json.loads((root / name).read_text())
run = load("run-final.json")
waiting = load("run-waiting.json")
runtime = load("runtime.json")
delegations = load("delegations.json")
children = load("children.json")
messages = load("messages.json")
artifacts = load("artifacts.json")
calls = load("model-calls.json")
events = load("events.json")
audit = load("audit.json")

assert waiting["status"] == "waiting_for_subagent"
assert waiting["revision"] < run["revision"]
assert run["status"] == "completed"
assert run["result"] == {
    "content": "Goal J aggregated two child results and Artifact references."
}
assert runtime["status"] == "completed"
assert runtime["coordination_policy_snapshot"]["enabled"] is True
assert len(delegations) == 2
assert {item["status"] for item in delegations} == {"completed"}
assert {item["execution_mode"] for item in delegations} == {"parallel"}
assert all(item["budget_grant"]["token_limit"] == 300 for item in delegations)
assert all(item["permission_snapshot"]["model_endpoint_id"] == endpoint_id for item in delegations)
assert all(item["permission_snapshot"]["secret_refs"] == {} for item in delegations)
assert len(children) == 2 and {item["status"] for item in children} == {"completed"}
results = [item for item in messages if item["message_type"] == "result"]
assert len(results) == 2 and {item["status"] for item in results} == {"acknowledged"}
assert all(len(item["refs"]) == 1 and item["refs"][0].startswith("artifact:") for item in results)
assert len(artifacts) == 2 and {item["status"] for item in artifacts} == {"available"}
assert all(item["owner_run_id"] != run_id for item in artifacts)
assert all("object_key" not in item and "temp_object_key" not in item for item in artifacts)
assert len(calls) >= 2 and all("credential_ref" not in item for item in calls)
assert any(item["event_type"] == "DelegationAccepted" for item in events)
assert any(item["action"] == "artifact.store" for item in audit)
assert any(item["action"] == "coordination.delegate" for item in audit)
for index in range(2):
    assert (root / f"artifact-{index}.txt").read_text().startswith("Artifact evidence for")
for path in root.iterdir():
    if path.is_file():
        text = path.read_text(errors="ignore")
        assert "nico-minio-change-me" not in text
        assert "goal-g-fake-token" not in text
PY

unsigned_status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  "http://localhost:${MINIO_API_PORT:-19010}/${MINIO_BUCKET:-nico-artifacts}/")"
[[ "$unsigned_status" =~ ^(401|403)$ ]] || die "MinIO bucket unexpectedly allows anonymous access"

env \
  NICO_VERIFY_MINIO_URL="http://localhost:${MINIO_API_PORT:-19010}" \
  NICO_VERIFY_MINIO_USER="${MINIO_ROOT_USER:-nico-minio}" \
  NICO_VERIFY_MINIO_PASSWORD="${MINIO_ROOT_PASSWORD:-nico-minio-change-me}" \
  NICO_VERIFY_MINIO_BUCKET="${MINIO_BUCKET:-nico-artifacts}" \
  NICO_VERIFY_TENANT_ID="$TENANT_ID" \
  "$ROOT_DIR/.venv/bin/python" - <<'PY'
import os
from urllib.parse import urlparse

from minio import Minio

url = urlparse(os.environ["NICO_VERIFY_MINIO_URL"])
client = Minio(
    url.netloc,
    access_key=os.environ["NICO_VERIFY_MINIO_USER"],
    secret_key=os.environ["NICO_VERIFY_MINIO_PASSWORD"],
    secure=url.scheme == "https",
)
prefix = f"tenants/{os.environ['NICO_VERIFY_TENANT_ID']}/tmp/"
assert list(client.list_objects(os.environ["NICO_VERIFY_MINIO_BUCKET"], prefix=prefix)) == []
PY

log "PASS Goal J dynamic delegation, private Artifacts, Worker recovery and aggregation"
