# CLI Goal A Handoff：仓库勘察与总体方案

日期：2026-07-19 UTC  
状态：Verified（100%）  
下一阶段：CLI Goal B——基础客户端

## 1. 本阶段目标

在不实现业务代码的前提下，完整审计 Nico 当前 CLI、Task、Run、RuntimeSession、Worker、Artifact、Approval、SSE 与 README 视觉素材，冻结 CLI 一等入口架构、服务端增量模型、终端 coin-cat 方案和 CLI-A–F 的验收顺序。

## 2. 实际完成内容

- 确认当前只有 `nico-api`、`nico-worker`、`nico-sandbox-runner`，没有 `nico` 用户 CLI。
- 确认 Task/Run/Worker/RuntimeSession 是可复用执行底座，CLI 不需要也不允许另建 Agent Loop。
- 确认 SSE 已支持 `Last-Event-ID`，Run Artifact 已有上传/列表/下载，但附件缺少 Run 前暂存。
- 确认现有 ContextSnapshot 可增量演进，AgentMessage 不能复用为用户 Turn。
- 确认 Run 状态已有 `waiting_for_approval`，但现有 Approval 仅服务 Memory/Skill 发布，不等价于工具审批。
- 冻结 Conversation、ConversationTurn、ContextSnapshot、ConversationAttachment 和 ToolApprovalRequest 的演进方式。
- 冻结 REST/SSE 薄客户端、Typer + prompt_toolkit + Rich + httpx + platformdirs 技术栈。
- 比较三个终端猫币候选，选择 19 列、9 行“像素圆章”，并定义 ASCII/无色/JSON 降级。
- 建立 CLI-A–F 进度与功能矩阵，所有未编码能力均标为 `NOT_IMPLEMENTED`。

## 3. 未完成内容

按 Goal A 阶段约束，以下内容有意未实现：`nico` 命令入口、profile/config、API client、Conversation/Turn 表与 API、chat、exec、run watch、slash commands、附件、summary、工具审批以及 coin-cat 的 Rich 代码。它们分别属于 CLI-B–F，不是 Goal A 遗漏。

## 4. 新增文件

- `docs/plans/2026-07-19-001-nico-cli-first-class-interface-plan.md`
- `docs/cli.md`
- `docs/decisions/ADR-0011-first-class-thin-cli-and-terminal-stack.md`
- `docs/decisions/ADR-0012-conversation-is-not-runtime-session.md`
- `docs/decisions/ADR-0013-bounded-conversation-context.md`
- `docs/decisions/ADR-0014-staged-conversation-attachments.md`
- `docs/decisions/ADR-0015-durable-tool-approval.md`
- `docs/decisions/ADR-0016-terminal-coin-cat-identity.md`
- `scripts/verify-cli-goal-a.sh`
- 本 Handoff 与 `artifacts/goals/cli-goal-a/20260719T100024Z/` 证据目录。

## 5. 修改文件

- `docs/progress/goal-status.md`：新增 CLI-A–F，CLI-A 标为 Verified，B–F 标为 Not Started。
- `docs/progress/feature-matrix.md`：新增 17 项 CLI 独立矩阵，不用设计状态冒充实现。

## 6. 数据库变更

无。Goal A 只记录未来追加迁移方案。数据库 head 仍为 `20260718_0016`。

## 7. API 变更

无。Conversation、Turn、附件与工具审批端点当前均为 `NOT_IMPLEMENTED`。已有 Run SSE 和 Run Artifact API 未改变。

## 8. CLI 命令变更

无。没有新增 `nico` entry point 或 `nico_agent/cli` 业务包。当前可执行入口仍只有服务端进程命令。

## 9. 测试命令

```bash
scripts/verify-cli-goal-a.sh
scripts/test.sh
python3 scripts/check-docs.py
npx --yes markdownlint-cli2@0.22.1 "*.md" "backend/*.md" "docs/*.md" ".github/*.md"
docker compose config --quiet
```

## 10. 测试结果

- CLI Goal A 专项验收：通过。
- 文档链接、发布面标记与凭据模式检查：通过。
- Markdown lint：通过。
- Shell 语法、Compose 配置和 `git diff --check`：通过。
- 既有仓库单元回归：后端 234 项、前端 11 项通过，前端 TypeScript 和 production build 通过。
- 本阶段没有数据库/API 代码变更，因此未重复执行 75 项真实基础设施集成与 Goal G–L E2E；其最近基线仍在 Goal L 证据中。

## 11. 已知问题

- 正式 API Key/JWT 尚未实现；Goal B profile 只能诚实标注开发租户 header 或未来 token，不能宣称当前具备生产认证。
- 现有 Artifact 只能绑定 Run；Goal E 前 `/attach` 不可用。
- `waiting_for_approval` 目前只有状态枚举与转换基础，没有 ToolApprovalRequest 或 Worker 审批唤醒。
- 当前 SSE 使用数据库轮询；足以作为第一版合同，低延迟通知优化不阻塞 CLI-C。
- Unicode block 字符在少数字体可能宽度不一致，Goal D 必须测试并保留 ASCII fallback。

## 12. 当前架构

CLI 目标架构是远程薄客户端：REST 创建/查询服务端对象，SSE 观看持久化 Event，Worker 独立领取 Run 并执行 Runtime。Conversation 跨多个 Turn/Run；RuntimeSession 仍严格属于一个 Run。完整历史保存在 PostgreSQL，模型只接收有界、可审计的 ContextSnapshot。

## 13. 下一阶段建议

CLI Goal B 只建立基础客户端：

1. 添加 `nico` project script 和 `nico_agent.cli` 包；
2. 实现 platformdirs/TOML profile、环境覆盖和 0600 权限；
3. 实现 httpx client、统一错误/请求 ID、human/json/no-color 输出；
4. 实现 version、health、doctor、config，以及 run/task/agent 基础只读或现有写命令；
5. 建立 CLI 单测和真实 API smoke；
6. 不提前创建 Conversation 表或实现 chat。

## 14. 禁止重复实现

- 不要在 CLI 内创建 Agent Loop、Runtime Provider、Tool Executor 或数据库连接。
- 不要把 RuntimeSession 或 AgentMessage 重命名/包装成 Conversation。
- 不要另建第二张 ContextSnapshot；应对现有表做可空增量扩展。
- 不要放松 Artifact.owner_run 约束来临时完成 `/attach`；按 ADR-0014 做受控暂存。
- 不要复用 Growth Approval 冒充 Tool Approval，也不要本地假审批。
- 不要重新选择 coin-cat；主版本和降级规则已由 ADR-0016 冻结。

## 15. 验收证据

最终证据：`artifacts/goals/cli-goal-a/20260719T100024Z/`。目录包含环境、仓库/API 能力扫描、测试与文档日志、coin-cat 三候选渲染、错误样例、验收摘要及 SHA-256 manifest。
