# CLI Goal D Handoff：exec、watch 与终端交互

日期：2026-07-19 UTC  
状态：Verified（100%）  
下一阶段：CLI Goal E——Conversation summary、上下文选择与附件

## 1. 本阶段目标

在不把 Agent Loop 移入终端的前提下，交付非交互 `nico exec`、可重连 `nico run watch`、首批 slash commands、执行事实专用 Rich 视图和终端 coin-cat，使 CLI 同时成为交互用户与自动化调用方的一等入口。

## 2. 实际完成内容

- 新增 `nico exec`：支持位置 prompt、严格 JSON `--input`、`--output`、命令级 `--json`、显式 AgentVersion 与 `--detach`。
- exec 复用 Conversation/Turn API；服务端仍在同一事务中创建 Turn、Task 和 Run，CLI 没有旁路执行端点。
- `--output` 使用同目录临时文件、`fsync`、原子替换和 `0600`，拒绝最终路径为符号链接。
- 新增 `nico run watch RUN_ID [--after SEQUENCE]`，复用 Last-Event-ID、sequence 去重、keep-alive 与终态 REST 校准。
- watch 中 Ctrl+C 只退出观察器，不取消远端 Run；chat 中 Ctrl+C 仍执行 Goal C 的权威树取消。
- 新增会话、状态、检查和控制四组共 24 条 slash commands，并提供 prompt_toolkit 补全与 `/help`。
- `/cancel` 与 `/retry` 使用现有 revisioned ConversationTurn API；通用 Run retry 仍不能绕过会话语义。
- 新增 Event、Markdown final、Plan、PlanStep、RunStep、ToolCall、Artifact 和 usage 专用 Rich renderer。
- `/inspect` 从现有租户 API 聚合 Run、Plan、Step、Tool、Artifact 和 usage，不读取数据库或导入服务端实现。
- 新增启动 header，显示 Agent、Version、Runtime、Project 和 Tool policy 摘要。
- 将 ADR-0016 选定的 19×9 蓝/金/白/深灰 Unicode coin-cat 落地；无色、窄终端、非 UTF-8 和 dumb terminal 降级为 5 行 ASCII；JSON 完全省略 Logo。
- JSON 输出最多保留 2,000 个 Event，并通过 `events_truncated` 明示是否截断，避免无界客户端内存。

## 3. 明确未完成

Conversation summary、recent-turn/context selection、`/compact`、上传暂存、`/attach`、`/download` 和 Artifact 引用属于 CLI Goal E，当前没有注册占位命令。

ToolApprovalRequest、Run `waiting_for_approval`、审批 API、Worker 挂起/唤醒和 CLI 决策属于 CLI Goal F。现有 Growth Approval 不能冒充 Tool Approval。

正式 API Key/JWT 仍是平台既有缺口；开发 Tenant Header 仅适用于本机或受信网络。

## 4. 新增文件

- `backend/src/nico_agent/cli/execution.py`
- `backend/src/nico_agent/cli/logo.py`
- `backend/src/nico_agent/cli/renderers.py`
- `backend/src/nico_agent/cli/slash.py`
- `backend/tests/unit/test_cli_execution.py`
- `backend/tests/unit/test_cli_logo.py`
- `backend/tests/unit/test_cli_renderers.py`
- `backend/tests/unit/test_cli_slash.py`
- `scripts/e2e-cli-goal-d.sh`
- `scripts/verify-cli-goal-d.sh`
- 本 Handoff 与 `artifacts/goals/cli-goal-d/20260719T111600Z/`。

## 5. 主要修改文件

- `cli/app.py`：注册 exec 与 run watch，增加 input/output/detach/命令级 JSON 合同。
- `cli/client.py`：增加 Plan、Step、ToolCall、Artifact、Context、children、messages、audit 和 Conversation patch 的稳定方法。
- `cli/chat.py`：复用统一 renderer，增加 slash parser、补全、会话切换、检查和 revisioned 控制。
- `cli/output.py`：向 renderer 暴露经过统一配置的 stream/no-color 能力。
- README、CLI/测试/实施计划/进度文档：更新当前能力和 Goal E/F 边界。

## 6. 数据库与 API 变更

本阶段没有数据库迁移，也没有新增服务端 API。exec、watch 和 slash inspection 全部复用 CLI-C 的 Conversation/Turn/SSE 以及平台既有 Run、Plan、Step、ToolCall、Artifact、Context、Coordination 和 Audit API。

