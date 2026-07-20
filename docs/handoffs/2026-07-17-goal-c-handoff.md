# Goal C Handoff：Agent、Task、Run 核心模型

## 1. 本阶段目标

在 Goal B 基础设施上交付通用控制面核心：Tenant/Project、Agent/AgentVersion、Task/Run/RunStep、Event/AuditRecord 的模型、CRUD、状态机、租户隔离和可重复验收；不提前接入 Runtime、Hermes、Tool、Memory、Skill、Team 或量化业务。

## 2. 实际完成内容

- 实现无框架依赖的 Agent、AgentVersion、Project、Task、Run、RunStep 状态机与稳定错误码。
- 建立九张核心业务表、状态约束、查询索引、乐观 `revision`、Run attempt/lease/checkpoint/budget 字段。
- 所有对象关系使用包含 `tenant_id` 的复合外键；租户表之外的业务表启用 PostgreSQL `ENABLE/FORCE RLS`。
- 创建无 `SUPERUSER/BYPASSRLS` 的 `nico_runtime` 角色；租户事务以 `SET LOCAL ROLE` 和事务级 `app.tenant_id` 应用上下文。
- 实现 Project、Agent/Version、Task、Run/Step 的事务服务和 REST API。
- 实现 Agent 克隆、归档、恢复、Draft 删除、版本发布与历史回滚；版本内容以 Hash 固定。
- 实现 Task 与 Run 分离、多次 Run、取消/重试、RunStep 结果、Run Event 和租户 Audit 查询。
- 状态变化与 Event/AuditRecord 在同一数据库事务提交，失败和 revision 冲突不会留下部分状态。
- 开发/测试使用显式 Tenant Header；生产环境拒绝临时上下文和 bootstrap。
- 增加真实 PostgreSQL API 集成与完整核心 HTTP E2E。

## 3. 未完成内容

- Worker 领取、租约心跳、失联恢复、幂等执行与调度公平性。
- AgentRuntimeProvider、MockRuntimeProvider、HermesRuntimeProvider、RuntimeSession 与轨迹导出。
- ModelCall、ToolDefinition/ToolCall、Sandbox、Artifact、Evaluation、Approval。
- Memory、Skill、Team、Workflow、Plugin、量化团队插件、SDK 和业务 Web Console。
- API Key/JWT、正式授权、生产 Secret、限额执行和公网部署加固。
- Project restore 路由；状态规则已预留，但不属于 Goal C 必需 CRUD 出口。

## 4. 主要新增文件

- `backend/src/nico_agent/domain/errors.py`、`states.py`、`models.py`。
- `backend/src/nico_agent/api_schemas.py`、`control_plane.py`、`domain_api.py`。
- `backend/migrations/versions/20260717_0002_core_control_plane.py`。
- `backend/tests/unit/test_domain_states.py`。
- `backend/tests/integration/test_control_plane_api.py`，并扩展 `test_infrastructure.py`。
- `scripts/e2e-goal-c.sh`、`scripts/verify-goal-c.sh`。
- `docs/plans/goal-c-core-domain-plan.md`、`docs/decisions/ADR-0007-development-tenant-context.md`。

## 5. 主要修改文件

- API 工厂接入 Database、领域路由、领域/数据冲突错误处理和开发 Header CORS。
- Database 增加 TenantContext、租户事务和管理事务。
- Alembic 环境注册 Goal C metadata。
- README、架构、领域、状态机、API、测试、Goal Status 与 Feature Matrix 更新为实际实现。
- 后端包和 API 版本提升到 `0.2.0`。

## 6. 数据库变更

Revision `20260717_0002` 创建 Tenant、Project、Agent、AgentVersion、Task、Run、RunStep、Event、AuditRecord，以及 `nico_runtime`、权限和 RLS policy。Agent 当前版本通过延迟创建的复合外键指向同租户同 Agent 的版本。迁移已验证从 Goal B head 升级、降到 base、再次升级；downgrade 会先解除 Agent/Version 循环外键。

所有租户业务表由数据库策略校验：

```sql
tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid
```

应用连接用户仍是迁移 Owner；只有租户事务切换到受限运行角色。Goal D 的全局 Run claimer 需要另行设计最小权限，不能直接复用 Owner 绕过 RLS。

## 7. API 变更

