# ADR-0009：平台唯一 Tool Gateway 与独立 Sandbox Runner

- 状态：Accepted
- 日期：2026-07-17

## 背景

Goal D 已将 Agent Runtime 与领域持久化隔离，但 Hermes 和其他 Runtime 都可能具备自己的工具执行能力。若允许 Runtime 直接读文件、联网、查询数据库或执行 Python，AgentVersion 白名单、租户隔离、Secret、ToolCall 审计和取消语义都会被绕过。

## 问题

如何在保留 Hermes 自主工具调用能力的同时，让所有实际副作用经过同一权限、隔离、审计和恢复边界，并避免 Python 沙箱获得 Worker 或宿主权限。

## 候选方案

1. 直接启用 Hermes 原生 web/terminal/file 工具，用 Prompt 约束路径和权限。
2. 每个 Runtime Adapter 自行实现权限、工具和审计，再把摘要事件返回平台。
3. 平台实现唯一 Tool Gateway；Runtime 只产生 tool intent 或通过按 Run 授权的 MCP 调用 Gateway；Python 交给独立最小接口的 Sandbox Runner。

## 最终选择

选择方案 3。ToolDefinition 和 ToolCall 由 PostgreSQL 在租户 RLS 下保存；Gateway 根据不可变 AgentVersion 与租户策略的交集授权，执行 Schema、Secret、幂等、超时、重试、输出限制和审计。文件、HTTP、数据库与报告实现为 Gateway Executor。Python 由独立 Runner 在一次性固定容器中执行，Worker 不挂载 Docker socket。

Hermes 只启用 Nico 按 Run 生成的 MCP server/toolset，MCP 凭据绑定 tenant/run/lease 并通过环境传递。Hermes 原生 terminal、web、file、browser 和 code 工具在平台运行中保持禁用。没有平台工具能力的 Provider 可以继续执行无工具任务，但不能自行声明已获得工具权限。

## 选择原因

权限、租户、Secret 和审计只实现一次；ToolCall 与 Run 状态可以在同一 PostgreSQL 权威边界恢复；MCP 保留 Hermes 的原生 Agent 工具循环而不导入 Hermes Python 内部类型。独立 Runner 将 Docker/容器管理权限与持有模型及数据库 Secret 的 Worker 隔离。

## 代价

Runtime 协议或 MCP 需要额外桥接；工具调用延迟增加；必须维护 DNS/IP、路径和 SQL 安全校验。Sandbox Runner 仍属于高风险基础设施，需要固定镜像、最小 API、认证、资源限额和独立部署；Docker socket 即使只在 Runner 内也必须视为宿主级权限。

Goal E 尚无 Team/Role/Plugin 实体，因此授权只使用租户与 AgentVersion，并对缺失或未知权限层失败关闭。后续 Goal G/H 只能把权限交集继续收窄，不能改变既有 ToolCall 记录或扩大历史 Run 权限。

## 后续影响

Goal E 实现核心工具和 Runtime/MCP 接入。Goal F Memory/Skill 只能通过已审计轨迹消费工具结果；Goal H Plugin 注册的 ToolDefinition 必须复用相同 Registry/Gateway 合同；Goal I 的任何交易相关外部写操作仍需新增高风险 Approval，不能因存在 HTTP/DB 工具而默认开放。

## 可逆性

高。Executor 可替换为远程工具服务，Sandbox Runner 可替换为 Kubernetes Job、gVisor、Firecracker 或托管沙箱；ToolDefinition/ToolCall 和权限合同保持不变。MCP 与规范化 tool intent 可以并存，不改变领域事实来源。
