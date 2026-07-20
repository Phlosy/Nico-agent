# Runtime Provider 与持久化 Worker

## 稳定边界

`AgentRuntimeProvider` v1 与 `AgentRuntimeProviderV2` 是执行后端共同遵循的异步协议。v2 用 `execute` 返回 terminal 或 suspended outcome，并只接收显式的 Tool、Coordination、Artifact capability handler；Provider 不接收 ORM、数据库 Session 或控制面服务。v1 Provider 由兼容包装层转换为 terminal outcome，历史版本无需数据回写。

能力通过 `RuntimeProviderDescriptor.capabilities` 明示，`implementation` 区分 native、adapter 与 test，`compatibility` 声明可恢复的 Provider/协议版本和执行模式。缺少能力必须抛出稳定的 `RUNTIME_CAPABILITY_UNSUPPORTED`，不能返回伪成功。事件使用单调 `sequence` 和平台枚举；应用服务检查连续性、幂等忽略已提交事件，再映射为 RunStep/Event/Audit。

## Provider 选择

新建 AgentVersion 的正式默认值是 `runtime_provider=nico_native` 和 `execution_mode=direct`。为兼容历史数据，旧版本仍依次读取 `run_config.runtime_provider`、`model_config.runtime_provider`，均缺省时解析为 `mock`；显式选择不会被静默替换。每次解析把 source、legacy 标记和兼容元数据写入 RuntimeSession；legacy 分支追加 `LegacyRuntimeProviderResolved` Event/Audit。生产 Worker 不注册 Mock，避免测试执行器被误用为真实推理。

恢复时，已经持久化的 `RuntimeSession.provider_name/provider_version/protocol_version` 是权威事实。Worker 必须查找同名 Provider，并按 descriptor 的 resume compatibility 校验；缺失、禁用或不兼容会失败关闭，不能换成 Native。历史 RuntimeSession 的 Provider/版本字段不会被 migration 或恢复逻辑重写。

### Provider capability matrix

| Provider | 协议/实现 | Direct | Resume/Cancel/Status | Platform Tool | Planning/Reflection | Coordination/Artifact | Usage |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `nico_native 0.2.0` | v2 / native | 是 | 是 | Tool Gateway | 是 | 是 | ModelCall exact/partial 状态 |
| `mock 1.0` | v1 terminal shim / test | 确定性测试 | 是 | Tool Gateway | 测试合同 | 测试合同 | 确定性测试值 |
| `hermes 0.18.2` | v2 / subprocess adapter | 是 | 是；不声明运行中 pause | 仅 Nico MCP → Tool Gateway | 否 | 否 | CLI 0.18.2 标记为 unavailable，绝不伪造 |

## Nico Native Direct、ReAct 与 Plan-and-Execute

`nico_native` 是 Nico 自带、无需 Hermes 的 Runtime。Direct 模式构造一个确定性 ContextSnapshot，调用一次 OpenAI-compatible 流式模型接口，合并有界输出增量，保存 ModelCall/usage/cost/checkpoint，并以结果或稳定错误终结 Run。

ReAct 模式执行有界的“模型推理 -> Tool Gateway -> 工具观察 -> 下一轮推理”循环。模型只能调用冻结策略中授权的精确 `name@version` 工具；每轮受 `max_iterations`、`max_tool_calls` 和 token budget 限制。模型协议错误、无权限工具、空最终输出和预算耗尽都会以稳定错误失败关闭。Direct 模式仍不执行 Tool Call，收到此类输出会以 `MODE_CAPABILITY_VIOLATION` 失败。

ReAct 在工具外部作用前保存带完整性 Hash 的 schema v2 checkpoint，工具成功后先提交 ToolCall/RunStep/Event/Audit，再保存观察 checkpoint。Worker 接管过期租约时会把未终结 ModelCall 标为 `interrupted`，使用新的 replay call key 重试；ToolCall 使用稳定 idempotency key 查询权威结果，已成功的文件、Python 或其他副作用不会再次执行。每个执行尝试使用独立的进程内 session id，旧租约持有者的迟到 cancel/release 不会误伤接管者。

