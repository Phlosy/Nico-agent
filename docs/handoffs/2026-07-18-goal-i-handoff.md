# Goal I Handoff：Plan、Reflection 与 Completion Evaluation

## 1. 本阶段目标

在 Goal H 可恢复的 Nico Native Runtime 上完成迁移计划 U6：实现有界 `plan_and_execute`，使复杂任务拥有可持久化、可验证、可重规划、可恢复且可审计的 Plan 执行闭环。

## 2. 实际完成内容

- 严格 JSON Schema Planner，以及步骤数量、重复键、未知依赖和 DAG 环检测；
- 追加式 Plan revision：初始 Plan 与每次 Replan 均为新事实，旧 revision 不覆盖；
- 确定性拓扑执行、PlanStep 状态、独立 ContextSnapshot/ModelCall/RunStep；
- PlanStep 复用平台唯一 Tool Gateway，冻结精确工具版本，执行权限交集、预算、幂等和审计；
- 工具副作用前保存 schema-v3 checkpoint，崩溃恢复使用相同幂等键，工具 RunStep 关联 Plan 父步骤；
- step acceptance 和 Task acceptance 的确定性 Completion Evaluation，支持 non-empty、required fields 与 JSON Schema；
- 受约束 Reflection，只允许 `retry`、`replan` 或 `fail`，并受 reflection/replan 预算约束；
- 可选 Completion 模型 judge，使用独立 ContextSnapshot、ModelCall、usage/cost 与 RuntimeEvaluation；
- schema-v3 紧凑 checkpoint，以及数据库事实领先 checkpoint 时的恢复对账，避免重复执行或重复计费；
- `plans`、`plan_steps`、`runtime_evaluations` 模型、migration 0012、FORCE RLS、复合外键与不可变防线；
- Plan/PlanStep/RuntimeEvaluation 的租户隔离只读 API；
- 单元、真实 PostgreSQL/Tool Gateway 集成、Compose fake-model E2E 与 Goal I 总验收脚本。

## 3. 阶段状态

Goal I 状态为 `Implemented`，完成比例 95%。本地静态门禁、220 项后端单测、7 项前端测试与构建、65 项真实依赖集成、完整 Alembic 往返、基础 Compose 和 Goal I hermetic E2E 均通过。

由于没有操作者提供的真实模型 endpoint、model 和 `env:NICO_MODEL_SECRET_*` credential ref，迁移计划要求的 credentialed real-model planning E2E 未执行，因此不能标记为 `Verified`。该外部验收缺口不阻止 Goal J 开发。

## 4. 运行与恢复语义

```text
Planner -> 保存 Plan revision -> 按 DAG 执行 PlanStep
                                  |
                                  +-> 模型请求工具
                                      -> schema-v3 pre-action checkpoint
                                      -> Tool Gateway / 稳定幂等键
                                      -> observation -> 下一模型轮次
                                  |
                                  +-> 确定性 step validation
                                      -> passed: 下一步
                                      -> failed: Reflection
                                                 -> retry / replan / fail

全部步骤完成 -> 确定性 Task Completion
              -> 可选独立模型 judge
              -> Run completed 或受约束修正 revision
```

Worker 接管时以 PostgreSQL 的 Plan/PlanStep/RuntimeEvaluation/ModelCall/ToolCall 为权威事实。若事件已提交而 checkpoint 尚未来得及更新，恢复对账会推进完成游标；若工具副作用已成功但观察 checkpoint 尚未提交，Tool Gateway 通过稳定幂等键返回缓存结果。

## 5. 数据库与 API 变化

- 新增 `plans`：Run 内递增 revision、状态、目标、supersedes、Planner ModelCall 与内容 Hash；
- 新增 `plan_steps`：步骤定义、依赖、acceptance、attempt、RunStep、输出 Hash 与 evidence refs；
- 新增 `runtime_evaluations`：step validation、Reflection、Completion 的方法、verdict、输入/输出 Hash、证据与可选 ModelCall；
- migration 0012 为三表启用 FORCE RLS、租户/Run 复合外键、状态约束、Plan 内容不可变和 Evaluation 追加式保护；
- 新增 Run 下的 Plan 列表/详情、PlanStep 列表和 RuntimeEvaluation 列表 API；
- migration 已完成 head -> base -> head 全链路重放。

## 6. 测试与证据

最终总验收命令：

```bash
NICO_EVIDENCE_DIR=artifacts/goals/goal-i/20260718T181557Z scripts/verify-goal-i.sh
```

覆盖内容：Ruff/format、220 项后端单测、7 项前端测试与生产构建、65 项真实依赖集成、完整 Alembic 往返、基础 Compose、Goal I 的失败验证/Reflection/Replan/Completion/独立 judge E2E，以及 Secret 扫描和证据清单。

真实集成还证明 PlanStep 通过真实 Tool Gateway 执行 `file.write`，schema-v3 checkpoint 在副作用前落盘，工具 observation 进入下一模型轮次，ToolCall 与 Plan 父 RunStep 可追溯。

最终证据目录：`artifacts/goals/goal-i/20260718T181557Z/`。以目录内最终 `verify.log`、`acceptance-summary.md` 和 `manifest.sha256` 为准。

## 7. 已知限制

- 未执行真实外部模型 Planning；hermetic fake model 只证明协议、治理、恢复与持久化，不证明模型质量、盈利能力或生产 Provider parity；
- Goal I 不包含 Delegation、Child Run、Agent 消息、共享 Artifact、并行聚合、父子预算或树取消；
- Completion Evaluation 是本次 Run 的运行时验收事实，不等同于 Memory/Skill 成长域中的发布 Evaluation；
- 超大工具观察仍按 checkpoint 上限截断；后续可在 Artifact 能力完成后改为引用；
- 正式 API 身份认证和多副本生产部署验收仍未完成。

## 8. 下一阶段禁止重复实现的内容

- 不建立第二套 Planner 模型网关、Tool Gateway、checkpoint 存储或幂等缓存；
- 不允许 Child Agent 或协调层绕过现有 Model/Tool 权限与审计边界；
- 不把进程内 Plan、消息或 session 当作恢复权威，PostgreSQL 事实才是权威；
- 不原地改写旧 Plan revision、已终结 ModelCall、ToolCall 或 RuntimeEvaluation；
- 不把运行时 Completion Evaluation 混入受控成长发布流程；
- 不把量化/科研 Team 组织结构和领域 Workflow 引入 Nico core。

## 9. 下一阶段入口

Goal J 执行迁移计划 U7–U9：实现通用的多 Agent coordination persistence、权限与预算收窄、Child Run 生命周期、父子消息、暂停/唤醒、树形取消、共享 Artifact 和并行结果聚合。开始前读取本 Handoff、迁移计划、`docs/runtime.md`、`docs/state-machines.md`、Goal Status 与 Feature Matrix，并复用现有 Runtime v2、Plan、Model Gateway、Tool Gateway、租约和 checkpoint 边界。
