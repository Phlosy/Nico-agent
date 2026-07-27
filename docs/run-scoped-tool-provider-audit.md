# Run-scoped External Tool Provider：现状审计

- 日期：2026-07-27
- 分支：`feat/run-scoped-tool-provider-binding`
- 范围：Nico Agent 核心控制面、Runtime、Worker、Tool Gateway、安全传输、API 与 CLI
- 状态词：`SUPPORTED`、`PARTIAL`、`MISSING`、`UNSAFE`、`INTERNAL_ONLY`

本审计基于源码而非既有文档。行号对应本文件创建时的分支版本；后续实现可能使行号移动。

## 结论

Nico 已有坚实的本地 Tool Gateway：精确的 `name@version` 定义、AgentVersion 与 Tenant
策略交集、RuntimeSession 冻结 allowlist、持久化 ToolCall、幂等键、审批、超时、重试、
Secret 引用、租户事务、事件与审计。缺失的是“外部执行绑定”这一独立领域概念及其公开协议。

当前最危险的扩展点位于
`backend/src/nico_agent/tools/gateway.py:181-189`：Gateway 在读取任何 Run 绑定之前，先从
进程级 `ToolRegistry` 解析本地 executor。若在此处简单包一层远程调用，会形成 Provider
失败后落到同名本地 Tool 的可能性。新实现必须先解析不可变 Run Snapshot，外部绑定存在
时只能走该 Provider。

## 审计矩阵

