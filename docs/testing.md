# 测试与验收

## 测试层级

| 层级 | 命令 | 覆盖 |
| --- | --- | --- |
| 后端单元 | `.venv/bin/pytest backend/tests/unit` | 配置/DSN 编码、探针、超时/安全失败、API 状态码、请求 ID、日志、Worker 监督 |
| 前端组件 | `npm --prefix frontend test` | API 运行时契约、加载、健康、503 降级、网络失败与手动重试 |
| 静态/构建 | `scripts/test.sh` | Ruff、后端单测、前端测试、TypeScript 与 Vite 生产构建 |
| 真实依赖 | `scripts/test-integration.sh` | pgvector/pgcrypto 升级—回滚—重放、Redis 往返、MinIO、真实 API lifespan |
| 整栈 E2E | `scripts/e2e.sh` | 镜像、Compose 依赖、容器健康、API/OpenAPI、Web、Worker、Bucket 初始化 |
| Goal B 总验收 | `scripts/verify-goal-b.sh` | 构建镜像，执行以上自动化出口，并证明迁移可回滚重放 |

集成测试默认跳过，只有 `RUN_INTEGRATION=1` 才运行；`test-integration.sh` 会准备真实依赖并设置该变量，因此不能把普通 pytest 的 skip 当成集成测试通过。

## 人工与故障验收

Goal B 的最终证据还包含：

- 1440px 与 390px 页面截图；
- 浏览器控制台错误和横向溢出检查；
- 停止 Redis 后 API 返回 503、Web 标记 Redis、Worker 输出 degraded warning；
- Redis 恢复后 API 自动回到 200；
- Alembic 从 head 降到 base 后再升级，扩展恢复；
- `cleanup.sh` 与 `cleanup.sh --volumes` 的清理验证。

## 证据规则

每个 Goal 在 `artifacts/goals/goal-<x>/<UTC timestamp>/` 保存命令、日志、响应、截图、版本、错误与验收摘要。证据必须对应实际执行，不能以固定文本代替测试输出。
