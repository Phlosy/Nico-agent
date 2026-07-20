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
export NICO_WORKER_LEASE_SECONDS=10
export NICO_WORKER_HEARTBEAT_SECONDS=1

log "starting the hermetic Goal K Memory/Skill runtime stack"
"${COMPOSE[@]}" --profile goal-k up --detach --build --force-recreate fake-model api worker
wait_for_service_health fake-model
wait_for_service_health api

API_BASE="http://localhost:${API_PORT:-18000}"
TMP_DIR="$(mktemp -d)"
RUN_KEY="$(date -u +%Y%m%d%H%M%S)-$$"

archive_and_cleanup() {
  if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
    mkdir -p "$NICO_EVIDENCE_DIR/e2e-goal-k"
    cp "$TMP_DIR"/* "$NICO_EVIDENCE_DIR/e2e-goal-k/" 2>/dev/null || true
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

memory_id() {
  python3 - "$1" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1]))
print(next(item["id"] for item in data["memories"] if item["memory_type"] == "semantic"))
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

wait_for_run() {
  local run_id="$1" output="$2"
  shift 2
  for attempt in {1..160}; do
    request GET "/api/v1/runs/$run_id" '' "$output" "$@"
    local status
    status="$(json_path "$output" status)"
    [[ "$status" == "completed" ]] && return 0
    if [[ "$status" =~ ^(failed|cancelled|timed_out)$ ]]; then
      cat "$output" >&2
      "${COMPOSE[@]}" logs --no-color --tail=120 worker >&2
      die "Run $run_id entered $status"
    fi
    sleep 0.25
  done
  die "Run $run_id did not complete"
}

request POST /api/v1/tenants/bootstrap \
  "{\"name\":\"Goal K E2E $RUN_KEY\",\"slug\":\"goal-k-$RUN_KEY\"}" \
  "$TMP_DIR/tenant.json"
TENANT_ID="$(json_path "$TMP_DIR/tenant.json" id)"
HEADERS=(--header "X-Tenant-ID: $TENANT_ID" --header "X-Actor-ID: goal-k-requester")
REVIEW_HEADERS=(--header "X-Tenant-ID: $TENANT_ID" --header "X-Actor-ID: goal-k-reviewer")

request POST /api/v1/model-endpoints \
  '{"stable_key":"goal-k-model","display_name":"Goal K fake model","base_url":"http://fake-model:8100/v1","credential_ref":"env:NICO_MODEL_SECRET_GOAL_G","allowed_models":["goal-k-fake"],"capabilities":{"streaming":true}}' \
  "$TMP_DIR/model-endpoint.json" "${HEADERS[@]}"
ENDPOINT_ID="$(json_path "$TMP_DIR/model-endpoint.json" id)"

request POST /api/v1/projects \
  "{\"name\":\"Knowledge Project $RUN_KEY\"}" \
  "$TMP_DIR/project.json" "${HEADERS[@]}"
PROJECT_ID="$(json_path "$TMP_DIR/project.json" id)"

request POST /api/v1/agents \
  "{\"name\":\"goal-k-source-$RUN_KEY\",\"display_name\":\"Goal K Source Agent\"}" \
  "$TMP_DIR/source-agent.json" "${HEADERS[@]}"
SOURCE_AGENT_ID="$(json_path "$TMP_DIR/source-agent.json" id)"
request POST "/api/v1/agents/$SOURCE_AGENT_ID/versions" \
  "{\"role\":\"knowledge-source\",\"mandate\":\"Produce reviewed reusable knowledge.\",\"runtime_provider\":\"nico_native\",\"execution_mode\":\"direct\",\"model_endpoint_id\":\"$ENDPOINT_ID\",\"model_name\":\"goal-k-fake\"}" \
  "$TMP_DIR/source-version.json" "${HEADERS[@]}"
SOURCE_VERSION_ID="$(json_path "$TMP_DIR/source-version.json" id)"
request POST "/api/v1/agents/$SOURCE_AGENT_ID/versions/$SOURCE_VERSION_ID/publish" \
  '{"expected_revision":1}' "$TMP_DIR/source-ready.json" "${HEADERS[@]}"

request POST /api/v1/tasks \
  "{\"project_id\":\"$PROJECT_ID\",\"title\":\"Create governed knowledge\",\"input\":{\"topic\":\"runtime knowledge reuse\"},\"acceptance\":{\"non_empty\":true},\"assignee_agent_id\":\"$SOURCE_AGENT_ID\",\"priority\":100}" \
  "$TMP_DIR/source-task.json" "${HEADERS[@]}"
SOURCE_TASK_ID="$(json_path "$TMP_DIR/source-task.json" id)"
request POST "/api/v1/tasks/$SOURCE_TASK_ID/runs" \
  '{"max_steps":4,"token_budget":1000,"timeout_seconds":60}' \
  "$TMP_DIR/source-run.json" "${HEADERS[@]}"
SOURCE_RUN_ID="$(json_path "$TMP_DIR/source-run.json" id)"
wait_for_run "$SOURCE_RUN_ID" "$TMP_DIR/source-run-final.json" "${HEADERS[@]}"

log "generating governed candidates and publishing exactly one Memory and one Skill"
request POST "/api/v1/runs/$SOURCE_RUN_ID/growth-candidates" '{}' \
  "$TMP_DIR/candidates.json" "${HEADERS[@]}"
MEMORY_ID="$(memory_id "$TMP_DIR/candidates.json")"
SKILL_ID="$(json_path "$TMP_DIR/candidates.json" skill skill_id)"
SKILL_VERSION_ID="$(json_path "$TMP_DIR/candidates.json" skill skill_version_id)"

request POST "/api/v1/memories/$MEMORY_ID/evaluations" '' \
  "$TMP_DIR/memory-evaluation.json" "${HEADERS[@]}"
request POST "/api/v1/memories/$MEMORY_ID/approvals" '{"expected_revision":1}' \
  "$TMP_DIR/memory-approval-request.json" "${HEADERS[@]}"
MEMORY_APPROVAL_ID="$(json_path "$TMP_DIR/memory-approval-request.json" id)"
request POST "/api/v1/growth-approvals/$MEMORY_APPROVAL_ID/decision" \
  '{"decision":"approved","reason":"Independent Goal K Memory review passed.","expected_revision":1}' \
  "$TMP_DIR/memory-approved.json" "${REVIEW_HEADERS[@]}"
request POST "/api/v1/memories/$MEMORY_ID/publish" '{"expected_revision":1}' \
  "$TMP_DIR/memory-published.json" "${HEADERS[@]}"

request POST "/api/v1/skills/$SKILL_ID/versions/$SKILL_VERSION_ID/evaluations" '' \
  "$TMP_DIR/skill-evaluation.json" "${HEADERS[@]}"
request POST "/api/v1/skills/$SKILL_ID/versions/$SKILL_VERSION_ID/approvals" \
  '{"expected_revision":1}' "$TMP_DIR/skill-approval-request.json" "${HEADERS[@]}"
SKILL_APPROVAL_ID="$(json_path "$TMP_DIR/skill-approval-request.json" id)"
request POST "/api/v1/growth-approvals/$SKILL_APPROVAL_ID/decision" \
  '{"decision":"approved","reason":"Independent Goal K Skill review passed.","expected_revision":1}' \
  "$TMP_DIR/skill-approved.json" "${REVIEW_HEADERS[@]}"
request POST "/api/v1/skills/$SKILL_ID/versions/$SKILL_VERSION_ID/publish" \
  '{"expected_skill_revision":1,"expected_version_revision":1}' \
  "$TMP_DIR/skill-published.json" "${HEADERS[@]}"

POLICIES="$(python3 - "$SKILL_ID" <<'PY'
import json
import sys

print(json.dumps({
    "memory_policy": {
        "enabled": True,
        "scopes": ["project"],
        "memory_types": ["semantic"],
        "top_k": 1,
        "max_chars": 3000,
        "max_tokens": 750,
        "minimum_similarity": 0,
    },
    "skill_policy": {
        "enabled": True,
        "scopes": ["project"],
        "allowed_skill_ids": [sys.argv[1]],
        "top_k": 1,
        "max_chars": 3000,
        "max_tokens": 750,
    },
}, separators=(",", ":")))
PY
)"
request PATCH /api/v1/tenant/settings \
  "{\"expected_revision\":1,\"settings\":$POLICIES}" \
  "$TMP_DIR/tenant-settings.json" "${HEADERS[@]}"

request POST /api/v1/agents \
  "{\"name\":\"goal-k-consumer-$RUN_KEY\",\"display_name\":\"Goal K Consumer Agent\"}" \
  "$TMP_DIR/consumer-agent.json" "${HEADERS[@]}"
CONSUMER_AGENT_ID="$(json_path "$TMP_DIR/consumer-agent.json" id)"
CONSUMER_VERSION_BODY="$(python3 - "$ENDPOINT_ID" "$SKILL_ID" <<'PY'
import json
import sys

print(json.dumps({
    "role": "knowledge-consumer",
    "mandate": "Use only frozen published project knowledge.",
    "runtime_provider": "nico_native",
    "execution_mode": "direct",
    "model_endpoint_id": sys.argv[1],
    "model_name": "goal-k-fake",
    "memory_policy": {
        "enabled": True,
        "scopes": ["project", "agent"],
        "memory_types": ["semantic"],
        "top_k": 1,
        "max_chars": 2000,
        "max_tokens": 500,
        "minimum_similarity": 0,
    },
    "skill_policy": {
        "enabled": True,
        "scopes": ["project", "agent"],
        "allowed_skill_ids": [sys.argv[2]],
        "top_k": 1,
        "max_chars": 2000,
        "max_tokens": 500,
    },
}, separators=(",", ":")))
PY
)"
request POST "/api/v1/agents/$CONSUMER_AGENT_ID/versions" "$CONSUMER_VERSION_BODY" \
  "$TMP_DIR/consumer-version.json" "${HEADERS[@]}"
CONSUMER_VERSION_ID="$(json_path "$TMP_DIR/consumer-version.json" id)"
request POST "/api/v1/agents/$CONSUMER_AGENT_ID/versions/$CONSUMER_VERSION_ID/publish" \
  '{"expected_revision":1}' "$TMP_DIR/consumer-ready.json" "${HEADERS[@]}"

request POST /api/v1/tasks \
  "{\"project_id\":\"$PROJECT_ID\",\"title\":\"Use frozen published knowledge\",\"input\":{\"question\":\"Apply the reviewed runtime reuse policy\"},\"acceptance\":{\"non_empty\":true},\"assignee_agent_id\":\"$CONSUMER_AGENT_ID\",\"priority\":100}" \
  "$TMP_DIR/consumer-task.json" "${HEADERS[@]}"
CONSUMER_TASK_ID="$(json_path "$TMP_DIR/consumer-task.json" id)"
request POST "/api/v1/tasks/$CONSUMER_TASK_ID/runs" \
  '{"max_steps":4,"token_budget":1000,"timeout_seconds":60}' \
  "$TMP_DIR/consumer-run.json" "${HEADERS[@]}"
RUN_ID="$(json_path "$TMP_DIR/consumer-run.json" id)"
wait_for_run "$RUN_ID" "$TMP_DIR/run-final.json" "${HEADERS[@]}"

request GET "/api/v1/runs/$RUN_ID/runtime" '' "$TMP_DIR/runtime.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/contexts" '' "$TMP_DIR/contexts.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/model-calls" '' "$TMP_DIR/model-calls.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/knowledge-usages" '' \
  "$TMP_DIR/knowledge-usages.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/trajectory" '' "$TMP_DIR/trajectory.json" "${HEADERS[@]}"
request POST "/api/v1/runs/$RUN_ID/growth-candidates" '{}' \
  "$TMP_DIR/consumer-growth.json" "${HEADERS[@]}"
CONSUMER_MEMORY_ID="$(memory_id "$TMP_DIR/consumer-growth.json")"
request GET "/api/v1/memories/$CONSUMER_MEMORY_ID/sources" '' \
  "$TMP_DIR/consumer-growth-sources.json" "${HEADERS[@]}"

python3 - "$TMP_DIR" "$MEMORY_ID" "$SKILL_ID" "$SKILL_VERSION_ID" "$RUN_ID" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
memory_id, skill_id, skill_version_id, run_id = sys.argv[2:]
load = lambda name: json.loads((root / name).read_text())

run = load("run-final.json")
runtime = load("runtime.json")
contexts = load("contexts.json")
calls = load("model-calls.json")
usages = load("knowledge-usages.json")
trajectory = load("trajectory.json")
growth_sources = load("consumer-growth-sources.json")

assert run["status"] == "completed"
assert run["result"] == {
    "content": "Goal K used one published Memory and one published Skill."
}
assert runtime["knowledge_policy_snapshot"]["memory"]["top_k"] == 1
assert runtime["knowledge_policy_snapshot"]["memory"]["max_tokens"] == 500
assert runtime["knowledge_policy_snapshot"]["skill"]["allowed_skill_ids"] == [skill_id]
assert "knowledge_selection_snapshot" not in runtime
assert len(contexts) == 1
assert contexts[0]["memory_refs"] == [{
    **contexts[0]["memory_refs"][0], "memory_id": memory_id
}]
assert contexts[0]["skill_refs"] == [{
    **contexts[0]["skill_refs"][0],
    "skill_id": skill_id,
    "skill_version_id": skill_version_id,
}]
assert len(calls) == 1 and calls[0]["status"] == "completed"
assert len(usages) == 2
assert {item["source_type"] for item in usages} == {"memory", "skill_version"}
assert {item["status"] for item in usages} == {"succeeded"}
assert all(item["context_count"] == 1 for item in usages)
assert all(item["model_call_count"] == 1 for item in usages)
assert all(item["effect_metadata"]["consumed"] is True for item in usages)
assert trajectory["status"] == "completed"
assert growth_sources and all(item["run_id"] == run_id for item in growth_sources)
for path in root.iterdir():
    if path.is_file():
        text = path.read_text(errors="ignore")
        assert "goal-g-fake-token" not in text
PY

log "PASS Goal K published knowledge recall, immutable references and effect tracing"
