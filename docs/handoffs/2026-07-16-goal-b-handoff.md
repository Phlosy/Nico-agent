# Goal B Handoff：项目骨架与基础设施

## 1. 本阶段目标

在 Goal A 架构基线上交付可运行、可测试、可验收的工程骨架：API、独立 Worker、Web、PostgreSQL/pgvector、Redis、MinIO、Alembic、配置、结构化日志、健康检查、Compose 和一键生命周期脚本；不提前实现 Goal C 的业务模型。

## 2. 实际完成内容

- 建立 Python 3.11+ 的 FastAPI/SQLAlchemy/Alembic 共享包。
- 建立 API 与独立 Worker 两个入口；Worker 当前只监督基础设施。
- 实现 Pydantic Settings、单行 JSON 日志、请求关联 ID 和 CORS 基线。
- 实现无依赖 liveness，以及并发、独立超时的 PostgreSQL/Redis/MinIO readiness。
- 建立首个 Alembic revision，只启用 `pgcrypto` 与 `vector`。
- 建立 React 19/Vite 状态页，读取真实 API 并表达加载、健康、503 降级和传输失败。
- 建立 PostgreSQL/pgvector、Redis、MinIO、Bucket 初始化、API、Worker、Web 七服务 Compose。
- 数据服务镜像使用版本与多架构 Digest；应用镜像分别使用 Python/Node/Nginx 构建。
- 提供 bootstrap、dev、test、test-integration、e2e、verify-goal-b、cleanup 七个脚本。
- 为避免与 `/home/node7/xpk/quantfirm-os` 等本机项目冲突，默认发布端口使用独立区间。

## 3. 未完成内容

- Tenant、Project、Agent、AgentVersion、Task、Run、RunStep、Event、Audit 等业务模型与 CRUD。
- TenantContext、运行角色、RLS 与跨租户隔离测试。
- PostgreSQL Run 租约领取、心跳、恢复与幂等执行。
- Runtime Provider、Hermes、Tool、Sandbox、Memory、Skill、Team、Plugin、SDK 与业务 Web Console。
- API Key/JWT、生产 Secret、指标与公网安全加固。

## 4. 主要新增文件

- `backend/pyproject.toml`、`backend/src/nico_agent/*`、后端单元/集成测试；
- `backend/alembic.ini` 与 `backend/migrations/*`；
- `frontend/package.json`、React 状态页、组件测试、Nginx 配置；
- `docker-compose.yml`、三个 Dockerfile/构建配置、`.env.example`；
- `scripts/bootstrap.sh`、`dev.sh`、`test.sh`、`test-integration.sh`、`e2e.sh`、`verify-goal-b.sh`、`cleanup.sh`；
- `docs/development.md`、`docs/api.md`、`docs/testing.md`；
- `artifacts/goals/goal-b/20260716T154321Z/*`。

## 5. 主要修改文件

- `README.md`：当前能力、启动、端口和验收入口。
- `docs/architecture.md`：Goal B 已实现切片与 Worker 的诚实边界。
- `docs/progress/goal-status.md`：Goal B 标记 Verified。
- `docs/progress/feature-matrix.md`：只将 Goal B 真实能力标记完成。

## 6. 数据库变更

Revision `20260716_0001`：

- 创建 `pgcrypto` 扩展；
- 创建 `vector` 扩展；
- Alembic 自动管理 `alembic_version`；
- 不创建任何 Goal C 业务表。

已在真实 PostgreSQL 上验证 `upgrade head`、`downgrade base`、再次 `upgrade head`。迁移 Owner/运行角色与 RLS 必须结合 Goal C 业务表实现，当前不得声称已具备租户数据库隔离。

## 7. API 变更

- `GET /api/v1/health/live`：200，不访问外部依赖。
- `GET /api/v1/health/ready`：全部健康返回 200；任一依赖异常返回 503 并逐项报告。
- `GET /openapi.json` 与 `GET /docs`。
- 响应带 `X-Request-ID`；日志关联相同 ID。

