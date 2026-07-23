# Goal 状态

最后更新：2026-07-23 UTC

| Goal | 状态 | 完成比例 | 验收结果 | 证据目录 | 当前阻塞 |
| ---- | -- | ---: | ---- | ---- | ---- |
| A | Verified | 100% | `scripts/verify-goal-a.sh` 通过 | `artifacts/goals/goal-a/20260716T142433Z/` | 无 |
| B | Verified | 100% | `scripts/verify-goal-b.sh` 通过；故障/UI/清理验收通过 | `artifacts/goals/goal-b/20260716T154321Z/` | 无 |
| C | Verified | 100% | `scripts/verify-goal-c.sh` 通过；31 后端单测、7 前端测试、10 项真实集成与核心 API E2E 通过 | `artifacts/goals/goal-c/20260717T015402Z/` | 无 |
| D | Verified | 100% | `scripts/verify-goal-d.sh` 通过；47 后端单测、7 前端测试、18 项真实集成、Goal C/D Compose E2E 通过 | `artifacts/goals/goal-d/20260717T032610Z/` | 无 |
| E | Verified | 100% | `scripts/verify-goal-e.sh` 通过；138 后端单测、7 前端测试/构建、31 项真实集成、Goal C/D/E Compose E2E 与 Hermes 0.18.2 MCP 边界通过 | `artifacts/goals/goal-e/20260717T063841Z/` | 无 |
| F | Verified | 100% | `scripts/verify-goal-f.sh` 通过；172 后端单测、7 前端测试/构建、58 项真实集成及 Goal C/D/E/F Compose E2E 全绿 | `artifacts/goals/goal-f/20260717T084217Z/` | 无 |
| G | Implemented | 95% | 协议 v2、Model Gateway、默认 Nico Native Direct、持久化、SSE 与无 Hermes 的 hermetic Compose 验收已通过 | `artifacts/goals/goal-g/20260718T171138Z/` | 缺少操作者提供的真实模型端点、模型和 credential ref，按计划不得标为 Verified |
| H | Implemented | 95% | `scripts/verify-goal-h.sh` 通过；205 后端单测、7 前端测试/构建、63 项真实集成、迁移往返、Direct 回归和 Worker SIGKILL ReAct 恢复全绿 | `artifacts/goals/goal-h/20260718T175454Z/` | 缺少操作者提供的真实模型端点、模型和 credential ref，按计划不得标为 Verified |
| I | Implemented | 95% | `scripts/verify-goal-i.sh` 通过；220 后端单测、7 前端测试/构建、65 项真实集成、迁移往返、Plan revision、Reflection/Replan、确定性 Completion、独立计费 judge、schema v3 工具恢复与 Compose E2E 全绿 | `artifacts/goals/goal-i/20260718T181557Z/` | 缺少操作者提供的真实模型端点、模型和 credential ref，按迁移计划不得标为 Verified |
| J | Implemented | 95% | `scripts/verify-goal-j.sh` 通过；228 项后端单测、7 项前端测试/构建、74 项真实集成、完整迁移往返、双 Child 并行、私有 Artifact 与 Worker 强杀恢复全绿 | `artifacts/goals/goal-j/20260718T192139Z/` | 缺少操作者提供的真实模型端点、模型和 credential ref，按迁移计划不得标为 Verified |
| K | Implemented | 95% | `scripts/verify-goal-k.sh` 通过；231 项后端单测、7 项前端测试/构建、75 项真实集成、完整迁移往返、已发布 Memory/Skill 冻结召回、候选排除、效果追踪与 Compose E2E 全绿 | `artifacts/goals/goal-k/20260718T195512Z/` | 缺少操作者提供的真实模型端点、模型和 credential ref，按迁移计划不得标为 Verified |
| L | Implemented | 95% | `scripts/verify-goal-l.sh` 通过；234 项后端单测、11 项前端测试/构建、75 项真实集成、完整迁移往返、Goal G–L 全路径与 Hermes 默认禁用/可选 v2 Adapter 验收全绿 | `artifacts/goals/goal-l/20260718T201512Z/` | 缺少操作者提供的真实外部模型与 Hermes Provider 凭据，不能声称 credentialed inference 已验证 |
| CLI-A | Verified | 100% | 仓库审计、差距分析、总体架构、3 个 coin-cat 候选及最终选择、6 份 ADR、分阶段计划、矩阵与 Handoff 已完成；`scripts/verify-cli-goal-a.sh` 通过 | `artifacts/goals/cli-goal-a/20260719T100024Z/` | 无；按阶段约束未实现 CLI 业务代码 |
| CLI-B | Verified | 100% | `nico` 入口、私有 profile、HTTP client、human/JSON/no-color 输出、health/doctor/version/config 及 Project/Agent/Task/Run 查询已实现；251 项后端单测、11 项前端测试、75 项集成测试与真实 CLI/API E2E 通过 | `artifacts/goals/cli-goal-b/20260719T101257Z/` | 无；正式 API 身份认证仍是平台既有缺口，不阻塞受信网络 CLI-B |
| CLI-C | Verified | 100% | Conversation/Turn、ContextSnapshot 关联、0017 迁移/API、冻结版本 retry、最小 chat、SSE、resume/continue/history 与 Ctrl+C 权威取消已实现；268 项后端单测、11 项前端测试/构建、78 项真实集成、迁移往返、CLI-B 回归与双轮/SIGINT E2E 通过 | `artifacts/goals/cli-goal-c/20260719T104013Z/` | 无；正式认证仍是平台既有缺口，不阻塞受信网络 CLI-C |
| CLI-D | Verified | 100% | exec/input/output/detach、run watch/cursor、24 条 slash commands、Rich 视图、header 与 coin-cat 已实现；280 后端单测、11 前端测试/构建、78 集成、迁移往返、CLI-B/C 回归与 CLI-D PTY E2E 全绿 | `artifacts/goals/cli-goal-d/20260719T111600Z/` | 无；summary、附件与审批按阶段属于 CLI-E/F |
| CLI-E | Verified | 100% | summary 独立 Run/ModelCall、冻结上下文选择、ContextSnapshot、受控附件物化、`/attach`、`/compact`、`/download` 已实现；283 项后端单测、11 项前端测试/构建、80 项真实集成、迁移往返、CLI-B/C/D 回归与 CLI-E PTY E2E 全绿 | `artifacts/goals/cli-goal-e/20260719T115130Z/` | 无；工具审批按阶段属于 CLI-F |
| CLI-F | Verified | 100% | 持久化工具审批、风险策略、Worker 挂起/唤醒、超时恢复、once/run/reject 决策、审计及 CLI 断线续接已实现；288 项后端单测、11 项前端测试/构建、81 项真实集成、完整迁移往返与 CLI-B/C/D/E/F Compose/PTY E2E 全绿 | `artifacts/goals/cli-goal-f/20260719T122323Z/` | 无；正式 API 身份认证仍是平台既有缺口，不阻塞受信网络 CLI-F |
| RH-A (U1) | Verified | 100% | Goal A verifier、665 项后端单测、11 项前端测试/构建、152 项真实集成、0027 迁移往返、7 项旧应用/新 schema 混跑、权限与审批竞态回归全绿 | `artifacts/goals/runtime-hardening-a/20260723T111151Z/` | 无；按 Goal 边界停止在 U1，未进入 U2 |

状态只允许 `Not Started`、`In Progress`、`Blocked`、`Implemented`、`Verified`。Goal G–L 采用 `docs/plans/2026-07-18-001-nico-native-runtime-architecture-migration-plan.md` 的迁移路线；六个阶段的代码、迁移、测试、Compose 验收、文档和 Handoff 已全部交付。Goal G–L 保留 95% 是对未提供外部凭据的诚实标记，不是未完成的仓库实现项。CLI-A–F 采用 `docs/plans/2026-07-19-001-nico-cli-first-class-interface-plan.md`，六个阶段均已按顺序完成实现与验收。领域 Team/Workflow 不属于 Nico core。

RH-A–RH-I2 采用 Runtime Hardening 统一计划。`Implemented` 表示该 U-ID 的代码、迁移
和 focused proof 已完成；只有计划规定的全量 gate 与证据复核完成后才升为
`Verified`。一个 Goal 只处理一个 U-ID。
