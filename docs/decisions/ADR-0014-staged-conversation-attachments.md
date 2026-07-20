# ADR-0014：Run 前受控暂存的 Conversation 附件

- 状态：Accepted
- 日期：2026-07-19

## 背景

现有 Artifact 必须绑定一个 Run，并由 PostgreSQL 保存元数据、MinIO 保存私有内容。`/attach` 可以在用户提交下一条 Turn 前发生，CLI 与服务端也可能位于不同机器。

## 问题

如何上传本地文件而不开放服务端本地目录，同时不破坏已有 Run-owned Artifact 约束？

## 候选方案

1. 把 CLI 本地路径放进 Prompt，让 Worker 直接读取。
2. 令 Artifact.owner_run_id 可空，并在提交 Turn 后改写 available Artifact 的所有权。
3. 新增有 TTL 的 ConversationAttachment 暂存元数据；CLI 上传字节，Turn 创建时物化为 Run-owned Artifact 并复用内容寻址对象。

## 最终选择

选择方案 3。暂存件私有、受租户和 Conversation 约束，提交 Turn 时生成带 provenance 的 Artifact 引用。未消费内容按 TTL 清理。

## 选择原因

方案不假定共享文件系统，不允许路径穿越，也不放松已有 Artifact 的 Run 归属和终态约束。内容寻址允许不复制字节，同时保持清晰的生命周期和审计。

## 代价

新增暂存表、清理任务、配额和对象引用计数。Turn 事务与 MinIO 操作需要可重试的两阶段处理，失败时要识别并清理孤儿对象。

## 后续影响

Goal E 新增上传/列出/删除暂存 API、CLI `/attach` 和物化逻辑。服务端不得接收任意本地路径；Artifact 正文也不默认完整注入模型上下文。

## 可逆性

中等。未来可改为预签名上传或独立 Blob service，但 ConversationAttachment 到 Artifact 的权威 provenance 需要保留。
