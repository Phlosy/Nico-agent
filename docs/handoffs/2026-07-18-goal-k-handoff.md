# Goal K Handoff：Memory/Skill Runtime 集成与效果追踪

日期：2026-07-18 UTC  
状态：Implemented（95%）  
下一阶段：Goal L / U11–U12

## 本阶段结论

Goal K 已把现有治理后的 Memory 与 Skill 接入 Nico Native 执行链路。首次 claim 时，Runtime 计算并冻结 Tenant 与 AgentVersion 的策略交集；Child 只能在 Parent、Child AgentVersion 与 delegation restrictions 的共同边界内进一步收缩。恢复时直接复用冻结选择，不读取新的发布状态，因此同一 Run 的执行语义可重放。

只有 active、未过期的 Memory，以及 published 的 Skill stable/canary 解析结果可以进入 ContextSeed。候选 Memory、Draft Skill、禁用或过期内容均不会注入。注入内容标记为 `untrusted_data`，不会扩大 Tool、Secret、Artifact 或协作权限。

ContextSnapshot 固定具体 Memory/Skill ID、版本、content hash、来源和部署解析结果。策略同时限制 top-k、字符数与估算 token 数。公开 RuntimeSession API 只返回策略快照，不返回私有的知识选择内容。

## 数据与 API 合同

- Alembic migration：`20260718_0015_memory_skill_runtime_integration.py`。
- `runtime_sessions` 保存公开的 `knowledge_policy_snapshot` 与内部 `knowledge_selection_snapshot`。
- `context_snapshots` 保存 `memory_refs`、`skill_refs` 与 `effect_metadata`。
- `runtime_knowledge_usages` 保存具体来源版本/Hash、ContextSnapshot、ModelCall、终态 outcome 和 effect metadata，并启用复合租户外键、FORCE RLS 与终态保护。
- `GET /api/v1/runs/{run_id}/knowledge-usages` 返回脱敏后的消费事实，不返回知识正文。
- Growth 轨迹可引用消费 Run 的知识使用事实，但仍只生成 Candidate；验证、独立审批与发布边界不变。

## 实现边界

- Nico Native 独立运行，不依赖 Hermes。
- Team 与业务 Workflow 仍由量化、科研等领域系统负责，不进入 Nico core。
- 字符/token 上限采用离线、确定性的近似预算；真实 Provider 的 tokenizer parity 属于后续 Provider 专项优化。
- 正式 API Key/JWT、SDK 与完整业务 Console 不属于 Goal K。

## 验收结果

- `ruff check backend`：通过。
- 后端单元测试：231 项通过。
- 前端测试与生产构建：7 项通过。
- 真实 PostgreSQL/pgvector、Redis、MinIO 集成测试：75 项通过。
- Alembic 从 Goal K head 完整 downgrade 到 base，再 upgrade 到 head：通过。
- Goal K Compose E2E：已验证治理发布、candidate/draft 排除、冻结引用、Context/ModelCall 关联、效果记录、GrowthSource 关联及公开 API 脱敏。
- 最终证据：`artifacts/goals/goal-k/20260718T195512Z/`。

当前唯一未完成的验证是操作者尚未提供真实外部模型 endpoint、model name 与 credential ref。因此按迁移计划保留 `Implemented 95%`，不得标记 `Verified`；hermetic fake-model 证据不能替代 live-model acceptance。

## Goal L 接续入口

1. 先读取本 Handoff、功能矩阵与迁移计划 U11/U12。
2. U11 处理 Hermes optional adapter、显式启用、v2 compatibility 与 Native/Mock/Hermes capability parity；默认 Compose 不得注册 Hermes。
3. U12 收口 legacy resolver/deprecation、用户文档、只读 Run Inspector 和 Goal G–L 全量验收。
4. 不重写 Goal K 的冻结选择与效果记录合同；Provider Adapter 只能消费相同的 Runtime v2 边界。
