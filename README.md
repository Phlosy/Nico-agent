# Nico Agent Platform

Nico Agent Platform 是一个通用、可扩展、多租户的成长型 Agent 服务平台。平台核心只提供 Agent 生命周期、结构化任务与可恢复 Run、工具、记忆、技能、团队工作流、插件、权限和审计；量化、软件研发、科研等领域能力通过独立插件接入。

当前状态：**Goal A（仓库勘察与架构基线）已验证**。后端、前端和基础设施将在 Goal B 开始实现，当前仓库不声称具备任何运行时业务能力。

## 需求基线

- [通用成长型多 Agent 服务平台实施任务书](docs/plans/agent-platform-implementation-taskbook.md)
- [Goal A 阶段计划](docs/plans/goal-a-architecture-baseline-plan.md)
- [阶段路线图](docs/roadmap.md)
- [Goal 状态](docs/progress/goal-status.md)
- [功能矩阵](docs/progress/feature-matrix.md)

## 架构文档

- [仓库勘察与缺口分析](docs/repository-assessment.md)
- [总体架构](docs/architecture.md)
- [领域模型](docs/domain-model.md)
- [状态机](docs/state-machines.md)
- [架构决策](docs/decisions/)

## Goal A 验证

```bash
bash scripts/verify-goal-a.sh
```

Goal B 开始前必须先阅读最新 [Goal A Handoff](docs/handoffs/2026-07-16-goal-a-handoff.md)，不得跳过基础设施决策或提前实现 Goal C 及之后的领域能力。
