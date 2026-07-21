# REST API

Nico REST API 提供持久化 Conversation/Turn、版本化 Agent、Task/Run、Runtime/Tool/ModelCall/ContextSnapshot、Plan/RuntimeEvaluation、动态协调、私有 Artifact、可续传 SSE，以及受控 Memory/Skill 生命周期。正式认证和 SDK 尚未实现。

## OpenAPI

- 规范：`GET /openapi.json`
- Swagger UI：`GET /docs`

## Liveness

```http
GET /api/v1/health/live
```

该接口不访问外部依赖。进程能够处理请求时返回 `200`：

```json
{
  "status": "alive",
  "service": "Nico Agent Platform",
  "version": "0.2.0"
}
```

## Readiness

```http
GET /api/v1/health/ready
```

API 并发探测 PostgreSQL、Redis 和 MinIO，每个探针有独立超时。全部可用返回 `200` 与 `ready`；任一不可用返回 `503` 与 `not_ready`，同时保留各组件结果：

```json
{
  "status": "ready",
  "checked_at": "2026-07-16T15:37:25.580402Z",
  "components": {
    "postgres": {"status": "up", "latency_ms": 4.683, "detail": null},
    "redis": {"status": "up", "latency_ms": 1.785, "detail": null},
    "minio": {"status": "up", "latency_ms": 3.023, "detail": null}
  }
}
```

所有 HTTP 响应（包括未处理的 500）包含 `X-Request-ID`。调用方提供的 ID 只有在满足安全字符与长度限制时才被沿用，否则服务生成 UUID。依赖失败只公开异常类别，不回显可能含凭据的异常原文；未处理的 500 使用固定安全响应。应用与 Uvicorn 运行日志使用单行 JSON；Alembic CLI 保留其标准迁移日志格式。

## 开发租户上下文

开发和测试环境的领域请求必须携带：

```http
X-Tenant-ID: <uuid>
X-Actor-ID: <optional actor; defaults to development-user>
```

请求体不接受 `tenant_id`。API 将 Header、请求关联 ID 组装为 `TenantContext`，数据库事务再应用 `nico_runtime` 与 PostgreSQL RLS。`POST /api/v1/tenants/bootstrap` 是仅限开发/测试的租户初始化入口，不需要 Tenant Header。

生产环境明确拒绝上述本地 Header 和 bootstrap，返回 `403 DEVELOPMENT_TENANT_CONTEXT_DISABLED`。API Key/JWT 的正式身份、权限与限额尚未实现，因此当前控制面不得直接暴露到公网。

## 核心控制面

