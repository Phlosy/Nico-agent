# Goal F：Memory 与 Skill 实施计划

- 状态：In Progress（F3 pgvector 检索完成）
- 日期：2026-07-17
- 基线：Goal E commit `431ad14`

## 范围

本阶段在 Goal E 的规范化终态 Run、RunStep、ToolCall 与 Runtime trajectory 之上，实现四类 Memory、租户安全作用域、来源链、确定性切片和嵌入、PostgreSQL/pgvector 检索，以及 Skill、不可变 SkillVersion、候选生成、验证、Evaluation、Approval、发布、灰度、禁用和回滚。

Goal F 不实现 Team/Role/Workflow、Plugin 加载、量化策略或交易、正式认证、SDK、SSE 和业务 Web Console。`team` Memory/Skill scope 会冻结为合同值，但在 Goal G 建立可验证的 Team 归属前必须失败关闭；不得用无外键 UUID 或 Prompt 约定伪造团队授权。

## 不变量

1. 自动成长只能产生 `candidate` Memory 或 `draft` SkillVersion，不能直接写入 Active/Published 状态或切换 active pointer。
2. 候选来源必须属于同一租户的终态 Run，并保存 Run、Step、ToolCall、trajectory、AgentVersion、工具版本和生成器版本的来源快照及 Hash。
3. Candidate 不能被正式 Memory 检索或 Skill 解析使用。发布要求同一 subject/content hash 的通过 Evaluation 和终态 Approved Approval。
4. Memory 内容更新创建新版本；Active/Invalidated/Expired/Deleted 行不改写内容。删除是 tombstone，失效和过期均从召回集合移除。
5. SkillVersion 发布后不可变。稳定发布、灰度和回滚只改变 Skill active pointer 或可审计 deployment，不修改历史版本。
6. 检索必须先限定 tenant、状态、未过期和调用者被授权的 scope，再执行向量排序；相似度永远不能扩大作用域。
7. Memory/Skill 服务不能执行工具、直接调用 Hermes、读取其他租户数据或把未脱敏的外部响应重新引入轨迹。
8. Team/Plugin/Role 尚未实现时相关 scope 或权限引用失败关闭，未来阶段只能收窄权限，不能改变 Goal F 历史事实。

## 领域合同

### Memory

- 类型：`working`、`episodic`、`semantic`、`procedural`。
- 作用域：`tenant`、`project`、`agent`、`team`；Goal F 可用前三种，`team` 保留并拒绝。
- 生命周期：`candidate -> active -> invalidated|expired|deleted`；验证失败保留 Candidate 及 Evaluation，不静默删除。
- 版本：同一 `memory_key` 的版本严格递增，通过 `supersedes_id` 建链；同一 key 最多一个 Active 版本。
- 来源：至少一个 terminal Run 和一个属于该 Run 的 terminal Step；可附加 ToolCall 和 trajectory 快照 Hash。
- 向量：Memory 发布后按确定性规则生成 chunk；每个 chunk 保存文本 Hash、位置、embedding provider/version 和固定维度向量。

### Skill

- `Skill` 是稳定身份和 active pointer；`SkillVersion` 冻结适用条件、前置条件、输入 Schema、步骤、精确工具版本、输出 Schema、验证规则、失败模式、来源和 content hash。
- Skill 生命周期：`candidate -> testing -> approved -> published -> deprecated|disabled`；回滚可把有历史 Published 版本的 Skill 恢复为 Published。
- SkillVersion 生命周期：`draft -> testing -> published|rejected`。失败修订创建新 Draft 版本，不把 Rejected 改回 Testing。
- 灰度 deployment 绑定 project/agent（未来可绑定 team）scope、Published SkillVersion 和 1–99 的比例；Run ID 与 deployment ID 的稳定 Hash 决定命中。全量推广切换 active pointer 并退役灰度。

### Evaluation 与 Approval

- Evaluation 保存 evaluator 名称/版本、subject/content hash、score、verdict、details 和 evidence；Completed/Failed 后不可变。
- Approval 保存 subject/content hash、action、requester、reviewer、reason 和期限；`requested -> approved|rejected|cancelled|expired`，终态不可变。
- 发布事务重新校验 subject hash、最新通过 Evaluation、未过期 Approved Approval 和作用域，不信任客户端传入的布尔值。

## 检索与嵌入合同

1. `EmbeddingProvider` 是 provider-neutral 接口，输出维度和版本写入每个 chunk；同一索引不混用维度。
2. 第一版使用无网络、可复现的本地 feature-hashing embedding：规范化 Unicode 后对词元和字符 n-gram 做带符号 Hash，并进行 L2 归一化。它是真实向量检索基线，不冒充外部大模型语义质量；后续 provider 可通过重建索引替换。
3. chunker 按规范化段落和固定字符窗口切片，边界、重叠、最大长度及版本固定；重复输入必须得到相同 chunk hash 和向量。
4. pgvector 使用 cosine distance；查询固定按 `(distance, memory_id, chunk_index)` 排序并去重到 Memory。API 返回 similarity、命中 chunk 和完整来源摘要。
5. 空查询、维度不匹配、未授权 scope、Team scope、Candidate/失效/过期/tombstone 一律不进入召回。

## 候选生成合同

1. 只从数据库权威终态 Run 和规范化持久化轨迹构造 `TrajectorySnapshot`；所有 source ID 必须在同一租户和 Run 内闭合。
2. 反思器只接收脱敏 DTO，不接收 ORM、数据库 Session、Secret 或 Tool Gateway 能力。
3. F4 提供确定性规则反思器作为可重复验收实现，并保留 `ReflectionProvider` 接口；未来模型反思器也只能返回候选 DTO，不能发布。
4. 幂等键由 tenant、source run、generator/version、candidate kind 和内容 Hash 构成；重复反思不得产生重复候选。
5. 单次任务成功、收益字段、相似度或模型自评不能单独构成通过 Evaluation 或 Approval。

## 验收出口

- 单测：四类型/作用域、状态机、版本、content hash、chunk/embed 稳定性、scope 解析、候选幂等、验证、审批、发布、灰度和回滚。
- 真实 PostgreSQL：迁移升级/降级/重放、pgvector 距离排序、索引、RLS、复合外键、跨租户、终态不可变、并发版本/发布和 tombstone 排除。
- API：Memory CRUD/检索/来源/失效/删除，Skill/Version/比较/验证/审批/发布/灰度/禁用/回滚，以及稳定错误和 OpenAPI 敏感字段检查。
- E2E：终态 Run → 候选 → 验证 → 审批 → Memory 召回与 Skill 灰度/发布 → 回滚；第二租户不可观察；未审批候选不可使用。
- `scripts/verify-goal-f.sh` 汇总 Goal E 回归、Goal F 单元/集成/Compose E2E 并归档实际日志和验收摘要。

## 阶段增量

1. F1：冻结合同、ADR、状态、来源链和验收计划；
2. F2：Memory/Skill/Evaluation/Approval 模型、迁移、RLS 和数据库不变量；
3. F3：确定性 chunk/embed、pgvector 检索和 scope 授权；
4. F4：终态轨迹快照、反思接口和候选生成；
5. F5：验证、Evaluation、Approval、Memory 发布/修订/失效/删除；
6. F6：SkillVersion 比较、发布、灰度解析、禁用和回滚；
7. F7：REST API、OpenAPI、单元与真实 PostgreSQL 集成测试；
8. F8：Compose E2E、全量回归、证据、文档审查和 Goal G Handoff。

只有全部出口真实通过，Goal F 才能从 In Progress 更新为 Verified。
