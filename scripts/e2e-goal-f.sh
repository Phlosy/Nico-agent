#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

require_command curl
require_command python3
load_env_file
"$ROOT_DIR/scripts/dev.sh" --detach

API_BASE="http://localhost:${API_PORT:-18000}"
TMP_DIR="$(mktemp -d)"
RUN_KEY="$(date -u +%Y%m%d%H%M%S)-$$"
STATUS_LOG="$TMP_DIR/http-statuses.txt"

archive_and_cleanup() {
  if [[ -n "${NICO_EVIDENCE_DIR:-}" ]]; then
    mkdir -p "$NICO_EVIDENCE_DIR/e2e-goal-f"
    cp "$TMP_DIR"/* "$NICO_EVIDENCE_DIR/e2e-goal-f/" 2>/dev/null || true
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
  python3 - "$1" "$2" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1]))
print(next(item["id"] for item in data["memories"] if item["memory_type"] == sys.argv[2]))
PY
}

request() {
  local method="$1"
  local path="$2"
  local body="$3"
  local output="$4"
  shift 4
  local curl_args=(
    --fail-with-body --silent --show-error --request "$method"
    --header "Content-Type: application/json"
  )
  if [[ -n "$body" ]]; then
    curl_args+=(--data "$body")
  fi
  curl "${curl_args[@]}" "$@" --output "$output" "$API_BASE$path"
}

request_status() {
  local expected="$1"
  local method="$2"
  local path="$3"
  local body="$4"
  local output="$5"
  shift 5
  local curl_args=(
    --silent --show-error --request "$method"
    --header "Content-Type: application/json"
  )
  if [[ -n "$body" ]]; then
    curl_args+=(--data "$body")
  fi
  local actual
  actual="$(curl "${curl_args[@]}" "$@" --output "$output" --write-out '%{http_code}' "$API_BASE$path")"
  printf '%s %s expected=%s actual=%s\n' "$method" "$path" "$expected" "$actual" >>"$STATUS_LOG"
  [[ "$actual" == "$expected" ]] || die "$method $path returned $actual, expected $expected"
}

log "creating a terminal Goal F Run through the public API and Compose worker"
request POST /api/v1/tenants/bootstrap \
  "{\"name\":\"Goal F E2E $RUN_KEY\",\"slug\":\"goal-f-$RUN_KEY\"}" \
  "$TMP_DIR/tenant.json"
TENANT_ID="$(json_path "$TMP_DIR/tenant.json" id)"
HEADERS=(--header "X-Tenant-ID: $TENANT_ID" --header "X-Actor-ID: goal-f-requester")
REVIEW_HEADERS=(--header "X-Tenant-ID: $TENANT_ID" --header "X-Actor-ID: goal-f-independent-reviewer")

request POST /api/v1/projects \
  "{\"name\":\"Growth Project $RUN_KEY\",\"metadata\":{\"suite\":\"goal-f\"}}" \
  "$TMP_DIR/project.json" "${HEADERS[@]}"
PROJECT_ID="$(json_path "$TMP_DIR/project.json" id)"

request POST /api/v1/agents \
  "{\"name\":\"goal-f-agent-$RUN_KEY\",\"display_name\":\"Goal F Growth Agent\"}" \
  "$TMP_DIR/agent.json" "${HEADERS[@]}"
AGENT_ID="$(json_path "$TMP_DIR/agent.json" id)"

VERSION_BODY="$(python3 - <<'PY'
import json

print(json.dumps({
    "role": "growth-acceptance",
    "mandate": "Produce bounded evidence and a reusable verified procedure",
    "boundaries": ["No external mutation", "Candidates cannot self-publish"],
    "run_config": {
        "runtime_provider": "mock",
        "mock": {
            "steps": [
                "collect tenant-scoped evidence",
                "verify evidence and summarize a reusable procedure",
            ],
            "output": {
                "finding": "Only reviewed tenant-scoped knowledge may be reused",
                "procedure": "collect, verify, independently review, publish",
            },
        },
    },
}, separators=(",", ":")))
PY
)"
request POST "/api/v1/agents/$AGENT_ID/versions" "$VERSION_BODY" \
  "$TMP_DIR/agent-version.json" "${HEADERS[@]}"
AGENT_VERSION_ID="$(json_path "$TMP_DIR/agent-version.json" id)"
request POST "/api/v1/agents/$AGENT_ID/versions/$AGENT_VERSION_ID/publish" \
  '{"expected_revision":1}' "$TMP_DIR/agent-ready.json" "${HEADERS[@]}"

request POST /api/v1/tasks \
  "{\"project_id\":\"$PROJECT_ID\",\"title\":\"Goal F controlled growth\",\"input\":{\"run_key\":\"$RUN_KEY\"},\"acceptance\":{\"reviewed_growth\":true},\"assignee_agent_id\":\"$AGENT_ID\",\"priority\":100}" \
  "$TMP_DIR/task.json" "${HEADERS[@]}"
TASK_ID="$(json_path "$TMP_DIR/task.json" id)"
request POST "/api/v1/tasks/$TASK_ID/runs" \
  '{"max_steps":8,"token_budget":2000,"timeout_seconds":60}' \
  "$TMP_DIR/run.json" "${HEADERS[@]}"
RUN_ID="$(json_path "$TMP_DIR/run.json" id)"

log "waiting for the Compose worker to complete Run $RUN_ID"
for attempt in {1..120}; do
  request GET "/api/v1/runs/$RUN_ID" '' "$TMP_DIR/run-final.json" "${HEADERS[@]}"
  run_status="$(json_path "$TMP_DIR/run-final.json" status)"
  if [[ "$run_status" == "completed" ]]; then
    break
  fi
  if [[ "$run_status" == "failed" || "$run_status" == "cancelled" || "$run_status" == "timed_out" ]]; then
    die "Goal F E2E Run entered terminal status: $run_status"
  fi
  [[ "$attempt" -eq 120 ]] && die "Goal F E2E Run did not complete"
  sleep 0.5
done
request GET "/api/v1/runs/$RUN_ID/trajectory" '' "$TMP_DIR/trajectory.json" "${HEADERS[@]}"

log "generating idempotent candidates and proving they are unusable before review"
request POST "/api/v1/runs/$RUN_ID/growth-candidates" '{}' \
  "$TMP_DIR/candidates.json" "${HEADERS[@]}"
request POST "/api/v1/runs/$RUN_ID/growth-candidates" '{}' \
  "$TMP_DIR/candidates-repeat.json" "${HEADERS[@]}"
MEMORY_ID="$(memory_id "$TMP_DIR/candidates.json" semantic)"
SKILL_ID="$(json_path "$TMP_DIR/candidates.json" skill skill_id)"
SKILL_VERSION_ID="$(json_path "$TMP_DIR/candidates.json" skill skill_version_id)"

request GET "/api/v1/memories/$MEMORY_ID" '' "$TMP_DIR/memory-candidate.json" "${HEADERS[@]}"
request GET "/api/v1/memories/$MEMORY_ID/sources" '' "$TMP_DIR/memory-sources.json" "${HEADERS[@]}"
MEMORY_SEARCH_BODY="$(python3 - "$TMP_DIR/memory-candidate.json" "$PROJECT_ID" <<'PY'
import json
import sys

memory = json.load(open(sys.argv[1]))
print(json.dumps({"query": memory["content"], "project_id": sys.argv[2]}))
PY
)"
request POST /api/v1/memories/search "$MEMORY_SEARCH_BODY" \
  "$TMP_DIR/memory-search-before-review.json" "${HEADERS[@]}"
request_status 400 POST "/api/v1/memories/$MEMORY_ID/publish" \
  '{"expected_revision":1}' "$TMP_DIR/memory-premature-publish.json" "${HEADERS[@]}"
request_status 400 GET "/api/v1/skills/$SKILL_ID/resolve?run_id=$RUN_ID" '' \
  "$TMP_DIR/skill-premature-resolve.json" "${HEADERS[@]}"

log "validating, independently approving, publishing and retrieving Memory"
request POST "/api/v1/memories/$MEMORY_ID/evaluations" '' \
  "$TMP_DIR/memory-evaluation.json" "${HEADERS[@]}"
request POST "/api/v1/memories/$MEMORY_ID/approvals" '{"expected_revision":1}' \
  "$TMP_DIR/memory-approval-request.json" "${HEADERS[@]}"
MEMORY_APPROVAL_ID="$(json_path "$TMP_DIR/memory-approval-request.json" id)"
request_status 403 POST "/api/v1/growth-approvals/$MEMORY_APPROVAL_ID/decision" \
  '{"decision":"approved","reason":"Self review must fail.","expected_revision":1}' \
  "$TMP_DIR/memory-self-review.json" "${HEADERS[@]}"
request POST "/api/v1/growth-approvals/$MEMORY_APPROVAL_ID/decision" \
  '{"decision":"approved","reason":"Independent source and validation review passed.","expected_revision":1}' \
  "$TMP_DIR/memory-approval.json" "${REVIEW_HEADERS[@]}"
request POST "/api/v1/memories/$MEMORY_ID/publish" '{"expected_revision":1}' \
  "$TMP_DIR/memory-published.json" "${HEADERS[@]}"
request POST /api/v1/memories/search "$MEMORY_SEARCH_BODY" \
  "$TMP_DIR/memory-search-after-review.json" "${HEADERS[@]}"
request GET "/api/v1/memories/$MEMORY_ID/evaluations" '' \
  "$TMP_DIR/memory-evaluations.json" "${HEADERS[@]}"
request GET "/api/v1/memories/$MEMORY_ID/approvals" '' \
  "$TMP_DIR/memory-approvals.json" "${HEADERS[@]}"

log "publishing Skill v1, releasing v2 through canary, promoting and rolling back"
request GET "/api/v1/skills/$SKILL_ID" '' "$TMP_DIR/skill-candidate.json" "${HEADERS[@]}"
request GET "/api/v1/skills/$SKILL_ID/versions/$SKILL_VERSION_ID" '' \
  "$TMP_DIR/skill-version-v1.json" "${HEADERS[@]}"
request GET "/api/v1/skills/$SKILL_ID/versions/$SKILL_VERSION_ID/sources" '' \
  "$TMP_DIR/skill-v1-sources.json" "${HEADERS[@]}"
request POST "/api/v1/skills/$SKILL_ID/versions/$SKILL_VERSION_ID/evaluations" '' \
  "$TMP_DIR/skill-v1-evaluation.json" "${HEADERS[@]}"
request POST "/api/v1/skills/$SKILL_ID/versions/$SKILL_VERSION_ID/approvals" \
  '{"expected_revision":1}' "$TMP_DIR/skill-v1-approval-request.json" "${HEADERS[@]}"
SKILL_V1_APPROVAL_ID="$(json_path "$TMP_DIR/skill-v1-approval-request.json" id)"
request POST "/api/v1/growth-approvals/$SKILL_V1_APPROVAL_ID/decision" \
  '{"decision":"approved","reason":"Independent v1 release review passed.","expected_revision":1}' \
  "$TMP_DIR/skill-v1-approval.json" "${REVIEW_HEADERS[@]}"
request POST "/api/v1/skills/$SKILL_ID/versions/$SKILL_VERSION_ID/publish" \
  '{"expected_skill_revision":1,"expected_version_revision":1}' \
  "$TMP_DIR/skill-v1-published.json" "${HEADERS[@]}"

V2_BODY="$(python3 - "$TMP_DIR/skill-version-v1.json" "$TMP_DIR/skill-v1-published.json" <<'PY'
import json
import sys

version = json.load(open(sys.argv[1]))
published = json.load(open(sys.argv[2]))
fields = (
    "conditions", "preconditions", "input_schema", "steps",
    "tools", "output_schema", "validation", "failure_modes",
)
draft = {field: version[field] for field in fields}
draft["steps"] = [*draft["steps"], {"id": "record-review", "action": "record_independent_review"}]
draft["validation"] = {**draft["validation"], "independent_review": True}
print(json.dumps({
    "draft": draft,
    "reason": "Require an explicit independent review record.",
    "expected_skill_revision": published["skill_revision"],
    "expected_version_revision": published["version_revision"],
}, separators=(",", ":")))
PY
)"
request POST "/api/v1/skills/$SKILL_ID/versions/$SKILL_VERSION_ID/revisions" "$V2_BODY" \
  "$TMP_DIR/skill-v2.json" "${HEADERS[@]}"
SKILL_VERSION_V2_ID="$(json_path "$TMP_DIR/skill-v2.json" skill_version_id)"
V2_VERSION_REVISION="$(json_path "$TMP_DIR/skill-v2.json" version_revision)"
request GET "/api/v1/skills/$SKILL_ID/versions/compare?from_version_id=$SKILL_VERSION_ID&to_version_id=$SKILL_VERSION_V2_ID" '' \
  "$TMP_DIR/skill-comparison.json" "${HEADERS[@]}"
request POST "/api/v1/skills/$SKILL_ID/versions/$SKILL_VERSION_V2_ID/evaluations" '' \
  "$TMP_DIR/skill-v2-evaluation.json" "${HEADERS[@]}"
request POST "/api/v1/skills/$SKILL_ID/versions/$SKILL_VERSION_V2_ID/approvals" \
  "{\"expected_revision\":$V2_VERSION_REVISION}" \
  "$TMP_DIR/skill-v2-approval-request.json" "${HEADERS[@]}"
SKILL_V2_APPROVAL_ID="$(json_path "$TMP_DIR/skill-v2-approval-request.json" id)"
request POST "/api/v1/growth-approvals/$SKILL_V2_APPROVAL_ID/decision" \
  '{"decision":"approved","reason":"Independent v2 release review passed.","expected_revision":1}' \
  "$TMP_DIR/skill-v2-approval.json" "${REVIEW_HEADERS[@]}"
V2_SKILL_REVISION="$(json_path "$TMP_DIR/skill-v2.json" skill_revision)"
request POST "/api/v1/skills/$SKILL_ID/versions/$SKILL_VERSION_V2_ID/publish" \
  "{\"expected_skill_revision\":$V2_SKILL_REVISION,\"expected_version_revision\":$V2_VERSION_REVISION}" \
  "$TMP_DIR/skill-v2-published.json" "${HEADERS[@]}"
V2_PUBLISHED_SKILL_REVISION="$(json_path "$TMP_DIR/skill-v2-published.json" skill_revision)"
request POST "/api/v1/skills/$SKILL_ID/deployments" \
  "{\"skill_version_id\":\"$SKILL_VERSION_V2_ID\",\"scope_type\":\"project\",\"scope_id\":\"$PROJECT_ID\",\"rollout_percentage\":50,\"expected_skill_revision\":$V2_PUBLISHED_SKILL_REVISION}" \
  "$TMP_DIR/skill-canary.json" "${HEADERS[@]}"
request GET "/api/v1/skills/$SKILL_ID/resolve?run_id=$RUN_ID" '' \
  "$TMP_DIR/skill-canary-resolution.json" "${HEADERS[@]}"
CANARY_SKILL_REVISION="$(json_path "$TMP_DIR/skill-canary.json" skill_revision)"
request POST "/api/v1/skills/$SKILL_ID/promote" \
  "{\"skill_version_id\":\"$SKILL_VERSION_V2_ID\",\"reason\":\"Canary evidence accepted.\",\"expected_skill_revision\":$CANARY_SKILL_REVISION}" \
  "$TMP_DIR/skill-promoted.json" "${HEADERS[@]}"
request GET "/api/v1/skills/$SKILL_ID/resolve?run_id=$RUN_ID" '' \
  "$TMP_DIR/skill-promoted-resolution.json" "${HEADERS[@]}"
PROMOTED_SKILL_REVISION="$(json_path "$TMP_DIR/skill-promoted.json" skill_revision)"
request POST "/api/v1/skills/$SKILL_ID/rollback" \
  "{\"skill_version_id\":\"$SKILL_VERSION_ID\",\"reason\":\"Verified historical rollback.\",\"expected_skill_revision\":$PROMOTED_SKILL_REVISION}" \
  "$TMP_DIR/skill-rolled-back.json" "${HEADERS[@]}"
request GET "/api/v1/skills/$SKILL_ID/resolve?run_id=$RUN_ID" '' \
  "$TMP_DIR/skill-rollback-resolution.json" "${HEADERS[@]}"
request GET "/api/v1/skills/$SKILL_ID/deployments" '' \
  "$TMP_DIR/skill-deployments.json" "${HEADERS[@]}"

log "proving a second tenant cannot observe any Goal F resource"
request POST /api/v1/tenants/bootstrap \
  "{\"name\":\"Goal F Foreign $RUN_KEY\",\"slug\":\"goal-f-foreign-$RUN_KEY\"}" \
  "$TMP_DIR/foreign-tenant.json"
FOREIGN_TENANT_ID="$(json_path "$TMP_DIR/foreign-tenant.json" id)"
FOREIGN_HEADERS=(--header "X-Tenant-ID: $FOREIGN_TENANT_ID" --header "X-Actor-ID: goal-f-foreign")
request_status 404 GET "/api/v1/memories/$MEMORY_ID" '' \
  "$TMP_DIR/foreign-memory.json" "${FOREIGN_HEADERS[@]}"
request_status 404 GET "/api/v1/skills/$SKILL_ID" '' \
  "$TMP_DIR/foreign-skill.json" "${FOREIGN_HEADERS[@]}"
request_status 404 POST "/api/v1/runs/$RUN_ID/growth-candidates" '{}' \
  "$TMP_DIR/foreign-candidates.json" "${FOREIGN_HEADERS[@]}"
request GET /api/v1/memories '' "$TMP_DIR/foreign-memories.json" "${FOREIGN_HEADERS[@]}"
request GET /api/v1/skills '' "$TMP_DIR/foreign-skills.json" "${FOREIGN_HEADERS[@]}"
request POST /api/v1/memories/search '{"query":"reviewed knowledge"}' \
  "$TMP_DIR/foreign-memory-search.json" "${FOREIGN_HEADERS[@]}"
request GET /openapi.json '' "$TMP_DIR/openapi.json"

python3 - "$TMP_DIR" "$MEMORY_ID" "$SKILL_ID" "$SKILL_VERSION_ID" "$SKILL_VERSION_V2_ID" "$RUN_ID" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
memory_id, skill_id, v1_id, v2_id, run_id = sys.argv[2:]

def load(name):
    return json.loads((root / name).read_text())

run = load("run-final.json")
trajectory = load("trajectory.json")
candidates = load("candidates.json")
repeat = load("candidates-repeat.json")
memory_sources = load("memory-sources.json")
before = load("memory-search-before-review.json")
premature_memory = load("memory-premature-publish.json")
premature_skill = load("skill-premature-resolve.json")
published_memory = load("memory-published.json")
after = load("memory-search-after-review.json")
comparison = load("skill-comparison.json")
canary = load("skill-canary-resolution.json")
promoted = load("skill-promoted-resolution.json")
rolled_back = load("skill-rollback-resolution.json")
deployments = load("skill-deployments.json")
openapi = load("openapi.json")

assert run["status"] == "completed"
assert trajectory["events"][-1]["type"] == "run.completed"
assert {item["memory_type"] for item in candidates["memories"]} == {
    "episodic", "semantic", "procedural"
}
assert candidates["skill"]["skill_id"] == skill_id
assert candidates["already_generated"] is False
assert repeat["already_generated"] is True
assert repeat["memories"] == candidates["memories"] and repeat["skill"] == candidates["skill"]
assert before == []
assert premature_memory["code"] == "GROWTH_EVALUATION_NOT_PASSED"
assert premature_skill["code"] == "SKILL_NOT_RESOLVABLE"
assert published_memory["id"] == memory_id and published_memory["status"] == "active"
assert published_memory["indexed"] is True and published_memory["chunk_count"] >= 1
assert any(item["memory_id"] == memory_id for item in after)
assert after[0]["sources"][0]["run_id"] == run_id
assert len(memory_sources) >= 1 and all("snapshot" not in source for source in memory_sources)
assert comparison["direction"] == "upgrade"
assert {"steps", "validation"} <= set(comparison["changed_fields"])
assert canary["selection"] in {"stable", "canary"}
assert promoted["selection"] == "stable" and promoted["skill_version_id"] == v2_id
assert rolled_back["selection"] == "stable" and rolled_back["skill_version_id"] == v1_id
assert deployments and all(item["status"] == "retired" for item in deployments)
assert load("foreign-memories.json") == []
assert load("foreign-skills.json") == []
assert load("foreign-memory-search.json") == []
assert load("foreign-memory.json")["code"] == "RESOURCE_NOT_FOUND"
assert load("foreign-skill.json")["code"] == "RESOURCE_NOT_FOUND"
assert load("foreign-candidates.json")["code"] == "RESOURCE_NOT_FOUND"

required_paths = {
    "/api/v1/runs/{run_id}/growth-candidates",
    "/api/v1/memories/search",
    "/api/v1/memories/{memory_id}/sources",
    "/api/v1/skills/{skill_id}/versions/compare",
    "/api/v1/skills/{skill_id}/deployments",
    "/api/v1/skills/{skill_id}/rollback",
}
assert required_paths <= openapi["paths"].keys()
source_properties = openapi["components"]["schemas"]["GrowthSourceRead"]["properties"]
assert not {"snapshot", "embedding", "arguments", "provider_state"} & source_properties.keys()
serialized = json.dumps({
    "sources": memory_sources,
    "search": after,
    "deployments": deployments,
})
assert "execution_lease_token" not in serialized
assert "nico-sandbox-development-token" not in serialized
PY

"${COMPOSE[@]}" ps
log "PASS Goal F terminal Run -> candidates -> reviewed Memory/Skill -> canary/promotion/rollback E2E (run=$RUN_ID)"
