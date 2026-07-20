# ADR-0013：Summary 加近期 Turn 的有界对话上下文

- 状态：Accepted
- 日期：2026-07-19

## 背景

Nico 已有 Run 级 ContextSnapshot、ModelCall 和 Memory/Skill 冻结引用。持续对话不能把完整历史无限追加到模型输入，也不能只在 CLI 保存不可审计的摘要。

## 问题

如何在控制 token 的同时保留完整事实、支持恢复并准确记录每一轮模型实际看到的内容？

## 候选方案

1. 每轮拼接全部历史，直到 Provider 拒绝。
2. CLI 本地截断或摘要，只把结果发给 Task。
3. PostgreSQL 保留完整 Turn；服务端按 Conversation summary、近期原始 Turn、Artifact 摘要/引用、已治理 Memory/Skill 和当前输入构建有界上下文，并扩展现有 ContextSnapshot 记录选择结果。

## 最终选择

选择方案 3。Summary 记录覆盖到的 sequence、输入 hash 和独立 ModelCall。恢复同一 Run 时复用冻结 ContextSnapshot，不重新选择。旧 ContextSnapshot 通过可空字段保持兼容。

## 选择原因

完整事实与模型输入可以同时审计；早期历史可压缩，近期语义保真；服务端统一执行 token 预算，不依赖某台 CLI。复用现有 ContextSnapshot 避免两个权威快照模型。

## 代价

Summary 本身消耗模型调用并可能损失细节；需要选择算法、版本和质量测试。Artifact 摘要及 token 估算必须有界，且 summary 失效/重算需要清晰规则。

## 后续影响

Goal C 先建立 Conversation 引用字段；Goal E 实现 summary、context selection、compact 和审计。ContextSnapshot 增加 selected turn、summary hash、artifact refs 和 token budget，但继续保留 rendered messages、Memory/Skill refs 和 content hash。

## 可逆性

高。选择器和摘要策略可版本化替换；完整 Turn 与历史快照不被改写。
