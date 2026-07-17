# 测试与验收

## 测试层级

| 层级 | 命令 | 覆盖 |
| --- | --- | --- |
| 后端单元 | `.venv/bin/pytest backend/tests/unit` | 配置/探针/API/日志/Worker、状态机，以及 Provider contract、Mock 状态/取消/轨迹、Hermes 进程/脱敏/恢复/取消边界 |
| 前端组件 | `npm --prefix frontend test` | API 运行时契约、加载、健康、503 降级、网络失败与手动重试 |
| 静态/构建 | `scripts/test.sh` | Ruff、后端单测、前端测试、TypeScript 与 Vite 生产构建 |
| 真实依赖 | `scripts/test-integration.sh` | 迁移升级—回滚—重放、RLS、最小 claimer、并发领取、优先级、租约过期接管、旧 token 拒绝、检查点恢复、取消/失败与 Runtime 原子持久化 |
| 整栈 E2E | `scripts/e2e.sh` | 镜像、Compose 依赖、容器健康、API/OpenAPI、Web、Worker、Bucket 初始化 |
| Goal B 总验收 | `scripts/verify-goal-b.sh` | 构建镜像，执行以上自动化出口，并证明迁移可回滚重放 |
| Goal C 核心 E2E | `scripts/e2e-goal-c.sh` | 真实 HTTP Tenant→Project→AgentVersion→Task→Run→Step→Event/Audit 生命周期与第二租户隔离 |
| Goal C 总验收 | `scripts/verify-goal-c.sh` | 构建、全量回归、真实依赖集成和核心控制面 E2E |
| Goal D Runtime E2E | `scripts/e2e-goal-d.sh` | HTTP 创建 Run，由独立 Compose Worker 经 PostgreSQL 租约和 Mock Provider 自动完成，再查询 Runtime/轨迹/Event/Audit |
| Goal D 总验收 | `scripts/verify-goal-d.sh` | Goal C 全量回归、47 后端单测、7 前端测试、18 真实集成、两个 Compose E2E 与 Hermes 边界检查 |

集成测试默认跳过，只有 `RUN_INTEGRATION=1` 才运行；`test-integration.sh` 会准备真实依赖并设置该变量，因此不能把普通 pytest 的 skip 当成集成测试通过。

Goal D 当前基线是后端 47 项单元测试、真实依赖 18 项集成测试、Goal C 核心 API E2E 和 Goal D Runtime Worker E2E。已覆盖 Mock 成功/模型失败/取消、未知 Provider、并发 claim、租约过期恢复与迟到结果拒绝。Tool 权限、工具超时/重试和 Sandbox 属于 Goal E，不能由 Hermes 原生工具或 Mock 事件冒充。

## 人工与故障验收

历史 Goal B 的最终证据还包含：

- 1440px 与 390px 页面截图；
- 浏览器控制台错误和横向溢出检查；
- 停止 Redis 后 API 返回 503、Web 标记 Redis、Worker 输出 degraded warning；
- Redis 恢复后 API 自动回到 200；
- Alembic 从 head 降到 base 后再升级，扩展恢复；
- `cleanup.sh` 与 `cleanup.sh --volumes` 的清理验证。

## 证据规则

每个 Goal 在 `artifacts/goals/goal-<x>/<UTC timestamp>/` 保存命令、日志、响应、截图、版本、错误与验收摘要。证据必须对应实际执行，不能以固定文本代替测试输出。