medium/high ToolCall 在副作用前创建 ToolApprovalRequest，并让 Native ReAct/Plan 返回 suspended outcome。Worker 保存同一 checkpoint、清除租约并进入 `waiting_for_approval`。approved/rejected/expired 决定把 Run 置回可领取状态；恢复时 approved 调用继续执行，其他终态作为失败工具观察返回给模型。决策可能早于 Worker 最后一笔 suspension 写入，因此挂起逻辑会重读持久化审批状态，避免丢失快速唤醒。审批行锁不参与 Run 锁顺序，防止操作者决策与挂起事务死锁。

Plan-and-Execute 使用结构化 Planner 生成最多由预算约束的 DAG，每次初始规划或 Replan 都追加独立 Plan revision，旧 revision 不覆盖。每个 PlanStep 产生独立 ModelCall、RunStep 与确定性 step validation；步骤可以调用冻结策略授权的精确版本工具，并复用同一个 Tool Gateway、权限交集、幂等键和审计链。工具副作用前强制保存 schema v3 checkpoint，工具 RunStep 挂到对应 Plan 执行步骤。失败后 Reflection 只能返回 `retry`、`replan` 或 `fail`。Replan 会终结旧 revision 并创建新 revision，不修改已经完成的步骤事实。

所有步骤结束后先按 Task acceptance（包括 `non_empty`、required fields 和可选 JSON Schema）执行确定性 Completion Evaluation。只有确定性检查通过，部署才可按 AgentVersion 的 `run_config.completion_model_judge=true` 启用独立模型 judge；judge 使用单独 ContextSnapshot、ModelCall、usage/cost 和 RuntimeEvaluation，生成结果的 ModelCall 不能给自己证明完成。最终 Run 结果同时包含 `result` 和 `execution_summary`。

Plan 模式使用紧凑、带完整性 Hash 的 schema v3 checkpoint，只保存当前 revision、步骤游标、完成键、当前步骤的有界工具状态、最新输出、恢复指令、Reflection 决策与 usage。完整 Plan 历史在 `plans`、`plan_steps` 和 `runtime_evaluations` 中；Worker 接管时从这些权威事实重建 active Plan，通过 replay call key 防止把已提交模型调用再次计费，并以稳定工具幂等键复用崩溃前已经产生的副作用。

## 动态多 Agent 协作

ReAct 在协调策略启用时获得保留工具 `delegate_agent`，可在同一模型轮次提出多个并行委托。每个请求都经 Coordination handler 校验目标版本、父子深度、数量/并行度、重复与循环，并以事务锁定 Parent 的 `RunBudgetLedger` 后预留 Child 预算。Child 的模型、工具、Secret 引用和继续委托能力只能取 Tenant、Parent 冻结快照、Child AgentVersion 与本次限制的交集。

委托成功后 Parent 保存包含 Child Run/Delegation 引用的 checkpoint，以 suspended outcome 进入 `waiting_for_subagent` 并释放 Worker 租约。Child 终结事务写入 result message、核销预算、附带可见 Artifact 引用，并在所有直接 Child 终结时唤醒 Parent。新 Worker 从 PostgreSQL 恢复消息并确认消费；通知丢失时由 reconciler 修复。取消 Parent 会幂等取消整棵 Run 树。

Child 可使用保留工具 `store_artifact` 把有界文本或 Base64 内容交给 Artifact handler。Provider 不接触 MinIO 凭据；服务完成临时上传、SHA-256 内容寻址、元数据提交与可选 direct-Parent share。Parent 恢复时只收到结构化结果和 `artifact:<id>` 引用。

模型端点按 Tenant 保存版本行，仅保存 `credential_ref`，不保存 Secret 值。Worker 发起请求时解析 `env:NICO_MODEL_SECRET_*`，默认强制 HTTPS，解析并固定连接到已校验 IP，拒绝 redirect、loopback、link-local、metadata 和私网地址。私网模型只能由部署级可信主机 allowlist 放开，AgentVersion 无权自行降低策略。

确定性 Mock 配置示例：

```json
{
  "runtime_provider": "mock",
  "mock": {
    "steps": ["plan", "execute"],
    "output": {"result": "ok"},
    "delay_seconds": 0,
    "fail": false
  }
}
```

Mock 的行为只由上述配置决定，支持暂停、恢复、取消、检查点、事件和轨迹，不能用于声明真实模型质量。

Mock 还支持用于确定性验收的 `mock.tool_calls`。每项必须给出精确工具名称、版本和参数；Mock 只生成规范化 tool intent，实际执行仍由 Worker 注入的 Tool Gateway handler 完成，不能直接调用 Executor。