| 资源 | 方法与路径 | 行为 |
| --- | --- | --- |
| Tenant | `POST /api/v1/tenants/bootstrap` | 开发/测试初始化租户 |
| Tenant | `PATCH /api/v1/tenant/settings` | 使用 revision 更新租户工具/协调等策略 |
| Project | `POST/GET /api/v1/projects` | 创建、列表 |
| Project | `GET/PATCH /api/v1/projects/{project_id}` | 读取、乐观锁更新 |
| Project | `POST /api/v1/projects/{project_id}/archive` | 归档 |
| Project collaboration | `POST /api/v1/projects/collaboration/preflight` | 在写入前验证 Lead runtime、协调 policy 和成员版本 |
| Project collaboration | `POST /api/v1/projects/collaboration` | 原子创建 shared Project、Lead/Member 和稳定 Session |
| Project member | `GET/POST .../projects/{project_id}/members` | 列表或 revision-safe 添加成员 |
| Project member | `POST .../projects/{project_id}/members/{agent_id}/state` | 暂停、恢复或移除成员 |
| Project lead | `POST .../projects/{project_id}/lead` | 原子替换唯一 active Lead |
| ProjectSession | `GET .../projects/{project_id}/sessions` | 列出成员稳定 Session |
| ProjectSession | `GET/POST .../sessions/{session_id}`、`.../open` | 读取或解析当前冻结 Conversation |
| ProjectSession | `GET .../sessions/{session_id}/timeline` | 使用 Event sequence 分页读取有界工作投影 |
| ProjectSession | `POST .../sessions/{session_id}/messages` | 在可写成员 Session 创建 Conversation Turn |
| Supervision | `POST .../projects/{project_id}/supervision/sync` | 以 Idempotency-Key 请求手动 Lead 周期 |
| Supervision | `PATCH .../projects/{project_id}/supervision/cadence` | revision-safe 设置 5 分钟至 7 天 cadence 或关闭 |
| Supervision | `GET .../projects/{project_id}/supervision/cycles` | 读取持久化周期、数据库 metrics 和可选 narrative |
| Intervention | `POST/GET .../sessions/{session_id}/runs/{run_id}/interventions` | 创建或查询有界 local guidance |
| Intervention | `POST .../interventions/{intervention_id}/withdraw` | revision-safe 撤回 pending guidance |
| Project change | `POST .../sessions/{session_id}/project-changes` | 把范围变化作为 Lead Session 新 Turn 重新规划 |
| Agent | `POST/GET /api/v1/agents` | 创建、列表 |
| Agent | `GET/PATCH/DELETE /api/v1/agents/{agent_id}` | 读取、更新；仅无引用 Draft 可物理删除 |
| Agent | `POST .../{agent_id}/clone\|archive\|restore` | 克隆、归档、恢复 |
| AgentVersion | `POST/GET .../{agent_id}/versions` | 创建不可变配置版本、列表 |
| AgentVersion | `POST .../versions/{version_id}/publish` | 发布并切换 active pointer |
| AgentVersion | `POST .../{agent_id}/rollback` | 回滚到历史版本 |
| Conversation | `POST/GET /api/v1/conversations` | 创建固定 AgentVersion 的会话；按 project/agent/status 分页列表 |
| Conversation | `GET/PATCH /api/v1/conversations/{conversation_id}` | 读取，或用 revision 修改标题/归档 |
| ConversationTurn | `GET/POST .../conversations/{conversation_id}/turns` | 读取历史；原子创建 Turn、Task 与首次 Pending Run |
| ConversationTurn | `GET .../conversation-turns/{turn_id}` | 读取 Run 权威状态投影、输出、usage、artifact refs 与错误 |
| ConversationTurn | `POST .../conversation-turns/{turn_id}/cancel` | 使用 Run revision 调用统一树取消服务 |
| ConversationTurn | `POST .../conversation-turns/{turn_id}/retry` | 对最新 Failed/TimedOut Turn 创建冻结版本的新 Run attempt |
| Conversation | `POST .../conversations/{conversation_id}/compact` | 创建受审计的摘要 Task/Run，返回覆盖序号与输入 Hash |
| ConversationAttachment | `POST/GET .../conversations/{conversation_id}/attachments` | 二进制暂存并列出下一轮附件；请求不接受服务端路径 |
| ConversationAttachment | `DELETE .../conversations/{conversation_id}/attachments/{attachment_id}` | 删除尚未消费的暂存件 |
| Task | `POST /api/v1/tasks`、`GET /api/v1/tasks/{task_id}` | 创建、读取 |
| Task | `POST .../tasks/{task_id}/transition` | 显式状态转换/分配 |
| Run | `POST .../tasks/{task_id}/runs`、`GET .../runs/{run_id}` | 创建一次执行、读取 |
| Run | `POST .../runs/{run_id}/transition\|cancel\|retry` | 状态转换、取消；Failed/TimedOut 创建重试 Run |
| Runtime | `GET .../runs/{run_id}/runtime` | 读取 Provider/session/capability/checkpoint/usage 摘要，包含解析来源、legacy 标记和兼容元数据 |
| Runtime | `GET .../runs/{run_id}/trajectory` | 读取终态规范化 Provider 轨迹；未完成时返回稳定错误 |
| Runtime | `GET .../runs/{run_id}/events/stream` | 使用 `Last-Event-ID` 续传 Run Event SSE |
| Model | `GET .../runs/{run_id}/model-calls` | 读取脱敏模型调用、usage 与 cost |
| Model | `GET .../runs/{run_id}/contexts` | 读取 ContextSnapshot |
| Knowledge usage | `GET .../runs/{run_id}/knowledge-usages` | 读取冻结的 Memory/Skill 版本、Context/ModelCall 引用计数与终态效果；不返回内容正文 |
| Model | `GET/POST/PATCH .../model-endpoints` | 读取或在部署允许时管理端点修订与运行状态 |
| Plan | `GET .../runs/{run_id}/plans` | 按 revision 读取不可覆盖的 Plan 历史 |
| Plan | `GET .../runs/{run_id}/plans/{plan_id}` | 读取指定历史 revision；旧 revision 在 Replan 后仍可访问 |
| PlanStep | `GET .../runs/{run_id}/plans/{plan_id}/steps` | 按 position 读取依赖、状态、输出 Hash 与证据引用 |
| RuntimeEvaluation | `GET .../runs/{run_id}/runtime-evaluations` | 按 sequence 读取步骤验证、Reflection 与 Completion 事实 |
| Coordination | `GET .../runs/{run_id}/delegations\|children\|messages` | 查询 Parent/Child 树、委托策略/预算与消息历史 |
| Coordination | `POST .../runs/{run_id}/tree-cancel` | 使用 revision 幂等取消 Root 及全部后代 |
| Coordination | `POST .../delegations/{delegation_id}/retry-request` | Child 以幂等键向 Parent 发送有界重试请求 |
| Artifact | `POST .../runs/{run_id}/artifacts` | 上传有大小上限的私有内容；Runtime 通常使用 `store_artifact` handler |
| Artifact | `GET .../runs/{run_id}/artifacts` | 只列出 Run 自有或持有有效共享链接的元数据 |
| Artifact | `GET .../runs/{run_id}/artifacts/{artifact_id}/content` | 复核 Run 授权后下载；不公开 MinIO 对象键或凭据 |
| ToolDefinition | `GET /api/v1/tool-definitions` | 读取当前租户已注册的精确版本、Schema、权限、风险和内容 Hash |
| ToolCall | `GET .../runs/{run_id}/tool-calls` | 按创建顺序读取脱敏参数、尝试、结果/错误和 usage；不公开执行租约 token |
| ToolApprovalRequest | `GET /api/v1/tool-approval-requests?run_id=...&status=...` | 列出当前租户的持久化工具审批；参数已经服务端脱敏 |
| ToolApprovalRequest | `GET /api/v1/tool-approval-requests/{approval_id}` | 读取风险、期限、授权范围、revision 与终态决定 |
| ToolApprovalRequest | `POST .../tool-approval-requests/{approval_id}/decision` | 使用 `Idempotency-Key`、`expected_revision` 批准 once/run 或拒绝 |
| RunStep | `GET .../runs/{run_id}/steps` | 按 sequence 读取持久化执行步骤，供只读 Inspector 和审计使用 |
| RunStep | `POST .../runs/{run_id}/steps` | 追加步骤 |
| RunStep | `POST .../steps/{step_id}/transition` | 步骤状态与结果转换 |
| Event | `GET .../runs/{run_id}/events` | 按 sequence 读取 Run 事件 |
| Audit | `GET /api/v1/audit?limit=100` | 读取当前租户审计记录 |