- 新增开发/测试租户 bootstrap 与 `X-Tenant-ID`/`X-Actor-ID` TenantContext。
- 新增 Project 创建、列表、读取、更新、归档。
- 新增 Agent 创建、列表、读取、更新、克隆、归档、恢复和条件删除。
- 新增 AgentVersion 创建、列表、发布和回滚。
- 新增 Task 创建、读取、状态转换；Run 创建、读取、转换、取消、重试。
- 新增 RunStep 创建/转换、Run Event 与 Audit 查询。
- OpenAPI 暴露以上契约；生产环境临时 Tenant Header 返回 403。

完整路径和错误语义见 `docs/api.md`。

## 8. 配置变更

没有新增 Secret 或外部服务。`NICO_ENVIRONMENT=development|test` 才允许临时 Tenant Header；`production` 明确拒绝。应用版本默认值为 `0.2.0`。数据库连接仍由 Goal B 的 `NICO_DATABASE_*` 提供。

## 9. 测试命令

```bash
scripts/test.sh
scripts/test-integration.sh
scripts/e2e-goal-c.sh
scripts/verify-goal-c.sh
scripts/cleanup.sh --volumes
```

## 10. 测试结果

- Ruff 与格式检查通过。
- 后端单元测试 31 passed；前端测试 7 passed，生产构建通过。
- 真实依赖集成 10 passed，包括迁移重放、角色/RLS、复合外键、双租户隔离和四组控制面 API 场景。
- 核心 API E2E 在 Compose 中完成 Tenant→Project→AgentVersion→Task→Run→RunStep→Event/Audit，并证明另一租户读取返回 404。
- 最终 `scripts/verify-goal-c.sh` 输出和环境版本归档在 `artifacts/goals/goal-c/20260717T015402Z/`。

## 11. 已知问题与边界

- 开发 Tenant Header 允许调用者自报租户，只用于本地和测试，不是认证；生产防线已有测试但正式身份属于 Goal K。
- API 服务返回 ORM 实例前依赖 `expire_on_commit=False`；时间戳使用客户端 UTC default/onupdate 与数据库 server default，避免事务关闭后的延迟加载。
- AgentVersion 的 JSON 字段目前由 Pydantic 基础类型约束；更严格的 Provider/Tool/Plugin Schema 在对应 Goal 引入。
- Event sequence 使用全局 Identity，但唯一约束和读取语义按租户；保证租户内单调，不保证连续。
- Goal C 只持久化 Run 和人工/API 状态转换；没有模型调用，也不宣称 Agent 已能自主执行。

## 12. 当前架构

FastAPI 路由调用 `ControlPlaneService`；每个 use case 开启一个 TenantContext 数据库事务，行锁与 revision 共同保护状态转换。PostgreSQL 保存当前状态、不可变版本、待执行 Run、Event 与 Audit。Redis 尚不承载领域权威状态，MinIO 尚无 Artifact 写入。Worker 与控制面共享包但不处理 Run。

## 13. 下一阶段入口

Goal D 建议顺序：

1. 阅读本 Handoff、最新 Goal Status/Feature Matrix、ADR-0002/0003/0006 和 Goal C 状态机。
2. 先定义与具体实现无关的 `AgentRuntimeProvider` async protocol、规范化事件和能力协商。
3. 实现确定性的 MockRuntimeProvider contract tests，再实现 PostgreSQL Run 领取、租约、心跳、取消和恢复。
4. 让 Worker 通过应用服务推进 Run/Step/Event，不允许 Provider 直接访问 ORM 或提交领域事务。
5. 将 Hermes 放在 Adapter/Provider 层，用同一 contract suite 验证；真实依赖不可用时必须有明确 skip/evidence，不能伪造。
6. 提供轨迹导出和 Runtime 故障/取消/重试集成验收。

## 14. 下一阶段禁止重复或提前实现的内容

- 不重建 Tenant/Agent/Task/Run 表、状态机、错误码或另一套数据库会话框架。
- 不让 Redis 成为 Run 唯一事实来源，不让 Worker 使用数据库 Owner 长期处理租户正文。
- 不把 Hermes 类型导入 domain/application，不让 Provider 直接修改 Task、Run、Event 或 Audit ORM。
- 不在 Goal D 顺带实现 Tool Gateway、Memory/Skill 成长、Team/Workflow 或量化策略。
- 不把 Agent “盈利”作为核心平台目标或自动成长审批依据；量化评价属于独立插件和后续 Evaluation。

## 15. 建议下一步任务

执行 Goal D：以 provider-neutral contract 和确定性 Mock 建立运行协议，再实现最小持久化 Worker 闭环，最后接 Hermes Adapter；每一步都必须复用 Goal C 的 TenantContext、Run 状态机、Event/Audit 原子事务和不可变 AgentVersion。
