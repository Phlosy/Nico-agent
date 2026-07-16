# 分阶段路线图

路线图严格对应任务书 Goal A–K。每个 Goal 必须先读取上一阶段 Handoff，完成独立测试、证据和状态更新后才能进入下一阶段。

| Goal | 目标 | 主要交付物 | 进入条件 | 验证出口 |
| --- | --- | --- | --- | --- |
| A | 仓库勘察与架构基线 | 任务书、能力/缺口、架构、领域模型、状态机、ADR、计划、矩阵 | 初始仓库 | `scripts/verify-goal-a.sh` 通过；Handoff 完整 |
| B | 项目骨架与基础设施 | FastAPI、React/Vite、PostgreSQL/pgvector、Redis、MinIO、Alembic、日志、健康检查、Compose、脚本 | Goal A Verified | 本地与 Compose 健康检查；迁移可升降；基础测试通过 |
| C | Agent/Task/Run 核心 | Tenant/Project、Agent/Version、Task/Run/Step、Event/Audit、CRUD、状态机 | Goal B Verified | 单元与 API 集成测试覆盖 CRUD、合法/非法转换、持久化 |
| D | Runtime Provider | Provider Protocol、Mock、Hermes Adapter、Session、取消/恢复/轨迹 | Goal C Verified | Provider contract tests；Mock 集成；Hermes 可选真实集成证据 |
| E | Tool 与 Sandbox | Registry、权限、ToolCall、文件/HTTP/DB/报告/Python 工具、隔离、重试 | Goal D Verified | 权限拒绝、超时、错误、重试、路径/网络隔离集成测试 |
| F | Memory 与 Skill | 四类 Memory、作用域、pgvector 检索、Skill/Version、Candidate、审批、回滚 | Goal E Verified | 来源追踪、租户隔离、检索、版本发布/回滚测试 |
| G | Team 与 Workflow | Team/Role/Membership、显式工作流、委派、审核、退回、汇总 | Goal F Verified | 多 Agent 模拟流程及非法转换测试 |
| H | Plugin System | Manifest、发现/校验/加载/启停/兼容性、扩展注册、示例插件 | Goal G Verified | 插件安装、注册生效、禁用、版本冲突和失败测试 |
| I | Quant Team Plugin | 三角色、五工具、一个审核工作流、报告、E2E | Goal H Verified | 量化 E2E 通过；核心无量化依赖；禁止实盘 |
| J | Web Console | Agent/Team/Task/Run/Memory/Skill/Plugin/Artifact/Audit UI | Goal I Verified | 桌面/移动浏览器验收；Run SSE 可视化；无静态伪数据 |
| K | 安全、观测与最终验收 | Auth、限额、审计、指标、故障/恢复、性能基线、SDK、Demo、完整文档 | Goal J Verified | 全套单元/集成/E2E/故障/安全验收，一键启动与清理 |

## 实施约束

1. 每个 Goal 只实现本阶段能力；跨阶段所需接口可以设计，但不能用固定值伪装实现。
2. 行为变更优先测试先行；所有数据库变化必须有 Alembic migration。
3. API 变化同步 OpenAPI、Python SDK、TypeScript SDK；SDK 只走 HTTP/SSE。
4. 量化领域逻辑只位于 `plugins/quant-team`。
5. 自动成长只产生 Candidate，发布前必须验证、审批并可回滚。
6. Goal 状态只有在验收脚本和阶段测试通过后才能标记 Verified。

## Goal B 预定边界

Goal B 建立工程和基础设施，不创建完整 Agent/Task/Run 业务表。允许创建健康检查所需的最小迁移基线和通用配置/数据库连接层；所有领域 CRUD 留给 Goal C。
