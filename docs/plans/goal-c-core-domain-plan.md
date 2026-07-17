# Goal C：核心领域与租户隔离实施计划

- 状态：In Progress
- 日期：2026-07-17
- 基线：Goal B commit `5408ab1`

## 范围

本阶段只实现通用控制面的 Tenant/Project、Agent/AgentVersion、Task/Run/RunStep、Event/Audit。Runtime Provider、Hermes、ToolCall、Memory、Skill、Team、Plugin、业务 Web Console、正式认证与 SDK 均不进入 Goal C。

## 强制不变量

1. 所有租户业务表携带不可变 `tenant_id`，跨对象引用使用包含 `tenant_id` 的复合外键。
2. Repository 事务必须先应用 `TenantContext`；运行角色受 PostgreSQL FORCE RLS 约束。
3. 状态只能通过纯领域转换函数改变；非法转换返回 `INVALID_STATE_TRANSITION`。
4. 状态变化、Event、AuditRecord 在同一数据库事务提交。
5. AgentVersion 发布后不可变；Run 终态不可逆；重试创建递增 attempt 的新 Run。
6. PostgreSQL 保存待执行 Run 与租约字段；Goal C 不领取、心跳或执行 Run。
7. Event sequence 在租户内单调递增，Event/Audit/Run/RunStep 不提供物理删除 API。

## API 边界

- 开发/测试环境通过受信 `X-Tenant-ID` 和可选 `X-Actor-ID` 注入 TenantContext。
- 生产环境拒绝该开发上下文；Goal K 的 API Key/JWT 才是正式身份来源。
- 租户初始化使用仅限开发/测试的 bootstrap API；领域请求体不接受 `tenant_id`。
- 实现 Agent CRUD/克隆/归档/恢复、版本创建/发布/回滚、Project CRUD、Task 创建/读取/转换、Run 创建/读取/取消/重试、RunStep 写入/转换、Run Event 与 Audit 查询。

## 验收出口

- 纯领域单测覆盖每个合法转换、代表性非法转换、终态和 optimistic revision。
- Alembic 从 Goal B head 升级、降级、重放；Schema/角色/RLS 与复合外键断言通过。
- 两个真实租户的数据互不可见，跨租户引用和缺失 TenantContext 均失败。
- API 集成证明 Agent/Version、Task、多次 Run/Step、Event/Audit 持久化且原子。
- `scripts/verify-goal-c.sh` 依次运行本地回归、真实依赖集成和核心 API E2E。

## 阶段成果

完成后更新 Goal Status、Feature Matrix、API/架构/状态机文档，保存 `artifacts/goals/goal-c/<timestamp>/`，并创建 Goal C Handoff。只有全部出口通过才标记 Verified。
