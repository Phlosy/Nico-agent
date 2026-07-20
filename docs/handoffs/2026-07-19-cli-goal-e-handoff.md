# CLI Goal E Handoff：有界会话上下文与受控附件

日期：2026-07-19  
阶段：CLI-E  
下一阶段：CLI-F——工具审批、挂起与恢复

## 阶段结果

CLI-E 已把 Conversation summary、最近 Turn 选择和 Run 前附件接入既有 Task/Run/Worker/Runtime 边界，没有创建本地 Agent Loop 或第二套会话运行时。

- `POST /api/v1/conversations/{id}/compact` 创建独立 Task/Run；Worker 完成后保存 summary、覆盖 sequence、输入 Hash 和对应 ModelCall。
- Runtime 首次 claim 按预算选择 summary、最近完成 Turn 和有界 Artifact 引用，并把选择冻结进 RuntimeSession；同一 Run 恢复不重选。
- 原生 Runtime 的 ContextSnapshot 保存 Conversation/Turn、selected Turn IDs、summary Hash、Artifact refs、token budget、实际 rendered messages 和裁剪事实。
- `ConversationAttachment` 提供租户/Conversation 隔离的短期暂存、SHA-256 内容寻址、TTL、单件/数量/累计大小限制和一次性消费。
- 创建 Turn 时在同一数据库事务内把有效暂存件物化为当前 Run 拥有的 Artifact，并把引用写入 Turn；retry 为新 Run 重建授权元数据并复用对象字节。
- CLI 注册 `/attach PATH`、`/compact` 和 `/download ARTIFACT_ID [PATH]`。本地路径只由 CLI 读取，不发送给服务端；下载使用 `0600` 原子文件并拒绝最终符号链接。

## 数据与迁移

Alembic head 为 `20260719_0018`。迁移新增 `conversation_attachments`，包含复合租户外键、消费完整性约束、幂等键、索引、`FORCE RLS` 和最小运行角色权限。已有 Conversation、Turn、ContextSnapshot 与 Artifact 继续复用，不复制权威事实。

## 主要代码

- `backend/src/nico_agent/conversations/attachments.py`
- `backend/src/nico_agent/conversations/context.py`
- `backend/src/nico_agent/conversations/service.py`
- `backend/src/nico_agent/runtime/service.py`
- `backend/src/nico_agent/cli/chat.py`
- `backend/src/nico_agent/cli/client.py`
- `backend/src/nico_agent/cli/execution.py`
- `backend/migrations/versions/20260719_0018_conversation_context.py`

## 安全边界

- 服务端 API 不接受本地文件路径、目录挂载或任意对象键。
- 暂存件和最终 Artifact 保持私有 MinIO 对象；下载仍按 Run ownership/link 复核 Hash 与 size。
- Artifact 正文不默认注入模型上下文；只使用有界文本摘要和元数据引用。
- 所有 ConversationAttachment 表访问使用 `FORCE RLS`；跨租户资源表现为不存在。
- summary 不删除或覆盖 Turn 历史；摘要输入 Hash 和 ModelCall 保留可审计来源。
- Goal E 没有实现 Tool Approval，也没有把 Growth Approval 误用为运行时审批。

## 验收入口

```bash
scripts/e2e-cli-goal-e.sh
NICO_EVIDENCE_DIR=artifacts/goals/cli-goal-e/<UTC> scripts/verify-cli-goal-e.sh
```

Goal E 新增测试覆盖真实 MinIO 上传/下载、附件幂等与消费、Run ownership、compact replay、Native summary ModelCall、下一轮 ContextSnapshot、CLI 原始字节传输和私有文件权限。

最终验收结果：

- 文档、Markdown、Shell、Compose 与 diff 静态检查通过。
- Ruff 与格式检查通过；283 项后端单测、11 项前端测试和生产构建通过。
- 80 项 PostgreSQL/Redis/MinIO 集成测试通过；Alembic `0018 → base → 0018` 往返通过。
- CLI-B、CLI-C、CLI-D 回归以及 CLI-E 真实 PTY 附件、摘要压缩、原字节下载路径通过。
- 最终证据目录为 `artifacts/goals/cli-goal-e/20260719T115130Z/`。

## CLI-F 接续约束

CLI-F 只能在现有 Tool Gateway、ToolCall、Run/RuntimeSession、Event/Audit 和 Worker 租约上增加审批事实：

1. 高风险 Tool 命中策略后先持久化 ToolApprovalRequest，再令 Run 进入 `waiting_for_approval` 并释放租约。
2. `allow once`、`allow for run` 和 `reject` 都必须是服务端 revisioned/idempotent 决策，CLI 只展示和提交。
3. 断开 CLI 不得默认批准或拒绝；重连必须读回待审批事实。
4. 审批后由 Worker 重新 claim 并从冻结 checkpoint/context 恢复，不能重复已完成副作用。
5. 不新增本地确认状态、不让 Prompt 授权工具、不复用 Memory/Skill 发布 Approval。
