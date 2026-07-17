# 测试与验收

## 测试层级

| 层级 | 命令 | 覆盖 |
| --- | --- | --- |
| 后端单元 | `.venv/bin/pytest backend/tests/unit` | 配置/探针/API/日志/Worker，以及全部核心状态机、非法终态、乐观 revision、生产租户 Header 防线与 OpenAPI 契约 |
| 前端组件 | `npm --prefix frontend test` | API 运行时契约、加载、健康、503 降级、网络失败与手动重试 |
| 静态/构建 | `scripts/test.sh` | Ruff、后端单测、前端测试、TypeScript 与 Vite 生产构建 |
| 真实依赖 | `scripts/test-integration.sh` | 迁移升级—回滚—重放、Schema/角色/FORCE RLS/复合外键、双租户隔离、Agent/Version、Task/多 Run/Step、Event/Audit 与事务冲突 |
| 整栈 E2E | `scripts/e2e.sh` | 镜像、Compose 依赖、容器健康、API/OpenAPI、Web、Worker、Bucket 初始化 |
| Goal B 总验收 | `scripts/verify-goal-b.sh` | 构建镜像，执行以上自动化出口，并证明迁移可回滚重放 |
| Goal C 核心 E2E | `scripts/e2e-goal-c.sh` | 真实 HTTP Tenant→Project→AgentVersion→Task→Run→Step→Event/Audit 生命周期与第二租户隔离 |
| Goal C 总验收 | `scripts/verify-goal-c.sh` | 构建、全量回归、真实依赖集成和核心控制面 E2E |

集成测试默认跳过，只有 `RUN_INTEGRATION=1` 才运行；`test-integration.sh` 会准备真实依赖并设置该变量，因此不能把普通 pytest 的 skip 当成集成测试通过。

Goal C 当前基线是后端 31 项单元测试、真实依赖 10 项集成测试，以及独立核心 API E2E。Run Worker/Runtime、模型失败、工具超时和 Worker 恢复等故障测试从 Goal D/E 进入，不能由仅持久化状态转换的测试冒充。

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
