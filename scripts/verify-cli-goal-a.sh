#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

required_files=(
  docs/plans/2026-07-19-001-nico-cli-first-class-interface-plan.md
  docs/cli.md
  docs/decisions/ADR-0011-first-class-thin-cli-and-terminal-stack.md
  docs/decisions/ADR-0012-conversation-is-not-runtime-session.md
  docs/decisions/ADR-0013-bounded-conversation-context.md
  docs/decisions/ADR-0014-staged-conversation-attachments.md
  docs/decisions/ADR-0015-durable-tool-approval.md
  docs/decisions/ADR-0016-terminal-coin-cat-identity.md
  docs/progress/goal-status.md
  docs/progress/feature-matrix.md
  docs/handoffs/2026-07-19-cli-goal-a-handoff.md
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

for marker in \
  "## 2. 仓库现状审计" \
  "## 3. 目标架构" \
  "## 4. 数据模型演进" \
  "## 5. API 演进" \
  "## 9. 分 Goal 路线" \
  "## 12. Goal A 验收定义"; do
  require_text docs/plans/2026-07-19-001-nico-cli-first-class-interface-plan.md "$marker"
done

for candidate in \
  "候选 A：像素圆章（最终选择）" \
  "候选 B：纯 ASCII 猫币" \
  "候选 C：紧凑徽章"; do
  require_text docs/cli.md "$candidate"
done

require_text docs/cli.md "inspired by README cat style"
require_text docs/cli.md "NOT_IMPLEMENTED"

for goal in A B C D E F; do
  require_text docs/progress/goal-status.md "| CLI-$goal |"
done
require_text docs/progress/goal-status.md "| CLI-A | Verified | 100% |"

for feature in \
  '`nico chat`' \
  '`nico exec`' \
  '`nico run watch`' \
  "Conversation CRUD" \
  "ConversationTurn" \
  "resume / continue / history" \
  "CLI SSE streaming" \
  "Slash commands" \
  "Attachments" \
  "Conversation summary" \
  "Conversation ContextSnapshot" \
  "Tool approval workflow" \
  "Coin-cat logo" \
  "Rich terminal rendering" \
  "Config / profiles" \
  "JSON / no-color / non-TTY output" \
  "CLI audit / artifacts integration"; do
  require_text docs/progress/feature-matrix.md "$feature"
done

for adr in docs/decisions/ADR-001{1..6}-*.md; do
  for heading in 背景 问题 候选方案 最终选择 选择原因 代价 后续影响 可逆性; do
    require_text "$adr" "## $heading"
  done
done

python3 scripts/check-docs.py
git diff --check
bash -n scripts/*.sh

echo "PASS CLI Goal A repository assessment and architecture baseline"
