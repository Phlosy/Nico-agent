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
| RH-B1 (U2 projection) | Verified | 100% | 配套 CC-D/CC-E 共同完成 provider-neutral AgentAction 合同、纯解析、完整批次持久化、安全投影与 ModelCall→Action 崩溃补写；724 项后端单测、161 项真实 PostgreSQL 集成和 `0030` 迁移往返全绿 | `artifacts/goals/conversation-continuity-d/20260723T165848Z/`、`artifacts/goals/conversation-continuity-e/20260723T171841Z/` | 无；RH-B2 仍由 CC-F / U6 单独投影 |
| RH-B2 (U10 projection) | Verified | 100% | 配套 CC-F 完成 Native final/Tool/Artifact/Delegation 统一 Action dispatcher、提交屏障、逐 ordinal cursor、unknown fail-closed 与 Provider v2 隔离；731 项后端单测、162 项真实 PostgreSQL 集成全绿 | `artifacts/goals/conversation-continuity-f/20260723T174317Z/` | 无；澄清 wait 与 Completion Gate 仍由后续 CC-G–CC-J 独立交付 |
| RH-H (U8 projection) | In Progress | 20% | 配套 CC-J 已提供共享 `semantic-completion-v1` 扩展点及 answered/user-response/pending-input 语义底线；不把该投影视为完整 obligation-aware Completion Gate | `artifacts/goals/conversation-continuity-j/20260723T190520Z/` | Tool、approval、Artifact、预算及其他数据库 obligation 仍由 parent RH-H 后续实现 |
| CC-A (U1) | Verified | 100% | 可执行请求审计确认历史 role 丢失、当前输入重复和隐式 final 三段因果链；2 项聚焦单测与 158 项真实 PostgreSQL 集成通过 | `artifacts/goals/conversation-continuity-a/20260723T162052Z/` | 无；CC-B / U2 dependency-ready，但按 Goal 边界未进入 |
| CC-B (U2) | Verified | 100% | Goal B verifier 通过；历史 Turn 标准 role、当前输入单点渲染、typed source/trust、结构化输出有界归一化和 v1 冻结恢复兼容完成；696 项后端单测与 158 项真实 PostgreSQL 集成通过 | `artifacts/goals/conversation-continuity-b/20260723T164205Z/` | 无；按用户连续执行约定自动进入 CC-C / U3 |
| CC-C (U3) | Verified | 100% | Goal C verifier 通过；Direct/ReAct/Planner/Plan Step/Reflection/修复轮次共用 v1 continuity policy；48 项聚焦测试、699 项后端单测与 158 项真实 PostgreSQL 集成通过 | `artifacts/goals/conversation-continuity-c/20260723T164925Z/` | 无；按用户连续执行约定自动进入 CC-D / U4 |
| CC-D (U4) | Verified | 100% | Goal D verifier 通过；immutable AgentAction DTO、纯 parser、稳定错误码、Provider-neutral action identity、capability-filtered schema 与 legacy plain-text final 兼容完成；66 项聚焦测试、721 项后端单测与 158 项真实 PostgreSQL 集成通过 | `artifacts/goals/conversation-continuity-d/20260723T165848Z/` | 无；已与 CC-E 共同投影 parent RH-B1 |
| CC-E (U5) | Verified | 100% | Goal E verifier 通过；`0030` 完整 Action 批次/ordinal/repair 持久化、同 Tenant 复合引用、FORCE RLS、不可变 Intent、安全 API 投影和终态 ModelCall 无重复计费补写完成；62 项聚焦测试、724 项后端单测与 161 项真实 PostgreSQL 集成通过 | `artifacts/goals/conversation-continuity-e/20260723T171841Z/` | 无；parent RH-B1 已投影 Verified，按用户连续执行约定自动进入 CC-F / U6 |
| CC-F (U6) | Verified | 100% | Goal F verifier 通过；统一 Action dispatcher、持久化提交屏障、权威结果后 cursor 推进、首个未决 ordinal 恢复、unknown/terminal 阻断、final-only shadow schema 与越权 ask_user 结构化拒绝完成；72 项聚焦测试、731 项后端单测、11 项前端测试/构建与 162 项真实 PostgreSQL 集成通过 | `artifacts/goals/conversation-continuity-f/20260723T174317Z/` | 无；parent RH-B2 已投影 Verified，按用户连续执行约定自动进入 CC-G / U7 |
| CC-G (U7) | Verified | 100% | Goal G verifier 通过；`0031` UserInputRequest、受保护答案、JSON Schema/revision/幂等校验、pre-action checkpoint、等待/释放租约、answer/expiry/cancel wake、answered-but-unwoken 调和与 Action cursor 恢复完成；742 项后端单测、11 项前端测试/构建、迁移往返和 169 项真实 PostgreSQL 集成通过 | `artifacts/goals/conversation-continuity-g/20260723T180458Z/` | 无；live `ask_user` Schema 仍关闭，按用户连续执行约定自动进入 CC-H / U8 |
| CC-H (U8) | Verified | 100% | Goal H verifier 通过；tenant-safe UserInput list/get/answer、稳定 revision/Schema/幂等错误、CLI Agent-question 独立 interrupt、启动/SSE 恢复、问题/审批/排队 Turn/composer 单一 owner 与 PTY 断线恢复完成；751 项后端单测、11 项前端测试/构建、迁移往返和 171 项真实 PostgreSQL 集成通过 | `artifacts/goals/conversation-continuity-h/20260723T182154Z/` | 无；live `ask_user` Schema 仍关闭，按用户连续执行约定自动进入 CC-I / U9 |
| CC-I (U9) | Verified | 100% | Goal I verifier 通过；版本化结构化 Clarification Gate、必要提问 durable wait、不必要提问一次无 Tool correction、稳定耗尽、源 Action repair、Direct/ReAct 崩溃恢复和 capability-gated `ask_user` 完成；70 项聚焦测试、766 项后端单测、11 项前端测试/构建、迁移往返和 172 项真实 PostgreSQL 集成通过 | `artifacts/goals/conversation-continuity-i/20260723T184135Z/` | 无；按用户连续执行约定自动进入 CC-J / U10 |
| CC-J (U10) | Verified | 100% | Goal J verifier 通过；Direct/ReAct/Plan 共享语义 Completion Gate、伪终态/矛盾/未决输入阻断、一次无 Tool 纠错、纠错后 AskUser durable wait、严格 envelope、内部候选输出及崩溃复用完成；95 项聚焦测试、787 项后端单测、11 项前端测试/构建、迁移往返和 173 项真实 PostgreSQL 集成通过 | `artifacts/goals/conversation-continuity-j/20260723T190520Z/` | 无；parent RH-H 保持开放，按用户连续执行约定自动进入 CC-K / U11 |
| CC-K (U11) | Verified | 100% | Goal K verifier 通过；同一 AE1–AE11 数据集驱动 hermetic/外部 Native 评测，结构化分母、baseline 对比、redacted case、缺失 usage/timeout/skipped 保留完成；hermetic 11/11、direct 8/8、非必要澄清 0/8、wrong intent 0/11、高风险 Tool effect 0/1；4 项聚焦测试、791 项后端单测、11 项前端测试/构建、迁移往返和 174 项真实 PostgreSQL 集成通过 | `artifacts/goals/conversation-continuity-k/20260723T191815Z/` | 无；外部凭据未提供，11 项明确 skipped/unverified；CC-A–CC-K 配套轨道关闭，parent RH-H 仍开放 |

