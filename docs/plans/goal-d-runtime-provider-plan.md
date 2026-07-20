# Goal D：Runtime Provider 与持久化 Worker 实施计划

- 状态：Completed / Verified
- 日期：2026-07-17
- 基线：Goal C commit `277f312`
- Hermes 参考：本地 `/home/node7/xpk/hermes-agent`，版本 `0.18.2`，commit `bda8bd76a`

## 范围

本阶段实现 provider-neutral 的 AgentRuntimeProvider、确定性 Mock、Hermes Adapter、RuntimeSession、PostgreSQL Run 领取/租约/心跳/恢复、Worker 执行、状态查询、取消与轨迹导出。Tool Gateway/ToolCall/Sandbox、Memory、Skill、Team、Workflow、Plugin、量化逻辑、正式认证和业务 Web Console 不进入 Goal D。

## Provider 合同

统一异步接口包含：

1. `create_session`：接收平台 RuntimeRequest，返回 provider session handle 和能力快照；
2. `run`：执行一次结构化任务并返回规范化 RuntimeResult；
3. `pause` / `resume` / `cancel`：按能力协商执行，缺失能力返回稳定 `RUNTIME_CAPABILITY_UNSUPPORTED`；
4. `get_status`：返回 provider session 状态；
5. `stream_events`：按单调 sequence 输出规范化事件；
6. `export_trajectory`：返回结构化、可 JSON 序列化的完整轨迹。

领域和应用层只能依赖平台合同，不能导入 Hermes 类型。Provider 不接收数据库 Session，不能直接修改 Task/Run/RunStep/Event/AuditRecord。

## 持久化与租约不变量

1. PostgreSQL Run 仍是权威队列；Redis 不是事实来源。
2. `nico_worker_claimer` 没有业务表直接权限，只能执行 SECURITY DEFINER 的最小 claim 函数；函数只返回 run/tenant/lease 标识和原状态。
3. claim 使用 `FOR UPDATE SKIP LOCKED`，Pending 或租约已过期的 Planning/Running Run 同一时刻只能被一个 Worker 领取。
4. 领取后的正文加载、RuntimeSession、状态、Event 和 Audit 均回到 `TenantContext` + `nico_runtime` 事务。
5. 每次心跳校验 `lease_owner + lease_token`；旧 Worker 永远不能续租或提交新状态。
6. API 取消先提交权威 Cancelled；Worker 检测租约/状态丢失后调用 Provider cancel，迟到结果不得覆盖终态。
7. Pending 崩溃可重新领取；active Run 只有 Provider 声明 resume 能力且持久化 session/checkpoint 可用时才恢复，否则以稳定错误失败，不伪造恢复成功。
8. RuntimeSession 与 Run 一一对应，保存 provider/version、external session、能力、状态、checkpoint、usage 与轨迹；普通租户 API 不提供物理删除。

## Hermes Adapter 边界

- 采用独立子进程调用已安装的 Hermes CLI，而不是把 Hermes 包导入平台 domain/application。
- 当前兼容目标是 Hermes `0.18.2`：运行使用 `hermes chat -q ... --quiet`，模型、provider、toolsets 和 resume 通过 CLI 参数映射。
- AgentVersion 的角色、mandate、boundaries、目标和 Task 输入由 Adapter 组装为清晰的任务 envelope；API Key 等 Secret 只来自 Worker 进程环境，不写入 AgentVersion、Event 或轨迹。
- stdout/stderr 被规范化为平台事件；external session ID 从 Hermes 的机器可读退出行解析；轨迹使用 `hermes sessions export ... --redact`。
- 子进程组允许平台硬取消。Hermes 当前没有可靠的运行中 pause/resume 合同，Adapter 必须声明 pause=false；历史 session resume 只用于新的/恢复的调用，不能冒充运行中暂停。
- Hermes 未安装、版本不兼容、凭据缺失和 CLI 失败都返回稳定、脱敏的 RuntimeError；Mock E2E 不依赖外部模型或 Secret。

## API 与 Worker 出口

- 现有 `POST /runs/{id}/cancel` 保持权威取消入口。
- 增加 `GET /runs/{id}/runtime` 和 `GET /runs/{id}/trajectory`。
- 增加仅在 Provider 能力允许时工作的 pause/resume 命令。
- Worker 从基础设施监督升级为“健康监督 + 有界轮询 + 单次领取执行”；并发度、poll interval、lease TTL、heartbeat 有 Settings。
- Worker 的一次执行必须可作为独立函数测试，不以无限循环作为唯一入口。

## 验收出口

- 所有 Provider 通过同一 contract suite；Mock 覆盖成功、失败、取消、事件序列、状态和轨迹。
- Hermes Adapter 使用假的 CLI 做确定性进程/解析/取消/导出测试，并对本地 Hermes parser/source 做兼容性检查；无外部 API Key 时不声称真实推理成功。
- 真实 PostgreSQL 证明并发 claim 唯一、过期租约接管、陈旧 token 拒绝、RLS 正文隔离、RuntimeSession/Event/Audit 原子。
- Worker + Mock 的 Compose E2E 由 API 创建 Run，Worker 自动推进到 Completed；同时覆盖取消与失败路径。
- `scripts/verify-goal-d.sh` 运行全量回归、真实集成和 Runtime E2E，保存证据与清理日志。

## 阶段成果

完成后更新 Runtime/API/架构/状态机/测试文档、Goal Status、Feature Matrix，保存 `artifacts/goals/goal-d/<timestamp>/`，创建 Goal D Handoff。只有全部出口实际通过才标记 Verified。
