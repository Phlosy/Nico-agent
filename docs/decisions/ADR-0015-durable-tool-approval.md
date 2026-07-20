# ADR-0015：持久化工具审批与 Worker 唤醒

- 状态：Accepted
- 日期：2026-07-19

## 背景

Run 状态机已经包含 `waiting_for_approval`，Runtime 也有挂起基础，但现有 Approval 只用于 Memory/Skill 发布。敏感 Tool Call 需要人在 CLI 中决定，且 CLI 断开后请求不能丢失。

## 问题

能否在 CLI 本地提示后直接继续工具调用，或复用 Growth Approval？审批如何与 Worker 租约和恢复连接？

## 候选方案

1. CLI 本地询问，进程内把布尔值发给仍在等待的 Worker。
2. 扩展 Growth Approval 兼容工具、发布和执行三类语义。
3. 新增 ToolApprovalRequest，事务化保存 ToolCall、风险、scope 和决定；Worker 挂起释放租约，决定后把 Run 重新置为可领取并从 checkpoint 恢复。

## 最终选择

选择方案 3。允许范围第一版为 once/run，状态为 requested、approved、rejected、expired、cancelled。重复决定按 revision 和终态规则幂等或冲突，所有转换写入 Event/Audit。

## 选择原因

服务端状态能跨 CLI 断线和 Worker 重启恢复，符合 Run 权威模型。Growth 发布审批与工具执行审批的主体、期限、授权范围和唤醒动作不同，分表比多态扩张更清晰。

## 代价

需要 Tool Gateway 风险策略、checkpoint、claim/wake SQL、超时和并发测试。错误批准可能扩大一次 Run 内的能力，因此 allowed scope 必须精确并受冻结 Tool policy 上限约束。

## 后续影响

Goal F 已按该选择实现实体、API、SSE `ApprovalRequested`、CLI 交互、断线恢复、Worker 唤醒和超时协调器。Native ReAct/Plan 在副作用前保存 checkpoint；不具备该持久化边界的 Adapter 调用失败关闭。不得以本地确认框冒充服务端审批。

## 可逆性

中等。审批 UI 和风险分类可演进；持久化决定与审计事实不可删除或改写。