| 项目 | 状态 | 源码证据 | 判断 |
|---|---|---|---|
| Tool 定义 | SUPPORTED | `backend/src/nico_agent/tools/contracts.py:62-139`；`backend/src/nico_agent/domain/models.py:2703-2757` | Pydantic 合同与租户持久化定义均包含精确版本、输入/输出 Schema、权限、风险、超时、重试、隔离和内容哈希。 |
| ToolVersion | PARTIAL | `backend/src/nico_agent/tools/contracts.py:78-96`；`backend/src/nico_agent/tools/registry.py:11-44` | `name@semver` 是稳定身份，但没有独立 ToolVersion 表；这不阻碍 Provider 绑定，且不应为每个 Provider 复制版本。 |
| Tool allowlist | PARTIAL | `backend/src/nico_agent/tools/policy.py:20-100,103-159`；`backend/src/nico_agent/runtime/service.py:349-376` | Tenant 与 AgentVersion allow/permission 取交集、Run 只能缩窄并 fail closed；但输入是未类型化 JSON，直到 Worker 首次 claim 才生成最终 snapshot。 |
| AgentVersion 中 Tool 冻结 | PARTIAL | `backend/src/nico_agent/domain/models.py:208-273`；`backend/src/nico_agent/runtime/service.py:349-377` | AgentVersion 保存 `tool_policy`，RuntimeSession 启动时生成 `tool_policy_snapshot`；Run 创建时尚未冻结 Provider 绑定。 |
| Run 创建 | PARTIAL | `backend/src/nico_agent/api_schemas.py:205-210`；`backend/src/nico_agent/control_plane.py:633-661,1052-1085` | 公开 Task Run 支持预算/超时，但不接受 `tool_bindings`，也不在创建事务内做 Provider 校验。 |
| Task 创建 | SUPPORTED | `backend/src/nico_agent/control_plane.py:540-599`；`backend/src/nico_agent/domain/models.py:749-811` | Task 强制 Tenant/Project 归属，并冻结 assignee；可为 Binding 提供 project scope。 |
| Direct Run | PARTIAL | `backend/src/nico_agent/conversations/service.py:409-426,665-679,918-940` | Conversation turn、compaction、retry 会直接构造 Run，绕过 `ControlPlaneService._new_run`；引入绑定时必须统一进入冻结服务。 |
| Worker 派发 | SUPPORTED | `backend/src/nico_agent/database.py:360-516`；`backend/src/nico_agent/worker.py:53-102` | PostgreSQL lease/claim/heartbeat 是唯一执行所有权，可承载外部调用恢复。 |
| Runtime Tool Resolver | PARTIAL | `backend/src/nico_agent/runtime/tools.py:103-158`；`backend/src/nico_agent/tools/gateway.py:146-179` | Runtime 只依赖 Gateway，边界正确；Gateway 目前仅枚举进程级本地 Registry。 |
| Tool Executor | SUPPORTED | `backend/src/nico_agent/tools/contracts.py:142-173` | 明确的执行上下文和结果协议；上下文缺少 project、agent version、Provider request identity。 |
| Tool Call Event | PARTIAL | `backend/src/nico_agent/tools/gateway.py:372-768,1190-1250` | 持久化 started/rejected/approval 等事件，但没有 binding/provider/request/deadline/trace 字段。 |
| Tool Result Event | PARTIAL | `backend/src/nico_agent/tools/gateway.py:949-1041` | 结果、usage、attempts 与终态事件已保存；缺 Provider protocol status、duration、execution id。 |
| Tool 权限 | SUPPORTED | `backend/src/nico_agent/tools/policy.py:103-142`；`backend/src/nico_agent/tools/gateway.py:1090-1127` | 精确 allow、permission、definition hash 和 implementation hash 均校验；外部绑定需要用 schema digest 替代本地 implementation hash。 |
| Tool approval | PARTIAL | `backend/src/nico_agent/domain/models.py:2834-2937`；`backend/src/nico_agent/tools/gateway.py:438-745` | durable approval 和 checkpoint 已有；`allowed_scope=run` 可复用，和新要求的“具体 call/arguments/attempt”存在冲突，只有显式策略才可继续复用。 |
| Tool timeout | SUPPORTED | `backend/src/nico_agent/tools/contracts.py:71`；`backend/src/nico_agent/tools/gateway.py:239-267`；`backend/src/nico_agent/runtime/executor.py:225-278` | Tool 与 Run 两层 timeout 已有；缺 binding override、Provider request deadline、cancel grace。 |
| Tool retry | UNSAFE | `backend/src/nico_agent/tools/gateway.py:231-323,408-508`；`backend/src/nico_agent/tools/contracts.py:142-173` | 结构化 policy/attempts 和本地唯一键已有，但 executor 收不到 request/idempotency/attempt；lease takeover 会重新执行非终态 call，外部副作用无法安全恢复。 |
| Tool credential | PARTIAL | `backend/src/nico_agent/tools/policy.py:47-59,126-138`；`backend/src/nico_agent/tools/secrets.py:23-38` | Snapshot 仅保留 Secret ref，执行时解析；当前只接受 `env:NICO_TOOL_SECRET_*`，没有 Provider 专用最小 scope 凭据。 |
| Secret 管理 | PARTIAL | `backend/src/nico_agent/tools/secrets.py:11-59` | env ref fail closed 且递归脱敏；没有 vault/KMS、短期令牌发行或系统化日志 sink 检查。 |
| Tenant 隔离 | SUPPORTED | `backend/src/nico_agent/database.py:166-265`；各查询均带 `tenant_id` | TenantContext、事务变量、RLS 与复合 FK 共同约束。新 Provider 表必须复用相同模式。 |
| Project 隔离 | PARTIAL | `backend/src/nico_agent/domain/models.py:749-811`；`backend/src/nico_agent/control_plane.py:635-639` | Run 经 Task 间接属于 Project；ToolDefinition/ToolCall 不含 project_id，外部 Provider scope 必须在创建时由 Task.project_id 校验并冻结。 |
| Run Snapshot | PARTIAL | `backend/src/nico_agent/domain/models.py:1089-1180`；`backend/src/nico_agent/runtime/service.py:377-410` | RuntimeSession 保存 tool/model/coordination/knowledge snapshot，但它在 Worker 首次执行时生成；Provider Binding 要在 Run 创建事务中立即冻结。 |
| AgentVersion publish | UNSAFE | `backend/src/nico_agent/agent_versions.py:38-143,243-247`；`backend/migrations/versions/20260717_0002_core_control_plane.py:341-359` | 应用层有 publish/supersede、content hash 和精确 version id；数据库仍授予 UPDATE 且没有 published-version immutable trigger，不能把应用约定当成数据库不变量。 |
| Run cancellation | PARTIAL | `backend/src/nico_agent/coordination/service.py:375-604`；`backend/src/nico_agent/runtime/executor.py:301-316` | Run/RuntimeSession/ToolCall 会终止，Worker ownership 丢失会 cancel runtime provider；ToolExecutor 无 cancel contract，不会向外部 Tool Provider 确认停止，也不记录可能残留副作用。 |
| Run timeout | PARTIAL | `backend/src/nico_agent/runtime/executor.py:225-280`；`backend/src/nico_agent/runtime/service.py:700-739` | Worker 每次 claim 用 `asyncio.timeout(run.timeout_seconds)` 并记录 `RunTimedOut`；恢复/审批后会重新获得完整相对 timeout，而非从绝对 Run deadline 扣除。 |
| Event / Audit | PARTIAL | `backend/src/nico_agent/domain/models.py:3595-3647`；`backend/src/nico_agent/tools/gateway.py:1160-1250` | 通用 append-only Event/Audit 可承载 Provider 事实；缺 Provider 专用事件、序列和 Secret payload validator。 |
| Trajectory | PARTIAL | `backend/src/nico_agent/runtime/contracts.py:400-480`；`backend/src/nico_agent/runtime/service.py:900-1190` | Runtime trajectory 保存 action/outcome；尚不能证明 binding resolution 与 Provider request/response 链。 |
| Public API | MISSING | `backend/src/nico_agent/domain_api.py:280-346`；`backend/src/nico_agent/api.py:256-269` | 只能创建/读取 Run 和观察 ToolCall；没有 ExternalToolProvider 注册、校验或生命周期 API。 |
| CLI | MISSING | `backend/src/nico_agent/cli/app.py:1772-1795`；`backend/src/nico_agent/cli/client.py:790-825` | CLI 能读 Run/事件并创建常规 Run，但没有 Provider 或 Binding 命令。 |
| Provider endpoint 安全 | PARTIAL | `backend/src/nico_agent/net/safe_http.py:20-310`；`backend/src/nico_agent/models/http_safety.py:55-156` | 已有 DNS pin、地址分类、端口、redirect 和 TLS 基础；通用 SafeHttpClient 只允许 GET/HEAD，外部执行需要安全 POST transport。 |
| 外部 Tool Provider | MISSING | 全仓无 `ExternalToolProvider` 或 `tool_binding_snapshot` | 需要新领域模型、协议、client、resolver 和 lifecycle。 |
| 本地回退保护 | UNSAFE | `backend/src/nico_agent/tools/gateway.py:181-189` | 当前先解析全局本地 executor。外部绑定实现必须改为 binding-first，且已绑定路径永不调用 Registry executor。 |

