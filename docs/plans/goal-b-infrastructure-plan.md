# Goal B：项目骨架与基础设施实施计划

- 状态：In Progress
- 日期：2026-07-16
- 基线提交：`6f0062a`
- 工作分支：`feat/agent-platform-goal-b`
- 输入：`docs/plans/agent-platform-implementation-taskbook.md` 的 Goal B

## 目标

交付一个不依赖业务假数据、可以从空环境启动的最小垂直切片：FastAPI API 和独立 Worker 共享 Python 包，React Web 读取真实健康 API，PostgreSQL/pgvector、Redis、MinIO 由 Compose 管理，Alembic 初始化平台扩展，并以一键脚本完成开发、测试、E2E 和清理。

## 范围

本阶段包含：

- Python 3.11+、FastAPI、SQLAlchemy Async、Alembic 工程；
- API 与基础设施监督 Worker 两个入口；
- Pydantic Settings、JSON 日志、请求关联 ID；
- liveness/readiness/OpenAPI；
- PostgreSQL 16 + pgvector、Redis 7.2、MinIO；
- React 19 + Vite 的真实基础设施状态页；
- Docker Compose、镜像、bootstrap/dev/test/e2e/cleanup；
- 后端与前端单元测试、真实依赖集成测试、HTTP/UI smoke E2E。

本阶段不包含：

- Tenant、Project、Agent、Task、Run 等业务表和 CRUD；
- 数据库租约执行循环、Hermes Runtime、Tool、Memory、Skill 或 Plugin；
- 静态伪造的业务页面或 Demo 数据。

## 版本基线

版本在 2026-07-16 从 PyPI、npm 和容器 Registry 核验。Python/Node 直接依赖精确锁定；基础镜像使用明确主/次版本，数据服务使用版本或多架构 Manifest Digest。

| 组件 | 版本 |
| --- | --- |
| FastAPI | 0.139.2 |
| SQLAlchemy | 2.0.51 |
| Alembic | 1.18.5 |
| Pydantic Settings | 2.14.2 |
| React / React DOM | 19.2.7 |
| Vite | 8.1.5 |
| PostgreSQL/pgvector | `pg16@sha256:1d533553…` |
| Redis | `7.2.14-alpine@sha256:dfa18828…` |
| MinIO / mc | Registry digest 固定 |

## 实施单元

1. 建立后端包、配置与测试基线。
2. 以失败测试定义 liveness、readiness、关联 ID 和日志契约，再实现 API/Worker。
3. 建立 Alembic 扩展迁移和真实依赖集成测试。
4. 以组件测试定义状态页加载/健康/降级状态，再实现 React 页面。
5. 建立镜像、Compose 和一键脚本。
6. 执行本地单测、Compose 集成、迁移、E2E、桌面/移动 UI 检查。
7. 归档命令、日志、API 输出、截图、版本和验收摘要，形成 Handoff。

## 验收契约

- `scripts/test.sh` 一键通过后端 lint/单测及前端测试/构建；
- `scripts/test-integration.sh` 在真实 PostgreSQL、Redis、MinIO 上验证迁移和 readiness；
- `scripts/e2e.sh` 从 Compose 构建启动后验证 API、OpenAPI、Web 与服务健康；
- `GET /api/v1/health/live` 不访问外部依赖并返回 200；
- `GET /api/v1/health/ready` 逐项报告依赖，全部健康返回 200，任一故障返回 503；
- Alembic `upgrade head` 后存在 `vector`、`pgcrypto` 扩展且没有 Goal C 业务表；
- Web 不使用硬编码健康结果，能表达加载、健康、降级和请求失败；
- 日志为单行 JSON，HTTP 响应带 `X-Request-ID`；
- `scripts/cleanup.sh` 可以停止环境，`--volumes` 可选择删除数据卷；
- Goal B 证据、状态、Feature Matrix 与 Handoff 完整且工作树已提交。

