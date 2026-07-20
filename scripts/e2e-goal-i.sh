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

log "starting the hermetic Goal I planning stack"
"${COMPOSE[@]}" --profile goal-g up --detach --build --force-recreate fake-model api worker
wait_for_service_health fake-model
wait_for_service_health api

API_BASE="http://localhost:${API_PORT:-18000}"
TMP_DIR="$(mktemp -d)"
RUN_KEY="$(date -u +%Y%m%d%H%M%S)-$$"

archive_and_cleanup() {
  if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
    mkdir -p "$NICO_EVIDENCE_DIR/e2e-goal-i"
    cp "$TMP_DIR"/* "$NICO_EVIDENCE_DIR/e2e-goal-i/" 2>/dev/null || true
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
  "{\"name\":\"Goal I E2E $RUN_KEY\",\"slug\":\"goal-i-$RUN_KEY\"}" \
  "$TMP_DIR/tenant.json"
TENANT_ID="$(json_path "$TMP_DIR/tenant.json" id)"
HEADERS=(--header "X-Tenant-ID: $TENANT_ID" --header "X-Actor-ID: goal-i-verifier")

request POST /api/v1/model-endpoints \
  '{"stable_key":"goal-i-model","display_name":"Goal I fake model","base_url":"http://fake-model:8100/v1","credential_ref":"env:NICO_MODEL_SECRET_GOAL_G","allowed_models":["goal-i-fake"],"capabilities":{"streaming":true,"structured_output":true}}' \
  "$TMP_DIR/model-endpoint.json" "${HEADERS[@]}"
ENDPOINT_ID="$(json_path "$TMP_DIR/model-endpoint.json" id)"

request POST /api/v1/projects \
  "{\"name\":\"Planning Project $RUN_KEY\"}" \
  "$TMP_DIR/project.json" "${HEADERS[@]}"
PROJECT_ID="$(json_path "$TMP_DIR/project.json" id)"
request POST /api/v1/agents \
  "{\"name\":\"goal-i-agent-$RUN_KEY\",\"display_name\":\"Goal I Planning Agent\"}" \
  "$TMP_DIR/agent.json" "${HEADERS[@]}"
AGENT_ID="$(json_path "$TMP_DIR/agent.json" id)"

VERSION_BODY="$(python3 - "$ENDPOINT_ID" <<'PY'
import json
import sys

print(json.dumps({
    "role": "planning-verifier",
    "mandate": "Plan, execute, validate, reflect, and complete the task.",
    "boundaries": ["Do not invent evidence", "Do not bypass deterministic validation"],
    "runtime_provider": "nico_native",
    "execution_mode": "plan_and_execute",
    "model_endpoint_id": sys.argv[1],
    "model_name": "goal-i-fake",
    "model_config": {"temperature": 0, "max_output_tokens": 256},
    "budgets": {"max_plan_steps": 4, "max_reflections": 2, "max_replans": 1},
    "run_config": {"completion_model_judge": True},
}, separators=(",", ":")))
PY
)"
request POST "/api/v1/agents/$AGENT_ID/versions" "$VERSION_BODY" \
  "$TMP_DIR/agent-version.json" "${HEADERS[@]}"
VERSION_ID="$(json_path "$TMP_DIR/agent-version.json" id)"
request POST "/api/v1/agents/$AGENT_ID/versions/$VERSION_ID/publish" \
  '{"expected_revision":1}' "$TMP_DIR/agent-ready.json" "${HEADERS[@]}"

request POST /api/v1/tasks \
  "{\"project_id\":\"$PROJECT_ID\",\"title\":\"Prove Plan Reflection Completion\",\"input\":{\"run_key\":\"$RUN_KEY\"},\"acceptance\":{\"non_empty\":true,\"required\":[\"content\"]},\"assignee_agent_id\":\"$AGENT_ID\",\"priority\":100}" \
  "$TMP_DIR/task.json" "${HEADERS[@]}"
TASK_ID="$(json_path "$TMP_DIR/task.json" id)"
request POST "/api/v1/tasks/$TASK_ID/runs" \
  '{"max_steps":8,"token_budget":4000,"timeout_seconds":90}' \
  "$TMP_DIR/run.json" "${HEADERS[@]}"
RUN_ID="$(json_path "$TMP_DIR/run.json" id)"

log "waiting for Goal I Run $RUN_ID"
for attempt in {1..180}; do
  request GET "/api/v1/runs/$RUN_ID" '' "$TMP_DIR/run-final.json" "${HEADERS[@]}"
  run_status="$(json_path "$TMP_DIR/run-final.json" status)"
  [[ "$run_status" == "completed" ]] && break
  if [[ "$run_status" =~ ^(failed|cancelled|timed_out)$ ]]; then
    cat "$TMP_DIR/run-final.json" >&2
    "${COMPOSE[@]}" logs --no-color --tail=120 worker >&2
    die "Goal I Run entered $run_status"
  fi
  [[ "$attempt" -eq 180 ]] && die "Goal I Run did not complete"
  sleep 0.5
done

request GET "/api/v1/runs/$RUN_ID/runtime" '' "$TMP_DIR/runtime.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/plans" '' "$TMP_DIR/plans.json" "${HEADERS[@]}"
FIRST_PLAN_ID="$(json_path "$TMP_DIR/plans.json" 0 id)"
SECOND_PLAN_ID="$(json_path "$TMP_DIR/plans.json" 1 id)"
request GET "/api/v1/runs/$RUN_ID/plans/$FIRST_PLAN_ID/steps" '' \
  "$TMP_DIR/plan-1-steps.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/plans/$SECOND_PLAN_ID/steps" '' \
  "$TMP_DIR/plan-2-steps.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/runtime-evaluations" '' \
  "$TMP_DIR/runtime-evaluations.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/model-calls" '' "$TMP_DIR/model-calls.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/contexts" '' "$TMP_DIR/contexts.json" "${HEADERS[@]}"
request GET "/api/v1/runs/$RUN_ID/events" '' "$TMP_DIR/events.json" "${HEADERS[@]}"

python3 - "$TMP_DIR" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
load = lambda name: json.loads((root / name).read_text())
run = load("run-final.json")
runtime = load("runtime.json")
plans = load("plans.json")
first_steps = load("plan-1-steps.json")
second_steps = load("plan-2-steps.json")
evaluations = load("runtime-evaluations.json")
calls = load("model-calls.json")
contexts = load("contexts.json")
events = load("events.json")

assert run["status"] == "completed"
assert run["result"]["result"] == {"content": "Goal I verified report"}
assert run["result"]["execution_summary"] == {
    "plan_revision": 2,
    "completed_steps": ["correct"],
    "reflections": 1,
}
assert run["checkpoint_schema_version"] == 3
assert runtime["execution_mode"] == "plan_and_execute"
assert runtime["loop_state"] == "completed"
assert [plan["revision"] for plan in plans] == [1, 2]
assert [plan["status"] for plan in plans] == ["superseded", "completed"]
assert plans[1]["supersedes_plan_id"] == plans[0]["id"]
assert first_steps[0]["status"] == "failed"
assert second_steps[0]["status"] == "completed"
assert [item["evaluation_type"] for item in evaluations] == [
    "step_validation", "reflection", "step_validation", "completion", "completion"
]
assert evaluations[-1]["method"] == "model" and evaluations[-1]["model_call_id"]
assert [item["call_key"] for item in calls] == [
    "planner:1",
    "plan:1:step:draft:attempt:1:round:1",
    "reflection:1",
    "planner:2",
    "plan:2:step:correct:attempt:2:round:1",
    "completion-judge:2:2",
]
assert all(item["status"] == "completed" for item in calls)
assert [item["version"] for item in contexts] == list(range(1, 7))
types = {item["event_type"] for item in events}
assert {
    "RuntimePlanCreated",
    "RuntimePlanStepFailed",
    "RuntimeReflectionCompleted",
    "RuntimeEvaluationCompleted",
    "RuntimePlanStepCompleted",
} <= types

serialized = "\n".join(path.read_text(errors="replace") for path in root.iterdir())
assert "goal-g-fake-token" not in serialized
PY

log "PASS Goal I Plan revisions, Reflection, Completion and billed judge"