## Hermes Adapter（可选）

Hermes 不是 Nico Native 的运行依赖。只有显式设置 `NICO_HERMES_ENABLED=true` 时 Worker 才注册 `HermesRuntimeProvider`；它直接返回协议 v2 terminal outcome，保留 `run()` 仅作为 v1 调用方的弃用 shim。Adapter 面向 Hermes `0.18.2`，采用 CLI 子进程而非 Python 内部类：

- 兼容：首次创建 session 前运行一次 `hermes version` 并缓存结果；非 0.18.2 或无法解析时 fail-closed；
- 执行：`hermes chat -q <envelope> -Q`，按配置映射 `--model`、`--provider` 和 `--resume`；有平台工具时只传 `--toolsets nico`；
- session：解析 stderr 的 `session_id:` 机器行，并在事件/RuntimeSession 中持久化；
- 轨迹：`hermes sessions export - --format jsonl --session-id <id> --redact`；
- 取消：新建进程组，先 TERM、超时后 KILL；
- Secret：剥离 `NICO_*`、数据库、Redis、MinIO、Docker 与外部 Hermes 配置环境；模型 Provider 凭据按管理员配置继承，MCP token 只写入 `0600` 的 Run 配置环境段，不进入参数、Prompt、事件或轨迹；
- pause：Hermes 0.18.2 不声明运行中 pause；历史 session resume 用于租约恢复，不冒充 pause/resume 对称能力。

每个 Run 使用独立 `HERMES_HOME/<tenant>/<run>`。配置将 platform CLI toolsets 限定为 `nico`，并禁用 terminal、web、browser、file、memory、skills 和 delegate；Nico MCP stdio 子进程再通过 `0600` Unix socket 和随机 token 回到当前 Worker。broker 每次 list/call 都复核 Run 租约和冻结权限。终态导出、取消、超时或启动失败会清理含 token 的 Run 目录。

默认 Compose Worker 镜像不包含 Hermes，也没有 Hermes 状态卷。可选 `hermes` profile 使用 `backend/Dockerfile.hermes` 构建固定 `hermes-agent[mcp]==0.18.2` 的独立 Worker；切换前必须停止默认 Worker，避免不同 Provider 集合竞争同一队列。`goal-l` profile 只运行仓库内 fake CLI 合同，不是用户部署方式。

仓库已完成 Hermes 0.18.2 CLI/MCP 发现、协议 v2、禁用失败关闭、成功/失败/取消/resume、Secret 脱敏和 Compose contract E2E；真实模型推理仍未执行，不能据此声称模型质量、外部 Provider 或生产部署可用。

## Legacy Provider 弃用窗口

- `0.2.x`：继续读取旧 `run_config.runtime_provider`、`model_config.runtime_provider` 和全空时的 Mock 缺省；每次命中均记录 telemetry。
- `0.3.x`：继续执行但在迁移检查中报告遗留 AgentVersion，部署方应审核后把 Provider 固化到正式字段。
- `0.4.0`：计划移除 AgentVersion 的隐式 legacy resolver。显式 `runtime_provider=mock/hermes` 仍按环境与 profile 执行；历史 RuntimeSession 保持原 Provider/版本，不做静默回写。

## 租约与恢复

1. `nico_worker_claimer` 不能直接查询业务表，只能执行固定 search path 的 `claim_next_run`。
2. 函数按 Task priority 和 Run 创建时间使用 `FOR UPDATE SKIP LOCKED` 领取 Pending 或租约过期的 Planning/Running Run。
3. Worker 回到 `nico_runtime` + TenantContext 事务加载 Task/AgentVersion，创建或锁定 RuntimeSession。
4. 心跳和每个事件/终态提交都校验 worker owner、lease token 和未过期时间。
5. API 取消先提交 Run/Task/RuntimeSession Cancelled 并清空租约；Provider 的迟到结果被拒绝。
6. 恢复只对 descriptor 明确声明 `resume` 的 Provider 开放；缺 session、能力不支持或 Provider 不匹配时以稳定错误失败。
7. Nico Native ReAct 从已提交的 schema v2 checkpoint 恢复，并校验 provider、protocol、execution mode、执行清单 Hash 和 checkpoint Hash；不兼容或终态 checkpoint 禁止恢复。
8. 接管时中断中的 ModelCall 被终态化并建立 replay relation；ToolCall 通过稳定 idempotency key 复用，旧 lease 的事件和终态提交继续由租约 fencing 拒绝。
9. Nico Native Plan-and-Execute 从 schema v3 checkpoint 和最新持久化 Plan revision 恢复；Plan 定义不塞入 checkpoint，已提交 Planner/Reflection/Step/Judge ModelCall 不重复计费。
10. 每次领取前执行最小权限 `reconcile_expired_tool_approvals()`，原子终态化到期请求、待执行 ToolCall/RunStep 并唤醒 Run；Run 树取消则把 requested 审批置为 cancelled。

