# 测试与验收

## 测试层级

| 层级 | 命令 | 覆盖 |
| --- | --- | --- |
| 后端单元 | `.venv/bin/pytest backend/tests/unit` | Goal A–E 回归，以及 Memory/Skill 状态机、切片/嵌入、轨迹快照、反思 DTO 与策略合同 |
| 前端组件 | `npm --prefix frontend test` | API 运行时契约、加载、健康、503 降级、网络失败与手动重试 |
| 静态/构建 | `scripts/test.sh` | Ruff、后端单测、前端测试、TypeScript 与 Vite 生产构建 |
| 真实依赖 | `scripts/test-integration.sh` | 迁移升级—回滚—重放、RLS、最小 claimer、Runtime/Tool，以及 Goal F 来源闭合、发布闸门、Hash 绑定、灰度唯一和终态不可变 |
| 整栈 E2E | `scripts/e2e.sh` | 镜像、Compose 依赖、容器健康、API/OpenAPI、Web、Worker、Bucket 初始化 |
| Goal B 总验收 | `scripts/verify-goal-b.sh` | 构建镜像，执行以上自动化出口，并证明迁移可回滚重放 |
| Goal C 核心 E2E | `scripts/e2e-goal-c.sh` | 真实 HTTP Tenant→Project→AgentVersion→Task→Run→Step→Event/Audit 生命周期与第二租户隔离 |
| Goal C 总验收 | `scripts/verify-goal-c.sh` | 构建、全量回归、真实依赖集成和核心控制面 E2E |
| Goal D Runtime E2E | `scripts/e2e-goal-d.sh` | HTTP 创建 Run，由独立 Compose Worker 经 PostgreSQL 租约和 Mock Provider 自动完成，再查询 Runtime/轨迹/Event/Audit |
| Goal D 总验收 | `scripts/verify-goal-d.sh` | Goal C 全量回归、47 后端单测、7 前端测试、18 真实集成、两个 Compose E2E 与 Hermes 边界检查 |
| Goal E Tool/Sandbox E2E | `scripts/e2e-goal-e.sh` | HTTP→Worker→Mock intent→Gateway→文件/报告/独立 Python 容器→ToolCall/Event/Audit/Trajectory/API；无残留 sandbox 容器 |
| Goal E 总验收 | `scripts/verify-goal-e.sh` | Goal D 完整回归、138 后端单测、7 前端测试、31 真实集成、三个 Compose E2E、固定镜像和真实 Hermes MCP 发现 |

集成测试默认跳过，只有 `RUN_INTEGRATION=1` 才运行；`test-integration.sh` 会准备真实依赖并设置该变量，因此不能把普通 pytest 的 skip 当成集成测试通过。

Goal E 当前基线是后端 138 项单元测试、真实依赖 31 项集成测试、Goal C/D/E 三条 Compose E2E。安全覆盖包括默认拒绝/权限交集、跨租户、Schema/Secret 脱敏、并发幂等、重试/超时/取消/租约丢失、路径遍历/符号与硬链接/竞态、SSRF/混合 DNS/重定向/rebinding、只读数据库角色，以及 Python 非 root/无网络/只读根/资源限制/清理。真实 Hermes 只验证无模型凭据的 MCP 工具发现，不冒充真实推理。

Goal F F6 当前回归为后端 170 项单元、前端 7 项及 57 项真实依赖集成测试。F5 覆盖冻结验证 DTO、验证器异常脱敏、最新评价、并发幂等、非自审审批和原子 Memory 生命周期。F6 新增覆盖 Skill 八区内容/精确工具差异、稳定分桶、不可变修订来源、首次发布、后续版本发布不自动切换指针、并发 canary 幂等、真实 Run scope 解析、稳定/命中分流、推广退役、弃用、同版本恢复、历史回滚、禁用、跨租户隐藏、scope 不扩张、陈旧 revision、审批后工具状态漂移，以及数据库拒绝自审发布、带 active canary 的停用/指针切换和越界 deployment。REST API 属于 F7，Goal F 整栈 E2E 属于 F8。

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
