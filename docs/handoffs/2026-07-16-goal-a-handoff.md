# Goal A Handoff：仓库勘察与架构基线

## 1. 本阶段目标

原样归档实施任务书，在零代码仓库上建立可验证、可接续的架构、领域、状态、决策和进度基线，不提前实现 Goal B–K。

## 2. 实际完成内容

- 任务书原样归档并完成逐字节 SHA-256 验证。
- 完成初始仓库能力清单、可复用资产、缺口和风险分析。
- 确定模块化单体 API、独立持久化 Worker、React Web 和插件目录的目标拓扑。
- 确定 PostgreSQL/pgvector 权威状态、Redis 事件扇出、MinIO Artifact 存储。
- 定义 Runtime Provider、Tool/Sandbox、Memory/Skill 成长、Plugin 和多租户边界。
- 定义任务书要求的 20 个核心对象及 Tenant、Project、RuntimeSession、ModelCall 等补充对象。
- 定义 Agent、Task、Run、Skill 和 Approval 状态机基线。
- 建立 A–K 路线图、Goal Status、Feature Matrix、六项 ADR 和验收证据。
- 新增并执行 `scripts/verify-goal-a.sh`。

## 3. 未完成内容

Goal B–K 全部未开始。仓库没有 FastAPI、React、数据库、迁移、Docker Compose、业务 API、Runtime、Hermes、工具、Memory、Skill、Team、Plugin、SDK 或 Web Console 实现。

## 4. 新增文件

- `docs/plans/agent-platform-implementation-taskbook.md`
- `docs/plans/goal-a-architecture-baseline-plan.md`
- `docs/repository-assessment.md`
- `docs/architecture.md`
- `docs/domain-model.md`
- `docs/state-machines.md`
- `docs/roadmap.md`
- `docs/decisions/ADR-0001` 至 `ADR-0006`
- `docs/progress/goal-status.md`
- `docs/progress/feature-matrix.md`
- `scripts/verify-goal-a.sh`
- `artifacts/goals/goal-a/20260716T142433Z/*`

## 5. 修改文件

- `README.md`：从单行占位更新为项目边界、状态和文档导航。

## 6. 数据库变更

无。Goal A 只完成模型和存储决策，没有创建数据库、Schema 或迁移。

## 7. API 变更

无。Goal A 不提供静态占位 API。Goal B 仅建立健康检查与 OpenAPI 基础；领域 API 在 Goal C 以后实现。

## 8. 配置变更

无运行配置。尚未创建 `.env.example`、应用 Settings 或 Compose；这些属于 Goal B。

## 9. 测试命令

```bash
bash -n scripts/verify-goal-a.sh
bash scripts/verify-goal-a.sh
```

## 10. 测试结果

两条命令均通过。验收输出：

```text
PASS Goal A architecture baseline
taskbook_sha=7088fddfb7fdbe6e43f81db1225f6e984ffc0c0ebd98904ef310f02b371ad604
evidence_dir=artifacts/goals/goal-a/20260716T142433Z
```

## 11. 已知问题

- 任务书包含“唯一 ID；名称；-显示名称”的原始排版瑕疵；为保证原样归档未修改任务书，领域模型按名称和显示名称两个字段解释。
- Postgres 租约 Worker 在高吞吐下可能成为瓶颈；ADR-0002 规定未来可用 Outbox 接入专用队列。
- Hermes 的 pause/resume 能力需在 Goal D 实测；不支持时必须通过 capability negotiation 明示，不得固定返回成功。
- Goal A 没有运行时行为，API、UI 和 E2E 测试不适用。

## 12. 当前架构

目标架构为单仓库模块化单体：FastAPI API 与持久化 Worker 分进程，共享领域/应用层；PostgreSQL/pgvector 为权威状态和向量库，Redis 做事件扇出，MinIO 存 Artifact；Runtime、Tool、Plugin 均通过受控接口接入。详见 `docs/architecture.md`。

## 13. 下一阶段入口

Goal B 开始顺序：

1. 阅读本 Handoff、`goal-status.md`、`feature-matrix.md` 和 ADR-0001/0002/0006。
2. 将 Goal B 标记 In Progress 并创建独立证据目录。
3. 建立 Python 3.11+ FastAPI/SQLAlchemy/Alembic 工程、React/Vite 工程和统一开发命令。
4. 建立 PostgreSQL + pgvector、Redis、MinIO、Docker Compose、配置、结构化日志和健康检查。
5. 提供 bootstrap/dev/test/cleanup 脚本和 Goal B 基础设施验收。

## 14. 下一阶段禁止重复实现的内容

- 不重新讨论或另建第二套平台拓扑、存储权威、Runtime 边界、插件信任、成长流程或租户策略；若证据迫使改变，先新增 ADR。
- 不复制任务书或创建平行的 Goal 状态/Feature Matrix。
- 不在 Goal B 提前实现完整 Agent/Task/Run 业务模型、Hermes、量化插件或静态 Web 功能。
- 不让 Redis 成为 Run 的唯一事实来源，不让 Hermes 代码进入领域层。

## 15. 建议下一步任务

执行 Goal B：先创建工程与 Compose 骨架，再以健康检查和迁移 smoke test 为最小垂直切片；验证宿主机开发与 Docker Compose 两条启动路径，最后更新状态、矩阵、证据和 Goal B Handoff。