## Memory 与 Skill 运行时准备

第一次领取 Run 时，Worker 在同一个租户事务中完成以下冻结：

1. 计算 Tenant settings 与不可变 AgentVersion 的 `memory_policy`/`skill_policy` 交集，包括 top-k、`max_tokens` 和 `max_chars` 双重上限；Child Run 再与 Parent 快照和 delegation restrictions 求交。
2. 先按 scope、状态、过期时间和类型过滤 Memory，再执行 pgvector top-k；Skill 只从显式 allowlist 中解析 Published stable/canary 版本。
3. 将精确 ID、版本、content hash、scope、来源 Hash、解析分支和受字符上限约束的内容写入 RuntimeSession 私有 `knowledge_selection_snapshot`。
4. 以 `untrusted_context` 重建 ContextSeed；ContextSnapshot 只公开 `memory_refs`、`skill_refs` 和效果元数据。
5. 为每个选择建立 RuntimeKnowledgeUsage；Context 和 ModelCall 投影分别绑定首次引用和计数，Run 终态再写入 outcome/result hash/effect metadata。

恢复只读取已冻结选择，绝不重新查询实时 Memory 或重新解析 Skill 指针。发布、失效、灰度切换和 rollback 只改变之后第一次领取的 Run。普通 Runtime API 不返回包含知识正文的 selection snapshot；查询消费事实使用 `GET /api/v1/runs/{run_id}/knowledge-usages`。

## 查询 API

- `GET /api/v1/runs/{run_id}`：权威 Run 当前状态与结果；
- `POST /api/v1/runs/{run_id}/cancel`：乐观锁权威取消；
- `GET /api/v1/runs/{run_id}/runtime`：Provider/session/capability/checkpoint/usage 摘要；
- `GET /api/v1/runs/{run_id}/trajectory`：终态规范化轨迹；
- `GET /api/v1/runs/{run_id}/events`：数据库 Event 回放。
- `GET /api/v1/runs/{run_id}/steps`：读取排序后的 RunStep；供只读 Run Inspector 使用；
- `GET /api/v1/runs/{run_id}/events/stream`：支持 `Last-Event-ID` 的租户隔离 SSE 续传；
- `GET /api/v1/runs/{run_id}/model-calls`：读取脱敏的模型请求/响应、usage、cost 与 provider request ID；
- `GET /api/v1/runs/{run_id}/contexts`：读取可审计的 ContextSnapshot；
- `GET /api/v1/runs/{run_id}/knowledge-usages`：读取精确知识版本、Context/ModelCall 引用计数与终态效果，不返回知识正文；
- `GET /api/v1/runs/{run_id}/plans` 与 `.../plans/{plan_id}`：按 revision 读取不可覆盖的 Plan 历史；
- `GET /api/v1/runs/{run_id}/plans/{plan_id}/steps`：读取步骤定义、依赖、执行状态、输出 Hash 和证据引用；
- `GET /api/v1/runs/{run_id}/runtime-evaluations`：读取 step validation、Reflection 和 Completion Evaluation；
- `GET /api/v1/model-endpoints`：读取当前租户的模型端点修订；写入接口受部署开关控制。
- `GET /api/v1/runs/{run_id}/tool-calls`：读取脱敏 ToolCall 结果与尝试，不公开执行租约 token；
- `GET /api/v1/tool-definitions`：读取当前租户已经实例化的版本化工具快照。
- `GET /api/v1/tool-approval-requests`：按 Run/状态读取审批请求与脱敏参数；
- `POST /api/v1/tool-approval-requests/{approval_id}/decision`：带 revision 和幂等键批准 once/run 或拒绝。

所有接口继续受 TenantContext 和 PostgreSQL FORCE RLS 约束。普通租户 API 不提供 RuntimeSession 或轨迹物理删除。
