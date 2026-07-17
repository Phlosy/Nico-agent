# REST API

Goal F 在通用控制面 REST API 上增加受控 Memory/Skill 成长生命周期；SSE、正式认证和 SDK 仍属于后续 Goal。

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

生产环境明确拒绝上述开发 Header 和 bootstrap，返回 `403 DEVELOPMENT_TENANT_CONTEXT_DISABLED`。API Key/JWT 的正式身份、权限与限额在 Goal K 实现，因此当前控制面不得直接暴露到公网。

## Goal C–E 控制面

| 资源 | 方法与路径 | 行为 |
| --- | --- | --- |
| Tenant | `POST /api/v1/tenants/bootstrap` | 开发/测试初始化租户 |
| Project | `POST/GET /api/v1/projects` | 创建、列表 |
| Project | `GET/PATCH /api/v1/projects/{project_id}` | 读取、乐观锁更新 |
| Project | `POST /api/v1/projects/{project_id}/archive` | 归档 |
| Agent | `POST/GET /api/v1/agents` | 创建、列表 |
| Agent | `GET/PATCH/DELETE /api/v1/agents/{agent_id}` | 读取、更新；仅无引用 Draft 可物理删除 |
| Agent | `POST .../{agent_id}/clone\|archive\|restore` | 克隆、归档、恢复 |
| AgentVersion | `POST/GET .../{agent_id}/versions` | 创建不可变配置版本、列表 |
| AgentVersion | `POST .../versions/{version_id}/publish` | 发布并切换 active pointer |
| AgentVersion | `POST .../{agent_id}/rollback` | 回滚到历史版本 |
| Task | `POST /api/v1/tasks`、`GET /api/v1/tasks/{task_id}` | 创建、读取 |
| Task | `POST .../tasks/{task_id}/transition` | 显式状态转换/分配 |
| Run | `POST .../tasks/{task_id}/runs`、`GET .../runs/{run_id}` | 创建一次执行、读取 |
| Run | `POST .../runs/{run_id}/transition\|cancel\|retry` | 状态转换、取消；Failed/TimedOut 创建重试 Run |
| Runtime | `GET .../runs/{run_id}/runtime` | 读取 Provider/session/capability/checkpoint/usage 摘要 |
| Runtime | `GET .../runs/{run_id}/trajectory` | 读取终态规范化 Provider 轨迹；未完成时返回稳定错误 |
| ToolDefinition | `GET /api/v1/tool-definitions` | 读取当前租户已注册的精确版本、Schema、权限、风险和内容 Hash |
| ToolCall | `GET .../runs/{run_id}/tool-calls` | 按创建顺序读取脱敏参数、尝试、结果/错误和 usage；不公开执行租约 token |
| RunStep | `POST .../runs/{run_id}/steps` | 追加步骤 |
| RunStep | `POST .../steps/{step_id}/transition` | 步骤状态与结果转换 |
| Event | `GET .../runs/{run_id}/events` | 按 sequence 读取 Run 事件 |
| Audit | `GET /api/v1/audit?limit=100` | 读取当前租户审计记录 |

更新和状态命令使用 `expected_revision`。并发冲突返回 `409 REVISION_CONFLICT`，非法状态转换返回 `409 INVALID_STATE_TRANSITION`，跨租户读取与不存在资源统一返回 `404 RESOURCE_NOT_FOUND`。唯一键或引用冲突返回 `409 DATA_CONFLICT`。

Run 的 Provider 由不可变 AgentVersion 的 `run_config.runtime_provider` 选择。API 不直接调用 Provider；独立 Worker 领取 Pending Run 后推进状态。`POST .../cancel` 先提交数据库权威 Cancelled 并清除租约，Worker 随后合作取消 Provider，迟到结果不能覆盖终态。

API 不提供直接执行工具的端点。工具只能由拥有有效 Run 租约的 Worker 经 Gateway 发起；因此客户端不能绕过 AgentVersion 策略、幂等、审计或 Sandbox。开发 Header 仍不是认证。

## Goal F 成长域

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