## 当前数据流

```text
AgentVersion.tool_policy + Tenant.settings.tool_policy
  -> Worker 首次执行
  -> RuntimeSession.tool_policy_snapshot
  -> Runtime provider 产出 RuntimeToolIntent
  -> GatewayRuntimeToolHandler
  -> ToolGateway.execute
  -> ToolRegistry.get(name, version)
  -> ToolDefinition/ToolCall/Approval 持久化
  -> 本地 ToolExecutor
  -> output schema 校验
  -> ToolCall + Event/Audit + RuntimeToolOutcome
```

## 目标数据流

```text
Provider Registry + verified capability
  -> POST Run(tool_bindings)
  -> 在同一事务校验 AgentVersion/Tenant/Project/schema/protocol/policy/budget
  -> Run.tool_binding_snapshot + durable RunToolBinding
  -> RuntimeToolIntent
  -> ToolGateway binding-first resolver
     -> external binding: authenticated ProviderClient only
     -> no binding: existing local ToolRegistry path
  -> response envelope + output schema 校验
  -> durable counters/attempt/request identity/Event/Audit/trajectory
```

## 设计前必须修复的缺口

1. 所有公开 Run 创建入口必须复用同一个 Binding freeze service。
2. Provider endpoint 只能通过 Registry 引用；Run 请求不得携带 URL 或明文 credential。
3. Snapshot 必须在 Run insert 的事务中生成，不能推迟到 Worker。
4. Tool Gateway 必须先判断 exact external binding；命中后禁止本地 fallback。
5. Provider request identity、调用计数、累计耗时和 binding lifecycle 必须持久化。
6. Run 取消必须尽力发送 Provider cancel，同时无条件完成本地终止。
7. Event/Audit/API 输出必须经过 credential 与敏感 header 脱敏。
8. Provider executor 必须接收稳定 request/idempotency/attempt；当前
   `ToolExecutionContext` 不包含这些值，lease takeover 会重放非终态 executor。
9. Run timeout 必须冻结成绝对 deadline，不能在 Worker recovery 后重置完整时长。

## 非目标

- 不引入 Terminal-Bench、Harbor 或 `nico-bench` 类型。
- 不允许外部进程轮询 ToolCall 后代执行。
- 不让调用方直接写数据库。
- 不给模型 endpoint、credential、binding token 或认证 header。
- 不改变 Tool Definition 的稳定身份。
