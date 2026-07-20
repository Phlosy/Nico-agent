# ADR-0003：Runtime Provider 隔离与 Hermes Adapter

- 状态：Accepted
- 日期：2026-07-16

## 背景

第一版需要 Hermes，但平台不能永久绑定 Hermes，也不能让领域层依赖其内部会话和事件类型。

## 问题

如何统一不同 Runtime 的运行、暂停、恢复、取消、状态、事件和轨迹能力，同时诚实表达能力差异。

## 候选方案

1. 业务层直接调用 Hermes Python 类。
2. 仅使用 OpenAI Chat Completions 作为最小接口。
3. 定义平台 `AgentRuntimeProvider` Protocol，并在 Adapter 中实现 Hermes、Mock 和未来 Provider。

## 最终选择

选择方案 3。Provider 暴露 create_session、run、pause、resume、cancel、get_status、stream_events、export_trajectory，并提供 capability negotiation。

## 选择原因

Provider Protocol 覆盖结构化任务和可恢复 Run，不把模型聊天接口误当完整 Agent Runtime；能力协商可避免伪造 pause/resume。

## 代价

Adapter 需要规范化 Hermes 事件和错误；平台检查点与 Provider 检查点可能存在语义差异，需要 contract tests。

## 后续影响

Goal D 先实现 Mock contract tests，再实现真实 Hermes Adapter。Provider 只返回事件和结果，不直接写业务表或批准成长内容。

## 可逆性

高。新增 Provider 不改领域层；接口重大变化通过新协议版本和兼容 Adapter 演进。
