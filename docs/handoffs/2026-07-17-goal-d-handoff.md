# Goal D Handoff：Runtime Provider 与持久化 Worker

## 1. 阶段结论

Goal D 已完成并通过 `scripts/verify-goal-d.sh`。平台现在能够通过公共 API 创建 Pending Run，由独立 Worker 以 PostgreSQL 最小权限租约领取，经可替换 Runtime Provider 执行，并原子持久化 RuntimeSession、RunStep、Event、Audit、checkpoint、结果和规范化轨迹。

本阶段没有实现 Tool Gateway/Sandbox、ToolCall/ModelCall、Memory、Skill、Team/Workflow、Plugin、量化策略、正式认证、SSE 或 SDK。这些缺口不能由 Hermes 原生工具、Prompt 或 Mock 事件冒充。

## 2. 交付内容

- `AgentRuntimeProvider`：冻结 DTO、descriptor/capability、8 个异步方法、稳定错误与规范事件。
- `MockRuntimeProvider`：确定性成功/失败、暂停/恢复、取消、checkpoint、连续事件和轨迹。
- `HermesRuntimeProvider`：完全隔离的 CLI 子进程 Adapter；兼容目标 0.18.2，首次 session 前版本 fail-closed；支持历史 session resume、进程组取消、stderr session ID 解析和 `--redact` JSONL 导出。
- `RuntimeSession`：与 Run 一一对应，保存 Provider/version/protocol/capability、external ID、状态、provider state、checkpoint、usage、trajectory 和 last event sequence；启用 FORCE RLS。
- PostgreSQL claimer：`nico_worker_claimer` 无业务表直读权限，只能执行固定 search path 的 SECURITY DEFINER 函数；claim 使用 priority + `FOR UPDATE SKIP LOCKED`。
- Runtime 应用服务：正文始终在 TenantContext + `nico_runtime` 中读取；心跳、事件和终态提交校验 owner/token/expiry；Provider 不接触 ORM。
- Worker：可配置轮询、租约、心跳和并发；生产不注册 Mock；一次执行可独立测试。
- API：`GET /runs/{id}/runtime`、`GET /runs/{id}/trajectory`，并复用既有权威 cancel/events/run 查询。

阶段提交：

- `561faa4`：锁定 Goal D 合同与边界；
- `214603e`：Provider 协议和确定性 Mock；
- `80b9578`：租约、RuntimeSession、应用服务、Worker 和真实数据库恢复；
- `e1e29ba`：Hermes CLI Adapter；
- `4a82eb4`：Runtime API、故障路径和 Compose Worker E2E；
- 本 Handoff 所在提交：最终审查、版本 fail-closed、文档和验收治理。

## 3. 权威执行路径

1. API 创建 Task/Run，PostgreSQL 保存 Pending Run。
2. Worker 以 claimer 角色调用 `claim_next_run(worker_id, ttl)`，只取得 run/tenant/token/previous status。
3. Worker 回到租户事务加载固定 AgentVersion，按 `run_config.runtime_provider` 从 Registry 选择 Provider。
4. 应用服务创建/锁定 RuntimeSession，Provider 只收到 `RuntimeSessionRequest`。
5. Provider event sequence 逐条持久化并映射 Run/RunStep/Event/Audit/checkpoint；Worker 独立心跳。
6. 终态结果和 trajectory 只有在 lease owner/token/expiry 仍有效时提交；随后清空租约并推进 Task。
7. API 取消先写入 Run/Task/RuntimeSession Cancelled 并清空租约；旧 Worker 检测 lease lost 后取消 Provider，迟到结果返回 false，不覆盖终态。

## 4. 恢复语义

- Pending Run 可正常重新领取。
- 过期的 Planning/Running Run 只有在 RuntimeSession 存在、Provider 名称一致且 capability 包含 resume 时恢复。
- `RuntimeSessionRequest` 携带 checkpoint、last event sequence 和 persisted external session ID。
- Mock 从 `completed_steps` 继续且事件序号从持久化值递增；Hermes 通过 `--resume <session_id>` 恢复历史 session。
- 缺失 session、Provider 不支持恢复、Provider 不匹配或未注册时，Run 以稳定错误进入 Failed，不留毒租约。

## 5. 在上层量化公司中的调用方式

`quantfirm-os` 不应 import Hermes 或 Worker 内部实现。它应把通用 Agent 专用化为不可变 AgentVersion，再只调用 Nico API：

```json
{
  "role": "strategy-researcher",
  "mandate": "研究候选策略并返回结构化证据",
  "boundaries": ["不得直接实盘下单", "不得承诺盈利"],
  "long_term_goal": "形成稳健、可复验的研究流程",
  "model_config": {"model": "<model>", "provider": "<provider>"},
  "run_config": {"runtime_provider": "hermes", "hermes": {"toolsets": ["nico-disabled"]}},
  "budgets": {"token_limit": 20000}
}
```

随后创建 Task 与 Run，轮询 `GET /runs/{id}`，完成后读取 `/runtime`、`/trajectory` 和 `/events`。当前 `nico-disabled` 是 Goal D 的工具隔离占位；真正的联网搜索、文件、Python、数据库与交易相关操作必须等 Goal E Tool Gateway/Sandbox，以权限、超时、审计和网络边界接入。任何实盘下单仍应由更高风险 Approval 和独立量化 Plugin 控制，不能靠 Prompt 授权。

## 6. 验收证据

证据目录：`artifacts/goals/goal-d/20260717T032610Z/`。

- 后端单测：47 passed；
- 前端组件：7 passed；TypeScript/Vite production build passed；
- 真实集成：18 passed；
- Alembic：head → base → head passed；
- Goal C HTTP/RLS E2E：passed；
- Goal D HTTP → Compose Worker → PostgreSQL lease → Mock → trajectory E2E：passed；
- Hermes：假 CLI 成功/失败/取消/恢复/导出/脱敏/版本不兼容和本地 0.18.2 源码兼容已验证。

当前主机没有安装 `hermes` 命令，也没有外部模型验收凭据，因此未执行真实 Hermes 推理。这是如实记录的可选外部依赖缺口，不是用 Mock 伪造的成功。

## 7. Goal E 入口

开始 Goal E 前读取本 Handoff、`docs/runtime.md`、ADR-0002/0003/0006/0008 和 Feature Matrix。推荐顺序：

1. 定义 Provider event 中 tool intent 到平台 Tool Gateway 的唯一边界；禁止 Hermes 原生工具绕过。
2. 实现 ToolDefinition/ToolCall、Registry 和 Agent/Role/Plugin/租户权限交集。
3. 先做拒绝、超时、重试、幂等、路径逃逸、DNS/重定向和网络隔离测试，再接文件/HTTP/DB/报告/Python 工具。
4. Worker 继续只通过应用服务持久化 ToolCall/RunStep/Event/Audit；Redis 不能成为权威状态。
5. 不提前进入 Memory/Skill 成长、Team/Workflow、Plugin 加载或量化业务。
