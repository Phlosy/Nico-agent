#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

required_files=(
  README.md
  docs/plans/agent-platform-implementation-taskbook.md
  docs/plans/goal-a-architecture-baseline-plan.md
  docs/repository-assessment.md
  docs/architecture.md
  docs/domain-model.md
  docs/state-machines.md
  docs/roadmap.md
  docs/progress/goal-status.md
  docs/progress/feature-matrix.md
  docs/handoffs/2026-07-16-goal-a-handoff.md
  docs/decisions/ADR-0001-modular-monolith-and-worker.md
  docs/decisions/ADR-0002-authoritative-storage-and-execution.md
  docs/decisions/ADR-0003-runtime-provider-boundary.md
  docs/decisions/ADR-0004-trusted-plugin-model.md
  docs/decisions/ADR-0005-controlled-growth.md
  docs/decisions/ADR-0006-multitenancy-isolation.md
)

for file in "${required_files[@]}"; do
  if [[ ! -s "$file" ]]; then
    echo "FAIL missing or empty: $file" >&2
    exit 1
  fi
done

require_text() {
  local file="$1"
  local text="$2"
  if ! grep -Fq -- "$text" "$file"; then
    echo "FAIL '$text' missing from $file" >&2
    exit 1
  fi
}

expected_taskbook_sha="7088fddfb7fdbe6e43f81db1225f6e984ffc0c0ebd98904ef310f02b371ad604"
actual_taskbook_sha="$(sha256sum docs/plans/agent-platform-implementation-taskbook.md | awk '{print $1}')"
if [[ "$actual_taskbook_sha" != "$expected_taskbook_sha" ]]; then
  echo "FAIL taskbook checksum: expected $expected_taskbook_sha got $actual_taskbook_sha" >&2
  exit 1
fi

if [[ "$(wc -l < docs/plans/agent-platform-implementation-taskbook.md)" -ne 1180 ]]; then
  echo "FAIL taskbook line count is not 1180" >&2
  exit 1
fi

for goal in {A..K}; do
  if ! grep -q "| $goal |" docs/progress/goal-status.md; then
    echo "FAIL Goal $goal missing from goal-status.md" >&2
    exit 1
  fi
done

require_text docs/progress/goal-status.md "| A | Verified | 100% |"
require_text docs/progress/feature-matrix.md "| 功能 | 设计完成 | 代码完成 | 单测完成 | 集成测试 | E2E | 文档 | 最终状态 |"
require_text docs/progress/feature-matrix.md "仅设计"
require_text docs/progress/feature-matrix.md "未实现"

for section in "Runtime Provider" "多租户边界" "Tool 与沙箱边界" "Memory 与 Skill 成长路径" "Plugin 边界"; do
  require_text docs/architecture.md "$section"
done

domain_objects=(Agent AgentVersion Team TeamMembership RoleDefinition Task Run RunStep ToolDefinition ToolCall Memory Skill SkillVersion Artifact Evaluation WorkflowDefinition Plugin Event Approval RuntimeProvider)
for object in "${domain_objects[@]}"; do
  require_text docs/domain-model.md "| $object |"
done

for state in Draft Ready Running Paused Archived Error Created Assigned WaitingForReview RevisionRequired Completed Failed Cancelled Pending Planning WaitingForTool WaitingForApproval TimedOut Candidate Testing Approved Published Deprecated Disabled; do
  require_text docs/state-machines.md "$state"
done

for section in {1..15}; do
  if ! grep -Eq "^## ${section}\. " docs/handoffs/2026-07-16-goal-a-handoff.md; then
    echo "FAIL Handoff section $section missing" >&2
    exit 1
  fi
done

adr_files=(docs/decisions/ADR-*.md)
if [[ "${#adr_files[@]}" -lt 6 ]]; then
  echo "FAIL expected at least 6 ADR files, found ${#adr_files[@]}" >&2
  exit 1
fi
for adr in "${adr_files[@]}"; do
  for heading in 背景 问题 候选方案 最终选择 选择原因 代价 后续影响 可逆性; do
    require_text "$adr" "## $heading"
  done
done

evidence_dirs=(artifacts/goals/goal-a/*)
if [[ ! -d "${evidence_dirs[0]}" ]]; then
  echo "FAIL Goal A evidence directory missing" >&2
  exit 1
fi

evidence_files=(commands.txt repository-scan.log validation.log api-output.txt page-screenshots.txt sample-data.txt errors.log version-info.txt acceptance-summary.md)
for file in "${evidence_files[@]}"; do
  if [[ ! -s "${evidence_dirs[0]}/$file" ]]; then
    echo "FAIL evidence missing or empty: ${evidence_dirs[0]}/$file" >&2
    exit 1
  fi
done

echo "PASS Goal A architecture baseline"
echo "taskbook_sha=$actual_taskbook_sha"
echo "evidence_dir=${evidence_dirs[0]}"