更新和状态命令使用 `expected_revision`。并发冲突返回 `409 REVISION_CONFLICT`，非法状态转换返回 `409 INVALID_STATE_TRANSITION`，已决定的工具审批收到不同决定返回 `409 TOOL_APPROVAL_ALREADY_DECIDED`，跨租户读取与不存在资源统一返回 `404 RESOURCE_NOT_FOUND`。唯一键或引用冲突返回 `409 DATA_CONFLICT`。

Conversation 与 Turn 创建支持请求体 `idempotency_key`，也支持长度 1–200 的 `Idempotency-Key` Header；同一个键携带不同输入会被拒绝。`mode=personal` 不接受显式 shared Project，而是按 Tenant/actor 幂等解析隐藏 Personal Project；默认 Project 列表不返回该系统资源。`mode=project` 保持旧显式 Project 路径，并在 ProjectSession 下再次校验 active membership。Conversation 创建时固定精确 AgentVersion，后续新版本发布不会改写已有会话。每个 Turn 对应一个新 Task 和首次 Run；同一 Conversation 同时只允许一个非终态 Run。`cancel` 不在 CLI 本地篡改 Turn，而是先提交 Run 树的权威取消，再由数据库投影更新 Turn。

Project timeline 是原始 Event、ConversationTurn、Task/Run、Plan、ToolCall、Delegation
和 Artifact 引用的有界查询投影，不是第二套执行状态。响应不包含 trajectory/raw
reasoning、对象存储键或 Secret。Intervention 内容按不可信输入处理，必须匹配活动
Run revision 和 active membership；Direct/Hermes 等未声明 capability 的 Runtime
明确拒绝，而不会静默丢弃。

Turn retry 请求必须提供当前 `expected_run_id` 和 `expected_run_revision`，只允许最新 Turn 的 Failed/TimedOut Run。服务在一个事务中创建 `retry_of_run_id` 指向旧 Run 的新 attempt、恢复 Task、切换 Turn current Run 并追加 Event/Audit；AgentVersion 继续使用 Conversation 冻结版本。并发重复请求在 current Run 已改变后失败，不会创建第二个 retry。Conversation 所属 Task 的通用 `/runs/{id}/retry` 被明确拒绝，调用方必须使用 Turn API，避免绕过冻结版本和 Turn 指针。

ConversationTurn 状态是面向会话的投影视图：`accepted/queued/running/waiting_for_approval/completed/failed/cancelled`；底层 `run_status`、`run_revision` 仍一并返回。完整历史保存在 PostgreSQL。每个模型上下文按预算选择 summary、最近完成 Turn、有界 Artifact 摘要/引用及已治理知识，并把 `conversation_id`、`conversation_turn_id`、`selected_turn_ids`、summary Hash、Artifact 引用、token budget 与裁剪事实写入 ContextSnapshot。摘要由独立 Task/Run 生成，模型支持时对应单独 ModelCall；非 Conversation Run 保持兼容。

