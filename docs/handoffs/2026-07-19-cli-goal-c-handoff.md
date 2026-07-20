# CLI Goal C Handoff：Conversation 与 chat 基础能力

日期：2026-07-19 UTC  
状态：Verified（100%）  
下一阶段：CLI Goal D——exec、watch 与终端交互优化

## 1. 本阶段目标

在不把 Agent Loop 移入终端的前提下，交付持久化 Conversation/ConversationTurn、ContextSnapshot 关联、REST API 和最小可用 `nico chat`，使用户能跨 CLI 进程持续对话、观看服务端 Run Event 并权威取消当前执行。

## 2. 实际完成内容

- 新增独立 Conversation 和 ConversationTurn 平台对象；Conversation 创建时固定精确 AgentVersion。
- 一个数据库事务内依次落库 Turn、Task、首次 Pending Run、Event、Audit 和 Conversation last-turn pointer，不暴露部分链路。
- Run 保持执行权威；数据库触发器将状态、输出、usage、error 与 artifact refs 投影到 Turn。
- ContextSnapshot 增加可空 Conversation/Turn、selected turns、summary hash、artifact refs 和 token budget 字段；旧 Run 无需虚假回填。
- 所有新表启用 FORCE RLS、租户复合外键、不可变身份触发器、状态/顺序/幂等约束。
- 新增 Conversation 创建/列表/读取/更新、Turn 创建/列表/读取和权威取消 API。
- 实现最新 Failed/TimedOut Turn 的 revisioned retry：新 Run attempt 保持冻结 AgentVersion，Task/Turn/Event/Audit 原子更新；通用 Run retry 不得绕过 Conversation 语义。
- 新增增量 SSE parser、`Last-Event-ID` 续读、sequence 去重和流后 REST 终态校准。
- 实现 `nico chat` 新建、`--resume`、`--continue`、`--read-only`、一条消息模式与滚动式 prompt。
- 实现 `nico conversation list|get|history`。
- 实现 Alt+Enter 换行、Ctrl+D 退出、空闲 Ctrl+C 清除输入，以及执行中 Ctrl+C 请求服务端 Run 树取消。
- chat 历史目录/文件权限为 `0700`/`0600`；JSON 模式输出单一无 ANSI 文档。

## 3. 未完成内容

`nico exec`、`nico run watch`、CLI `/retry`、完整 slash commands、Plan/Tool/Artifact/usage 专用渲染、header/coin-cat、Conversation summary、上下文裁剪、附件上传、`/compact` 和 ToolApprovalRequest 均未在本阶段实现。服务端 Turn retry 已完成；其交互命令与其他能力分别属于 CLI-D–F，不以占位命令冒充完成。

正式 API Key/JWT 仍是平台既有缺口；受信网络开发 Tenant Header 不能作为生产认证。

## 4. 新增文件

- `backend/migrations/versions/20260719_0017_conversations.py`
- `backend/src/nico_agent/conversations/{__init__,api,contracts,service}.py`
- `backend/src/nico_agent/cli/{chat,sse}.py`
- `backend/tests/unit/test_{conversation_contracts,cli_chat,cli_sse}.py`
- `backend/tests/integration/test_conversation_api.py`
- `scripts/e2e-cli-goal-c.sh`
- `scripts/verify-cli-goal-c.sh`
- 本 Handoff 与 `artifacts/goals/cli-goal-c/20260719T104013Z/`。

## 5. 修改文件

- `domain/models.py`、`domain/states.py`：Conversation 领域、Run/ContextSnapshot 关联和枚举。
- `runtime/service.py`、`model_api_schemas.py`：原生 Runtime 的 Conversation ContextSnapshot 关联与读取合同。
- `api.py`：Conversation router 和 Idempotency-Key CORS 支持。
- `cli/client.py`、`cli/app.py`：Conversation REST/SSE client 与命令树。
- README、API/架构/领域/状态机/CLI/测试/进度文档：当前能力与阶段边界。
- `test_api.py`、`test_cli_{app,client}.py`、`test_infrastructure.py`：OpenAPI、client/命令和迁移 head 回归。

## 6. 数据库变更

Alembic head 从 `20260718_0016` 升级为 `20260719_0017`。新增 `conversations`、`conversation_turns`，为 Run 增加可验证的 `(tenant_id, task_id, id)` 唯一键，并为 ContextSnapshot 增加可空会话关联与选择元数据。迁移支持完整 downgrade；旧 ContextSnapshot 保持可读。

## 7. API 变更

```text
POST  /api/v1/conversations
GET   /api/v1/conversations
GET   /api/v1/conversations/{conversation_id}
PATCH /api/v1/conversations/{conversation_id}
GET   /api/v1/conversations/{conversation_id}/turns
POST  /api/v1/conversations/{conversation_id}/turns
GET   /api/v1/conversation-turns/{turn_id}
POST  /api/v1/conversation-turns/{turn_id}/cancel
POST  /api/v1/conversation-turns/{turn_id}/retry
```

