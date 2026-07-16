# 仓库勘察与缺口分析

## 勘察范围

- 仓库：`Nico-agent`
- 基线提交：`489899e`（Initial commit）
- 勘察日期：2026-07-16 UTC
- 需求来源：`docs/plans/agent-platform-implementation-taskbook.md`

## 当前能力清单

勘察时仓库仅包含一行 `README.md`，没有应用代码或工程配置。经文件扫描和内容搜索，确认：

| 能力 | 当前状态 | 证据 |
| --- | --- | --- |
| Git 仓库与远程 | 已存在 | `main` 跟踪 `origin/main` |
| 项目说明 | 极小占位 | 初始 `README.md` 仅含标题 |
| Python/FastAPI 后端 | 未实现 | 无 `pyproject.toml`、Python 包或 API |
| React/TypeScript 前端 | 未实现 | 无 `package.json` 或前端目录 |
| PostgreSQL/Redis/MinIO | 未实现 | 无 Compose、迁移或配置 |
| Agent/Task/Run 模型 | 未实现 | 无领域代码或数据库模型 |
| Runtime Provider | 未实现 | 无接口或 Adapter |
| Hermes 集成 | 未包含 | 无 Hermes 依赖、源码、配置或调用 |
| Tool/Sandbox | 未实现 | 无工具注册与隔离执行代码 |
| Memory/Skill | 未实现 | 无存储、检索、版本或审批代码 |
| Team/Workflow | 未实现 | 无编排或状态机代码 |
| Plugin System | 未实现 | 无 manifest 或插件加载器 |
| SDK/Web Console | 未实现 | 无 SDK 或 UI |
| 测试与脚本 | 未实现 | 无测试框架或执行脚本 |

## 可复用资产

初始仓库没有代码资产可复用。可复用的唯一资产是 Git 历史和本任务书形成的需求边界。后续实现不得复制其他项目的领域代码作为核心平台捷径；可以借鉴接口模式，但必须保持领域插件隔离。

## 关键缺口

1. 缺少可启动工程和开发环境。
2. 缺少多租户身份与数据隔离边界。
3. 缺少持久化领域模型、迁移和受约束状态机。
4. 缺少可替换 Runtime Provider 及 Hermes Adapter。
5. 缺少持久化 Worker、取消、恢复和幂等语义。
6. 缺少工具权限、沙箱、网络/文件边界和 Secret 隔离。
7. 缺少带来源、候选、审批、版本和回滚的 Memory/Skill。
8. 缺少 Team 显式工作流和审核退回机制。
9. 缺少可信插件注册、兼容性和启停机制。
10. 缺少 API、SSE、SDK、Web Console、测试及验收证据。

## 风险

- **范围风险**：任务书覆盖完整平台，必须严格按 Goal A–K 推进，避免骨架阶段混入后续业务。
- **安全风险**：Hermes Profile 或 Prompt 不能视为安全边界；工具权限与沙箱必须由平台执行。
- **一致性风险**：Task、Run、Event、ToolCall 等必须以 PostgreSQL 为权威，不能只存在于 Worker 内存或 Redis。
- **成长污染风险**：轨迹生成的 Memory/Skill 只能成为 Candidate，未经验证与批准不得发布。
- **领域耦合风险**：量化角色、指标、回测和报告只能位于 `plugins/quant-team`。
- **伪完成风险**：所有占位能力必须在 Feature Matrix 标记，禁止固定返回值冒充实现。

## Goal A 结论

仓库属于绿地项目。采用单仓库、模块化单体控制面、独立持久化 Worker、独立 Web Console 与插件目录是第一版风险最低的起点。详细选择见总体架构和 ADR；Goal B 只建立可启动基础设施，不提前实现 Goal C 的业务模型。
