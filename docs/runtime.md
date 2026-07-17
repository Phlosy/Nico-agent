# Runtime Provider 与持久化 Worker

## 稳定边界

`AgentRuntimeProvider` 是所有 Agent 执行后端共同遵循的异步协议：`create_session`、`run`、`pause`、`resume`、`cancel`、`get_status`、`stream_events` 和 `export_trajectory`。输入输出均为冻结的 Pydantic DTO；Provider 不接收 ORM、数据库 Session 或控制面服务。

能力通过 `RuntimeProviderDescriptor.capabilities` 明示。缺少能力必须抛出稳定的 `RUNTIME_CAPABILITY_UNSUPPORTED`，不能返回伪成功。事件使用单调 `sequence` 和平台枚举；应用服务检查连续性、幂等忽略已提交事件，再映射为 RunStep/Event/Audit。

## Provider 选择

AgentVersion 的 `run_config.runtime_provider` 选择 Provider；若缺省，则读取 `model_config.runtime_provider`，开发/测试默认 `mock`。生产 Worker 不注册 Mock，避免测试执行器被误用为真实推理。

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

Goal E 增加仅用于确定性验收的 `mock.tool_calls`。每项必须给出精确工具名称、版本和参数；Mock 只生成规范化 tool intent，实际执行仍由 Worker 注入的 Tool Gateway handler 完成，不能直接调用 Executor。

## Hermes Adapter

`HermesRuntimeProvider` 面向本地参考版本 Hermes `0.18.2`，采用 CLI 子进程而非 Python 内部类：

- 兼容：首次创建 session 前运行一次 `hermes version` 并缓存结果；非 0.18.2 或无法解析时 fail-closed；
- 执行：`hermes chat -q <envelope> -Q`，按配置映射 `--model`、`--provider` 和 `--resume`；有平台工具时只传 `--toolsets nico`；
- session：解析 stderr 的 `session_id:` 机器行，并在事件/RuntimeSession 中持久化；
- 轨迹：`hermes sessions export - --format jsonl --session-id <id> --redact`；
- 取消：新建进程组，先 TERM、超时后 KILL；
- Secret：剥离 `NICO_*`、数据库、Redis、MinIO、Docker 与外部 Hermes 配置环境；模型 Provider 凭据按管理员配置继承，MCP token 只写入 `0600` 的 Run 配置环境段，不进入参数、Prompt、事件或轨迹；
- pause：Hermes 0.18.2 不声明运行中 pause；历史 session resume 用于租约恢复，不冒充 pause/resume 对称能力。

每个 Run 使用独立 `HERMES_HOME/<tenant>/<run>`。配置将 platform CLI toolsets 限定为 `nico`，并禁用 terminal、web、browser、file、memory、skills 和 delegate；Nico MCP stdio 子进程再通过 `0600` Unix socket 和随机 token 回到当前 Worker。broker 每次 list/call 都复核 Run 租约和冻结权限。终态导出、取消、超时或启动失败会清理含 token 的 Run 目录。

Goal E 已使用本地 Hermes 0.18.2 源码和官方 `mcp==1.26.0` client 完成真实 initialize/tools/list 发现测试，无需模型凭据；真实模型推理仍未执行，不能据此声称模型质量或外部 Provider 可用。

## 租约与恢复

1. `nico_worker_claimer` 不能直接查询业务表，只能执行固定 search path 的 `claim_next_run`。
2. 函数按 Task priority 和 Run 创建时间使用 `FOR UPDATE SKIP LOCKED` 领取 Pending 或租约过期的 Planning/Running Run。
3. Worker 回到 `nico_runtime` + TenantContext 事务加载 Task/AgentVersion，创建或锁定 RuntimeSession。
4. 心跳和每个事件/终态提交都校验 worker owner、lease token 和未过期时间。
5. API 取消先提交 Run/Task/RuntimeSession Cancelled 并清空租约；Provider 的迟到结果被拒绝。
6. 恢复从 RuntimeSession 的 external session、checkpoint 和 last event sequence 继续；缺 session、Provider 不支持 resume 或 Provider 不匹配时以稳定错误失败。

## 查询 API

- `GET /api/v1/runs/{run_id}`：权威 Run 当前状态与结果；
- `POST /api/v1/runs/{run_id}/cancel`：乐观锁权威取消；
- `GET /api/v1/runs/{run_id}/runtime`：Provider/session/capability/checkpoint/usage 摘要；
- `GET /api/v1/runs/{run_id}/trajectory`：终态规范化轨迹；
- `GET /api/v1/runs/{run_id}/events`：数据库 Event 回放。
- `GET /api/v1/runs/{run_id}/tool-calls`：读取脱敏 ToolCall 结果与尝试，不公开执行租约 token；
- `GET /api/v1/tool-definitions`：读取当前租户已经实例化的版本化工具快照。

所有接口继续受 TenantContext 和 PostgreSQL FORCE RLS 约束。普通租户 API 不提供 RuntimeSession 或轨迹物理删除。