## 8. 配置变更

新增 `NICO_` 前缀 Settings，以及 `.env.example` 中 PostgreSQL、Redis、MinIO、API/Web 端口与开发凭证。默认宿主机端口：PostgreSQL 15432、Redis 16379、API 18000、Web 18080、MinIO 19010/19011。

## 9. 测试命令

```bash
scripts/test.sh
scripts/test-integration.sh
scripts/e2e.sh
scripts/verify-goal-b.sh
scripts/cleanup.sh --volumes
```

## 10. 测试结果

- 后端 Ruff 与格式检查：通过。
- 后端单元测试：16 passed。
- 前端组件/API 契约测试：7 passed；TypeScript/Vite 生产构建通过；npm audit 0 vulnerabilities。
- 真实依赖集成：4 passed。
- Compose/E2E：API、Web、OpenAPI、Worker、Bucket 初始化和容器健康全部通过。
- UI：1440px 与 390px 无横向溢出、零 console/page error、三张真实组件卡完整。
- 故障：Redis 停止后 API 503、Web 降级、Worker warning；恢复后 API 200。
- 清理：容器、网络和数据卷全部删除。

完整输出在 `artifacts/goals/goal-b/20260716T154321Z/`。

## 11. 已知问题与边界

- 开发凭证不能用于共享或生产环境；生产 Secret 管理属于后续安全 Goal。
- 健康接口尚无认证，仅应位于开发或受信运维网络。
- 探针与 500 响应不回显异常原文；数据库组件配置通过 SQLAlchemy URL 构造器编码，避免特殊字符破坏 DSN。
- 应用/Uvicorn/Worker 日志是 JSON；Alembic CLI 保留标准文本日志。
- MinIO 固定到 `RELEASE.2025-09-07T16-13-09Z` 对应 Digest，升级前需重跑集成和故障测试。
- Python 直接依赖精确固定，传递依赖由安装时解析；后续发布流程应引入完整 lock/哈希供应链。
- Worker 没有 Run 队列能力；它是基础设施监督进程，不是 Mock Runtime。

## 12. 当前架构

API 与 Worker 共享 `nico_agent` 包但分进程运行；PostgreSQL/pgvector、Redis、MinIO 分别承担未来权威状态、事件协调和 Artifact 存储。当前数据库只有扩展和迁移版本，Web 只展示真实基础设施状态。

## 13. 下一阶段入口

Goal C 开始顺序：

1. 阅读本 Handoff、最新 Goal Status、Feature Matrix 与 ADR-0001/0002/0006。
2. 在本阶段迁移基线上新增 Tenant、Project、Agent/Version、Task、Run/Step、Event/Audit。
3. 先建立 TenantContext、复合租户外键、数据库运行角色与 FORCE RLS 测试基线。
4. 实现 CRUD 与显式状态机，非法转换必须可测试拒绝。
5. 实现 PostgreSQL 权威的待执行 Run/租约表基础，但不在 Goal C 接入 Hermes。
6. 以 API 集成测试证明持久化、租户隔离、事件原子性与多次 Run。

## 14. 下一阶段禁止重复或提前实现的内容

- 不重建第二套配置、日志、健康、Compose 或数据库连接框架；在现有包上演进。
- 不把 Redis 改成 Run 的权威来源。
- 不让 SQLAlchemy/FastAPI 进入纯领域规则。
- 不在 Goal C 接入 Hermes、Tool、Memory、Skill 或量化插件。
- 不删除 Goal B 的“无业务表”集成断言而不以 Goal C 的明确 Schema 断言替换。

## 15. 建议下一步任务

执行 Goal C：先以迁移与隔离失败测试建立 Tenant/Project 边界，再实现 Agent/Task/Run 状态机和 API 垂直切片；保留当前健康状态页作为运行环境入口，业务管理页面仍留给后续 Web Console Goal。