Alembic head 保持 `20260719_0017`；完整 base→head downgrade/upgrade 仍通过。

## 7. CLI 合同

```text
nico exec [PROMPT] --project ID --agent ID [--version ID]
          [--input FILE] [--output FILE] [--json] [--detach]
nico run watch RUN_ID [--after EVENT_SEQUENCE] [--json]
```

detach 成功文档包含 `conversation_id`、`turn_id`、`task_id` 和 `run_id`。attached exec 与 watch 的 JSON stdout 始终是单一文档；human 模式以滚动事件显示，不使用全屏 TUI。

## 8. Slash commands

- 会话：`/help`、`/new`、`/continue`、`/resume`、`/history`、`/conversations`、`/title`、`/exit`。
- 状态：`/status`、`/agent`、`/version`、`/runtime`、`/usage`、`/context`。
- 检查：`/plan`、`/steps`、`/tools`、`/children`、`/messages`、`/artifacts`、`/audit`、`/inspect`。
- 控制：`/cancel`、`/retry`。

## 9. 测试结果

- CLI 专项单测：41 项通过。
- 完整后端单测：280 项通过。
- 前端组件测试：11 项通过；TypeScript 和 production build 通过。
- 真实 PostgreSQL/Redis/MinIO 集成：78 项通过；完整迁移往返通过。
- CLI-D Compose E2E：attached exec、JSON input/private output、detach、watch、cursor 续读、TTY/no-color/non-TTY/JSON、coin-cat、`/help` 和 `/inspect` 全部通过。
- CLI-B 与 CLI-C E2E 回归通过。
- 文档、Markdown、Shell、Compose、Ruff format/lint 和 `git diff --check` 通过。

## 10. 验收命令

```bash
.venv/bin/pytest backend/tests/unit/test_cli_*.py
scripts/e2e-cli-goal-d.sh
NICO_EVIDENCE_DIR=artifacts/goals/cli-goal-d/20260719T111600Z \
  scripts/verify-cli-goal-d.sh
```

## 11. 已知限制

- SSE 语义可靠但当前仍由服务端数据库轮询并发送 keep-alive；低延迟通知扇出可后续优化。
- JSON 为保护客户端内存最多保留 2,000 个 Event；`events_truncated=true` 时应通过 `run events` 或带 cursor 的 `run watch` 分段读取。
- Plan、ToolCall 和 Artifact 视图只展示当前 Run 已持久化的事实；确定性 Mock Run 可能显示空集合，这不是伪造数据。
- 非交互 human 重定向使用紧凑无色 Logo；自动化应使用 `--json` 获得严格机器输出。

## 12. 当前架构

CLI 仍是 remote-only 薄客户端。exec/chat 创建 Conversation/Turn，服务端原子建立 Task/Run；Worker 是唯一执行者。watch/chat 读取持久化 SSE Event，inspection 读取只读 REST API。CLI 不连接 PostgreSQL、Redis、MinIO，不导入 Runtime、Tool Gateway、Worker 或领域 ORM，也不持有最终状态。

## 13. 下一阶段建议

CLI Goal E 应在当前 Conversation/Turn 和 ContextSnapshot 关联上继续：

1. 先冻结 token budget、summary hash 和 recent-turn 选择算法；
2. 由服务端 Model Gateway 生成可审计 summary，并将 ModelCall/ContextSnapshot 关联落库；
3. 设计受控暂存上传，再在 Turn 创建时物化为 Run Artifact；
4. 实现 `/compact`、`/attach`、`/download` 与 RLS/大小/hash/文件名安全验收；
5. 保持 Tool Approval 严格留在 CLI Goal F。

## 14. 禁止重复实现

- 不要在 CLI 中创建 Agent Loop、数据库连接、本地任务队列或本地最终状态。
- 不要重建 Conversation/Turn、SSE parser、profile、HTTP client、Output 或 Goal D renderer。
- 不要让 exec 直接调用 Runtime，也不要为 watch 创建第二套事件协议。
- 不要用本地目录直连替代受控 Artifact 上传。
- 不要把 Growth Approval 当作 Tool Approval。

## 15. 验收证据

最终证据目录：`artifacts/goals/cli-goal-d/20260719T111600Z/`。包含总验收日志、环境与命令说明、attached/detached/watch JSON、TTY/非 TTY/slash 渲染、错误记录、摘要和 SHA-256 manifest。
