# Nico Agent Platform

Nico Agent Platform 是一个通用、可扩展、多租户的成长型 Agent 服务平台。平台核心只提供 Agent 生命周期、结构化任务与可恢复 Run、工具、记忆、技能、团队工作流、插件、权限和审计；量化、软件研发、科研等领域能力通过独立插件接入。

当前状态：**Goal F（Memory 与 Skill）进行中，F4 候选生成已完成**。Goal E 的 Runtime、Tool Gateway、工具与 Sandbox 已通过验收；Goal F 已落地租户安全成长模型、确定性 `vector(384)` 检索，以及终态 Run/Step/ToolCall/Runtime 脱敏快照、provider-neutral 反思合同和幂等 Memory/Skill 候选生成。候选仍不可召回或发布；验证、审批、发布/回滚服务与 API 将从 F5 起逐项实现。Team、Plugin 和量化业务仍未实现。

## 快速启动

要求 Docker Engine 与支持 Compose Specification 的 Docker Compose v2。首次启动：

```bash
cp .env.example .env
scripts/dev.sh --detach
```

默认入口：

- Web 状态页：<http://localhost:18080>
- API 健康检查：<http://localhost:18000/api/v1/health/ready>
- OpenAPI UI：<http://localhost:18000/docs>
- MinIO Console：<http://localhost:19011>

这些发布端口特意避开常见的 `5432`、`6379`、`8000` 和 `9000`，便于与量化公司项目并行运行；可在 `.env` 中覆盖。容器内部仍使用标准端口。

停止环境但保留数据：

```bash
scripts/cleanup.sh
```

删除容器和数据卷：

```bash
scripts/cleanup.sh --volumes
```

## 需求基线

- [通用成长型多 Agent 服务平台实施任务书](docs/plans/agent-platform-implementation-taskbook.md)
- [Goal A 阶段计划](docs/plans/goal-a-architecture-baseline-plan.md)
- [Goal B 阶段计划](docs/plans/goal-b-infrastructure-plan.md)
- [Goal C 阶段计划](docs/plans/goal-c-core-domain-plan.md)
- [Goal D 阶段计划](docs/plans/goal-d-runtime-provider-plan.md)
- [Goal E 阶段计划](docs/plans/goal-e-tool-sandbox-plan.md)
- [Goal F 阶段计划](docs/plans/goal-f-memory-skill-plan.md)
- [阶段路线图](docs/roadmap.md)
- [Goal 状态](docs/progress/goal-status.md)
- [功能矩阵](docs/progress/feature-matrix.md)

## 架构文档

- [仓库勘察与缺口分析](docs/repository-assessment.md)
- [总体架构](docs/architecture.md)
- [领域模型](docs/domain-model.md)
- [状态机](docs/state-machines.md)
- [开发与运行](docs/development.md)
- [基础 API](docs/api.md)
- [Runtime 与 Worker](docs/runtime.md)
- [Tool Gateway 与 Sandbox](docs/tool-gateway.md)
- [Memory 与 Skill](docs/memory-and-skill.md)
- [测试策略](docs/testing.md)
- [架构决策](docs/decisions/)

## 验证

本地单元测试与前端构建：

```bash
scripts/test.sh
```

完整 Goal E 验收（Goal D 全量回归、Tool/Sandbox 安全测试与 Compose 工具链 E2E）：

```bash
NICO_EVIDENCE_DIR=artifacts/goals/goal-e/<timestamp> scripts/verify-goal-e.sh
```

Goal A 架构基线仍可独立验证：

```bash
bash scripts/verify-goal-a.sh
```

Goal F 当前已完成 F1–F4，下一增量是 F5 验证、Evaluation、Approval 与 Memory 生命周期服务。开始前必须阅读 Goal E Handoff、Goal F 计划、Memory/Skill 边界、ADR-0009/0010 和 Feature Matrix。不得绕过 Tool Gateway、让反思器执行工具、把 Candidate 加入正式召回，或在 Goal G 前伪造 Team scope。