创建接口接受请求体或 1–200 字符 `Idempotency-Key` Header。取消使用当前 Run revision 并复用 CoordinationService 的树取消；retry 使用 expected Run ID/revision 并保留冻结 AgentVersion；没有建立 CLI 专用旁路执行端点。

## 8. CLI 命令变更

```text
nico chat [MESSAGE] --project ID --agent ID [--version ID] [--title TEXT]
nico chat [MESSAGE] --resume CONVERSATION_ID [--read-only]
nico chat [MESSAGE] --continue [--project ID] [--agent ID]
nico conversation list [--project ID] [--agent ID] [--status STATUS]
nico conversation get CONVERSATION_ID
nico conversation history CONVERSATION_ID
```

全局 `--json` 的一条消息模式适合机器调用，但不替代后续 `nico exec` 的 detach/input/output 合同。

## 9. 测试命令

```bash
.venv/bin/pytest backend/tests/unit/test_conversation_contracts.py \
  backend/tests/unit/test_cli_sse.py backend/tests/unit/test_cli_chat.py \
  backend/tests/unit/test_cli_client.py backend/tests/unit/test_cli_app.py
RUN_INTEGRATION=1 .venv/bin/pytest backend/tests/integration/test_conversation_api.py
scripts/e2e-cli-goal-c.sh
NICO_EVIDENCE_DIR=artifacts/goals/cli-goal-c/20260719T104013Z \
  scripts/verify-cli-goal-c.sh
```

## 10. 测试结果

- Conversation/CLI 专项单测：34 项通过。
- Conversation 真实数据库/API 集成覆盖双 Turn、RLS、冻结版本、幂等、取消、归档、失败 retry 和不可变约束。
- 真实 CLI E2E：新会话、第二 Turn、continue、resume、read-only、history、SSE、JSON 无 ANSI 和真实 SIGINT 服务端取消通过。
- 完整后端单测 268 项通过；前端 11 项、TypeScript 与 production build 通过；真实基础设施集成 78 项通过。
- 从 `20260719_0017` 完整 downgrade 到 base 再 upgrade、CLI-B 回归、文档/Markdown/Shell/Compose/diff 检查全部通过。

## 11. 已知问题

- 当前 SSE 仍以持久化 Event 轮询为基础；语义可靠，低延迟通知扇出可后续优化。
- 交互输出只提供基础 Run 生命周期和最终结果；高级 Rich Event 视图属于 CLI-D。
- Conversation summary 字段与 ContextSnapshot 关联 Schema 已落库，但 summary 生成、recent-turn 选择和 token 裁剪属于 CLI-E。
- 服务端 retry 已实现，但 CLI `/retry` 尚未实现；普通 Run retry 会拒绝 Conversation Task，不能绕过 Turn API。
- POSIX 历史权限已验证；非 POSIX ACL 尚需目标平台验收。

## 12. 当前架构

终端通过 REST 创建/查询 Conversation 和 Turn，通过 SSE 读取持久化 Run Event。服务端在一个事务中建立 Turn→Task→Run；Worker 独立领取 Run 并调用 Runtime。Run/RuntimeSession 是单次执行事实，Conversation 跨多个 Run，Turn 是 Run 的用户视图投影。CLI 断开只影响显示，不影响队列、租约、checkpoint、Event、Audit 或最终结果。

## 13. 下一阶段建议

CLI Goal D 应直接复用现有 client、SSE parser、ChatRunner 和输出层：

1. 实现 `nico exec` 与 detach 输出合同；
2. 实现 `nico run watch`，复用同一 Last-Event-ID/去重逻辑；
3. 增加 Plan/Step/Tool/Artifact/usage 只读视图和第一批 slash commands；
4. 落地 Rich header、README 风格终端 coin-cat 及 TTY/窄终端/无色降级；
5. 保持 summary、附件和审批分别在 CLI-E/F。

## 14. 禁止重复实现

- 不要在 CLI 内创建 Agent Loop、Runtime Provider、数据库连接或本地最终状态。
- 不要把 RuntimeSession、AgentMessage 或 prompt_toolkit history 当作 Conversation。
- 不要重建 Conversation/Turn 表、SSE parser、配置、HTTP client、错误或基础输出层。
- 不要绕过 Run 树取消或数据库 Turn 投影。
- 不要在旧 Conversation 上静默切换 AgentVersion。
- 不要把尚未实现的 retry、summary、附件、审批或 coin-cat 标为完成。

## 15. 验收证据

最终证据目录：`artifacts/goals/cli-goal-c/20260719T104013Z/`。包含完整验收日志、环境/命令说明、CLI E2E JSON、SIGINT 取消结果、错误记录、API/终端说明、摘要与 SHA-256 manifest。
