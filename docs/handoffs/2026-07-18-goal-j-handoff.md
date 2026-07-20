# Goal J Handoff：动态多 Agent Runtime 与私有 Artifact

## 1. 本阶段目标

在 Goal I 的 Nico Native Plan/ReAct 基线上完成迁移计划 U7–U9：交付通用、动态、可恢复、可审计的 Parent/Child Agent 协作闭环，同时不在 Nico Core 固化任何量化、科研或其他业务 Team/Workflow。

## 2. 实际完成内容

- `Delegation`、`AgentRunRelation`、`AgentMessage` 与 `RunBudgetLedger` 租户级持久化事实；
- Parent 通过原生 `delegate_agent` 动态选择已发布 Child AgentVersion，支持一轮多个并行 Child；
- 最大深度、子节点数、并行度、重复任务、循环委托、Token、成本、工具调用和执行时间约束；
- Tenant、Parent 快照、Child AgentVersion 与单次 Delegation 限制的权限交集，只传递允许的 Secret 引用；
- Child Task/Run、闭包关系、任务消息和预算预留在同一事务中创建；
- Parent 等待 Child 时进入 `waiting_for_subagent` 并释放 Worker lease；全部 Child 终结后事务性核销预算、发送结果消息并唤醒 Parent；
- reconciliation 修复丢失通知，PostgreSQL 保持权威，Redis 只承担非权威通知；
- 幂等 retry request、整树取消、迟到 Child 结果保护和预算释放；
- PostgreSQL Artifact 元数据、私有 MinIO 内容字节、临时上传到 SHA-256 内容寻址对象的完成协议；
- 原生 `store_artifact`、Run 级 Artifact API、Child 到 direct Parent 的显式共享链接、Sibling 拒绝、Hash/size 完整性验证和终态不可改写；
- Artifact 并发幂等上传、大小限制、安全文件名、匿名访问拒绝和临时孤儿检查；
- fail-fast、best-effort 和有独立 Token 上限的 model-judge 聚合策略；默认原生路径把结构化 Child result 与 Artifact refs 回填 Parent，由 Parent 的后续 ModelCall 聚合；
- migrations 0013/0014、FORCE RLS、同租户复合外键、API、Compose fake model、故障注入 E2E 和总验收脚本。

## 3. 阶段状态

Goal J 状态为 `Implemented`，完成比例 95%。最终验收通过 228 项后端单测、7 项前端测试与生产构建、74 项真实 PostgreSQL/Redis/MinIO 集成测试、完整 Alembic base 往返、基础 Compose E2E，以及 Goal J 专用多 Agent 故障恢复 E2E。

没有操作者提供的真实模型 endpoint、model 和 `env:NICO_MODEL_SECRET_*` credential ref，因此迁移计划要求的 credentialed real-model multi-Agent E2E 未执行，不能标记为 `Verified`。该外部验收缺口不阻止 Goal K 开发。

## 4. 运行与恢复语义

```text
Parent ModelCall
  -> delegate_agent × N
  -> 事务性创建 Delegation / Child Task / Child Run / Relation / Message / Budget
  -> Parent 保存 checkpoint，进入 waiting_for_subagent，释放 lease

不同 Worker 领取 Child Run
  -> Child 原生 Agent Loop
  -> 可选 store_artifact -> private MinIO + PostgreSQL metadata + Parent link
  -> Child terminal
  -> 核销父子预算 + result message + Artifact refs
  -> 最后一个 Child 终结时唤醒 Parent

Parent 被重新领取
  -> 从 checkpoint 和 PostgreSQL result messages 恢复
  -> Child results 以结构化 tool observations 进入上下文
  -> Parent ModelCall 验证、处理冲突并生成最终结果
```

Worker 被强杀时，已提交的 Delegation、消息、checkpoint、Artifact 和 ModelCall 不依赖进程内状态。新 Worker 等待旧 lease 过期后接管 Child，完成后仍能唤醒 Parent；reconciler 可修复通知丢失造成的等待状态。

## 5. 数据库与 API 变化

- migration 0013 新增 `delegations`、`agent_run_relations`、`agent_messages`、`run_budget_ledgers`，并扩展 Run/Runtime waiting 状态和协调 reconciliation；
- migration 0014 新增 `artifacts`、`shared_artifact_links`，保存内容 Hash、大小、状态和受控共享关系，MinIO 对象键不进入公开 API；
- 新增 Delegation、Child tree、Message、retry request 与 tree-cancel API；
- 新增 Run Artifact 列表、上传和受控下载 API；
- 新增带 revision 的 Tenant settings 更新 API，用于配置动态 coordination policy；
- 所有新表启用 FORCE RLS，并完成 terminal immutability、跨租户复合外键和 migration base 往返验收。

## 6. 测试与证据

最终总验收命令：

```bash
NICO_EVIDENCE_DIR=artifacts/goals/goal-j/20260718T192139Z scripts/verify-goal-j.sh
```

Goal J E2E 创建一个 Parent 和两个动态 Child。两个 Child 模型调用同时在途时，脚本向 Worker 发送 `SIGKILL`，再启动替代 Worker；旧 lease 过期后 Child 从 checkpoint 恢复，各自产生并共享私有 Artifact，Parent 被唤醒并汇总最终答案。验收同时确认消息、预算、事件、审计、模型调用、匿名 MinIO 拒绝、Sibling 越权拒绝、无凭据/API 对象键泄露和无临时孤儿。

最终证据目录：`artifacts/goals/goal-j/20260718T192139Z/`。目录内 `manifest.sha256` 已逐项校验，`verify.log` 和 `acceptance-summary.md` 是最终验收记录。

## 7. 已知限制

- 未执行真实外部模型的多 Agent 协作；fake model 证明协议、并发、治理、恢复和持久化，不证明模型协作质量、业务收益或盈利能力；
- 首版 Artifact 共享只允许 Child 到 direct Parent，不提供任意 Run 分享、预签名 URL、版本工作台或跨 Project 发布；
- model-judge 提供受预算约束的聚合合同；原生默认闭环由 Parent 的后续 ModelCall 基于结构化结果完成聚合，尚无独立的可视化聚合策略配置页；
- retry request 当前是持久化、可审计的协调命令，不自动克隆出新的 Child attempt；
- 正式 API 身份认证、生产多副本压测和真实 Provider parity 尚未完成。

## 8. 下一阶段禁止重复实现的内容

- 不建立固定 Team、Role、Membership 或业务 Workflow；这些属于量化、科研等上层领域系统；
- 不建立第二套模型调用、工具执行、预算、消息、Artifact 或 checkpoint 事实；
- 不允许 Child 绕过现有 Model Gateway、Tool Gateway、Tenant/RLS、Secret ref 和审计边界；
- 不把 Redis、进程内队列或 MinIO 元数据当成协调权威，PostgreSQL 仍是事实源；
- 不向公开 API 暴露 lease token、Secret 值、MinIO 凭据或对象键；
- 不在 Goal K 自动发布 Memory/Skill，成长候选仍须沿用 Evaluation、Approval 和发布治理。

## 9. 下一阶段入口

Goal K 执行迁移计划 U10：把现有已发布 Memory/Skill 接入 Native Runtime 的执行前准备和 ContextSnapshot，冻结具体版本、Hash、来源与效果记录；严格排除 candidate、draft、expired 和 disabled 内容，并让 Child 的召回范围继续服从父子权限收缩。开始前读取本 Handoff、Feature Matrix、迁移计划、`docs/memory-and-skill.md`、`docs/runtime.md` 与 Goal Status。