Run 的 Provider 优先由不可变 AgentVersion 的 `runtime_provider` 选择。旧数据继续依次兼容 `run_config.runtime_provider`、`model_config.runtime_provider` 和历史 Mock 缺省；这些分支会写入 `LegacyRuntimeProviderResolved` Event/Audit，并声明在 `0.4.0` 移除。恢复时 RuntimeSession 已持久化的 Provider/version/protocol 是权威事实，不兼容或未启用时失败关闭，不会切换 Provider。API 不直接调用 Provider；独立 Worker 领取 Pending Run 后冻结 Tenant ∩ AgentVersion 的 Memory/Skill 策略和精确版本，再推进状态。`GET .../runtime` 只返回策略摘要，不返回含知识正文的 `knowledge_selection_snapshot`。Run cancel 使用同一个树取消服务，先提交 PostgreSQL 权威终态并清除所有后代租约，迟到结果不能覆盖终态。

Console Run Inspector 使用上述只读路由，但当前仍由浏览器发送本地 Tenant Header，因此只能用于 localhost/受信网络，不是生产身份或授权边界。

API 不提供直接执行工具的端点。工具只能由拥有有效 Run 租约的 Worker 经 Gateway 发起；审批 API 只决定已经持久化的请求，不能创建 ToolCall。批准不会绕过 AgentVersion 策略、幂等、审计或 Sandbox。开发 Header 仍不是认证。

## 受控成长域

API 不提供任意创建正式 Memory 或 Skill 的端点。唯一自动入口是 `POST /api/v1/runs/{run_id}/growth-candidates`，且只接受当前租户已终态化、来源闭合的 Run；结果始终是 Candidate/Draft。发布仍必须依次通过确定性 Evaluation 和独立人工 Approval。

| 资源 | 方法与路径 | 行为 |
| --- | --- | --- |
| Candidate | `POST /api/v1/runs/{run_id}/growth-candidates` | 从终态 Run 幂等生成 Memory/Skill 候选 |
| Memory | `GET /api/v1/memories`、`GET /api/v1/memories/{memory_id}` | 按状态、类型、scope 列表及读取不可变版本 |
| Memory | `POST /api/v1/memories/search` | 在 project/agent 上下文中 scope-first 向量检索 Active Memory |
| Memory | `GET .../{memory_id}/sources\|evaluations\|approvals` | 查询脱敏来源摘要、验证和审批历史 |
| Memory | `POST .../{memory_id}/evaluations\|approvals\|publish` | 验证、申请独立审批并发布/索引 |
| Memory | `POST .../{memory_id}/revisions\|invalidate\|expire`、`DELETE .../{memory_id}` | 创建不可变修订、失效、到期或 tombstone |
| Approval | `GET /api/v1/growth-approvals/{approval_id}` | 查询审批 |
| Approval | `POST .../{approval_id}/decision\|cancel` | 独立批准/拒绝或由申请者取消 |
| Skill | `GET /api/v1/skills`、`GET /api/v1/skills/{skill_id}` | 列表及读取稳定身份/current pointer |
| SkillVersion | `GET .../{skill_id}/versions`、`GET .../versions/{version_id}` | 查询不可变版本及其来源/验证/审批 |
| SkillVersion | `GET .../{skill_id}/versions/compare` | 比较八个内容区、工具集合和版本方向 |
| SkillVersion | `POST .../revisions\|evaluations\|approvals\|publish` | 创建 Draft、验证、审批和发布版本 |
| Deployment | `GET/POST .../{skill_id}/deployments` | 查询或创建 project/agent 级 1–99% canary |
| Deployment | `POST .../deployments/{deployment_id}/retire` | 退役 canary，保留历史 |
| Resolution | `GET .../{skill_id}/resolve?run_id=...` | 从持久化 Run scope 稳定解析 stable/canary 版本 |
| Skill | `POST .../{skill_id}/promote\|rollback\|deprecate\|disable` | 切换稳定指针、回滚或停止 Skill |

所有写命令使用 `expected_revision` 或显式的 Skill/SkillVersion revision。请求者自审返回 `403 APPROVAL_SELF_REVIEW_FORBIDDEN`；跨租户资源与不存在资源统一为 `404`；乐观锁冲突为 `409`；请求 Schema 错误为 `422`。来源响应只包含可审计 ID、Hash 和生成器版本，不公开内部 trajectory snapshot、embedding、原始工具 arguments 或 Provider 状态。

## 当前安全边界

健康接口仍未启用认证，只应暴露在开发或受信运维网络。生产身份、细粒度授权、限额和 Secret 管理尚未完成；开发 Tenant Header 不是认证机制。
