# ADR-0012：Conversation 独立于 RuntimeSession

- 状态：Accepted
- 日期：2026-07-19

## 背景

现有 RuntimeSession 与 Run 一对一，记录 Provider 身份、执行模式、checkpoint、loop state 和 usage。持续对话需要跨多个用户 Turn 和 Run 存活，并在切换 CLI 或 Worker 后恢复。

## 问题

是否可以把 RuntimeSession、AgentMessage 或 Task 直接当作用户 Conversation，还是需要新增平台领域对象？

## 候选方案

1. 让一个 RuntimeSession 跨多个用户输入复用。
2. 把多 Agent 的 AgentMessage 当作用户对话记录。
3. 新增 Conversation 和 ConversationTurn；每个 Turn 原子创建 Task 和初始 Run，RuntimeSession 仍然只属于一个 Run。

## 最终选择

选择方案 3。Conversation 创建时冻结 Project、Agent 和不可变 AgentVersion。每个 Turn 有一个 Task 和当前 Run；重试沿用 Task 的多 attempt 机制并更新当前 Run 指针，旧 Run 关系保持不变。

## 选择原因

用户会话、执行器会话和 Agent 间消息有不同的生命周期、可见性和恢复语义。分离后可以重放单个 Run、跨 Run 压缩上下文，并保证更换 AgentVersion 时显式新建 Conversation，而不污染历史语义。

## 代价

增加表、RLS、复合外键、状态投影和事务编排。Turn 与 Run 都有状态，需要明确 Run 是执行权威、Turn 是用户视图，并以幂等投影避免漂移。

## 后续影响

Goal C 新增 Conversation/Turn 与 API；现有非对话 Task/Run 继续合法。AgentMessage 只服务于父子 Run 协调。RuntimeSession 不增加 Conversation 生命周期职责。

## 可逆性

低。Conversation 成为持久化公开合同后不应移除；但字段和投影策略可以通过追加迁移演进。