状态只允许 `Not Started`、`In Progress`、`Blocked`、`Implemented`、`Verified`。Goal G–L 采用 `docs/plans/2026-07-18-001-nico-native-runtime-architecture-migration-plan.md` 的迁移路线；六个阶段的代码、迁移、测试、Compose 验收、文档和 Handoff 已全部交付。Goal G–L 保留 95% 是对未提供外部凭据的诚实标记，不是未完成的仓库实现项。CLI-A–F 采用 `docs/plans/2026-07-19-001-nico-cli-first-class-interface-plan.md`，六个阶段均已按顺序完成实现与验收。领域 Team/Workflow 不属于 Nico core。

RH-A–RH-I2 采用 Runtime Hardening 统一计划。`Implemented` 表示该 U-ID 的代码、迁移
和 focused proof 已完成；只有计划规定的全量 gate 与证据复核完成后才升为
`Verified`。一个 Goal 只处理一个 U-ID。

CC-A–CC-K 采用 Conversation Continuity and Clarification Runtime 配套计划。
每次 Goal 只执行一个 U-ID；CC-A 仅完成真实请求链路审计与字符化测试，没有修改
生产 Runtime 或提前进入 CC-B。本次执行按用户显式覆盖，从 CC-B 开始在同一个
Goal 内自动推进后续 Unit；每个 Unit 仍单独完成验证、证据、摘要和 handoff，并以
这些持久化输出作为下一 Unit 的上下文边界。CC-K 验证后该配套轨道已关闭；这不改变
上表中 parent RH-H 尚未完成的非语义 obligation 范围。
