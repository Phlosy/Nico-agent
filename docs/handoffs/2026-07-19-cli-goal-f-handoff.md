# CLI Goal F Handoff：持久化敏感工具审批

日期：2026-07-19  
阶段：CLI-F  
下一阶段：CLI-A–F 完成审计与常规维护

## 阶段结果

CLI-F 已完成任务书最后一个阶段。审批是 PostgreSQL 权威领域事实，CLI 仍是薄 HTTP/SSE 客户端，Agent Loop、工具执行、checkpoint 和唤醒都留在服务端。

- 新增独立 ToolApprovalRequest，状态为 requested、approved、rejected、expired、cancelled；批准 scope 为 once/run。
- medium/high Tool Gateway 调用先保存 Native pre-action checkpoint、Pending ToolCall、Waiting RunStep、请求 Event/Audit，再进入 `waiting_for_approval`。
- CLI 收到 `ApprovalRequested` 后显示精确工具、风险、期限和脱敏参数；支持交互选项以及 `/approvals`、`/approve`、`/reject`。
- 决策 API 使用 TenantContext、FORCE RLS、expected revision 和 `Idempotency-Key`。同键同决定可重放，不同终态决定返回 409。
- Worker 挂起时释放租约。决定将 Run 重新置为可领取；Native ReAct/Plan 从同一 checkpoint 和稳定 ToolCall 幂等键恢复，副作用不会重复。
- 非 TTY/JSON/exec/watch 不自动批准，在请求处安全返回 `approval_required`。`chat --resume` 会重新读取 requested 请求。
- Worker 领取前运行最小权限超时协调函数，原子终态化请求、ToolCall/RunStep 并唤醒 Run；Run 树取消会把请求置为 cancelled。
- Growth Approval 没有被复用；Hermes/MCP 等没有 Native checkpoint 的敏感调用失败关闭。

## 数据与迁移

Alembic head 为 `20260719_0019`。迁移创建 `tool_approval_requests`、同租户复合外键、请求身份不可变与单次终态触发器、索引、FORCE RLS，以及只授予 `nico_worker_claimer` 的 `reconcile_expired_tool_approvals()`。

请求保存 ToolDefinition、ToolCall、RunStep、Run、风险、请求者、脱敏参数、参数 Hash、期限和完整决定。审批终态及相关 Event/Audit 不可改写。

## 主要代码

- `backend/src/nico_agent/tool_approvals/`
- `backend/src/nico_agent/tools/gateway.py`
- `backend/src/nico_agent/runtime/native/loop.py`
- `backend/src/nico_agent/runtime/service.py`
- `backend/src/nico_agent/runtime/executor.py`
- `backend/src/nico_agent/cli/chat.py`
- `backend/src/nico_agent/cli/client.py`
- `backend/src/nico_agent/cli/renderers.py`
- `backend/migrations/versions/20260719_0019_tool_approvals.py`

## 安全与并发边界

- Tool policy 与精确版本授权仍先于审批；批准只缩小到一次调用或当前 Run，不能扩张租户/AgentVersion 权限。
- 请求参数在 Gateway 侧递归脱敏，CLI 不读取 ToolCall 原始 Secret。
- Gateway 在请求提交前不会执行 Executor。恢复复用原 ToolCall 和 idempotency key。
- 决策与 Worker 最终 suspension 可能并发；挂起事务不持有审批写锁，并在 Run 锁内读取已提交状态，避免死锁和丢失唤醒。
- 终态 Run、过期请求和并发不同决定均失败关闭；跨租户读取表现为 404。
- 当前操作者身份仍基于受信网络 Header，不应宣称生产级认证已经完成。

## 验收入口

```bash
scripts/e2e-cli-goal-f.sh
NICO_EVIDENCE_DIR=artifacts/goals/cli-goal-f/<UTC> scripts/verify-cli-goal-f.sh
```

阶段测试覆盖状态合同、CLI 不自动批准、服务端决策、RLS、幂等/冲突、审批前零副作用、Worker 挂起/恢复、超时回收、exactly-once，以及 JSON 断开后 PTY resume 完成两次真实工具审批。

## 接续约束

CLI-A–F 已无下一开发 Goal。后续若新增交易、外部写入或更高风险工具：

1. 必须复用 Tool Gateway、ToolApprovalRequest、checkpoint 和审计边界。
2. 正式部署应先增加 API 身份认证与授权；不能把开发 Tenant/Actor Header 当作生产身份。
3. 新 scope 不得默认扩大 `run`，必须有单独 ADR、迁移和并发/撤销测试。
4. Adapter 只有在能提供持久化 pre-action checkpoint 时才可支持敏感审批恢复。
5. Team 与业务 Workflow 继续由量化、科研等领域系统负责，不进入 Nico core。
