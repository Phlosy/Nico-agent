# Memory 与 Skill 边界

Goal F 采用“轨迹事实 → Candidate → 验证 → 审批 → 不可变发布 → 受限使用”的成长路径。本文是运行时、API 和后续 Plugin/Team 接入必须遵守的稳定边界；F5 已将领域记录、pgvector 检索、终态轨迹候选生成、验证/审批和 Memory 生命周期落地，代码状态见 Feature Matrix。

## 能力流

```text
terminal Run + terminal Steps + terminal ToolCalls + normalized trajectory
  -> tenant-bound TrajectorySnapshot
  -> ReflectionProvider (candidate DTO only)
  -> Memory candidate / SkillVersion draft
  -> deterministic validation + immutable Evaluation
  -> immutable Approval decision
  -> Active Memory / Published SkillVersion
  -> scope-filtered retrieval / deterministic canary resolution
  -> invalidate, disable, or rollback pointer
```

反思器没有工具执行、Hermes 会话、数据库 Session 或发布权限。Runtime 没有验证和审批权限。任何层都不能把成功率、收益、相似度或模型自评直接转换为发布决定。

## Memory 分类与作用域

| 类型 | 用途 | 默认期限 |
| --- | --- | --- |
| `working` | 当前工作上下文和短期中间事实 | 必须设置过期时间 |
| `episodic` | 某次任务的经历、结果和教训 | 按租户策略 |
| `semantic` | 经验证的稳定事实和概念 | 可长期保留 |
| `procedural` | 可复用操作知识；复杂可执行过程应升级为 Skill | 按租户策略 |

| Scope | 所有者 | Goal F 行为 |
| --- | --- | --- |
| `tenant` | 当前 Tenant | 当前租户内显式授权调用方可用 |
| `project` | 已存在且同租户的 Project | 仅该 Project 上下文可用 |
| `agent` | 已存在且同租户的 Agent | 仅该 Agent 上下文可用 |
| `team` | Goal G 的 Team | 合同保留，当前创建和检索均失败关闭 |

调用者提交的是业务上下文，不提交任意 scope allowlist。服务从 TenantContext、Project、Agent 及未来 Team 关系计算可用 scopes。

## 来源与完整性

每个 Candidate 保存来源快照：Run、至少一个 terminal RunStep、相关 terminal ToolCall、RuntimeSession/trajectory hash、AgentVersion、精确工具版本、代码/生成器版本和生成时间。数据库复合外键确保 tenant/run/step/tool call 闭合；快照正文只使用已持久化且已脱敏的字段。

Candidate、Evaluation、Approval 和发布对象都保存 content hash。发布事务以数据库内容重新计算 Hash，并要求通过 Evaluation 与 Approved Approval 的 subject、action 和 Hash 全部一致。

## 检索

检索只选择当前租户、Active、未过期、非 tombstone 且位于计算所得 scope 内的 Memory chunk，然后使用 pgvector cosine distance 排序。候选、失效、过期和已删除版本在 SQL 层排除。结果附 similarity、命中片段、Memory 版本和来源摘要，便于 Runtime 引用和审计。

第一版 embedding 是版本化、确定性的本地 feature hashing，确保离线可运行和测试可复现。它不是外部语义模型质量的替代声明；替换 provider 必须生成新索引版本，不能把不同维度或模型的向量混排。

当前实现使用 `nico-paragraph-window@1.0.0` 对 NFKC 规范化内容进行段落优先、固定窗口和重叠切片；`nico-feature-hashing@1.0.0` 将词元及 3–5 字符 n-gram 映射为 L2 归一化的 384 维向量。MemoryChunk 同时冻结 Memory/Chunk Hash、offset、chunker 和 embedding profile，使用 pgvector HNSW `vector_cosine_ops`。同一 profile 重复索引返回既有结果，任何 Hash/切片漂移都失败关闭。

`MemoryRetriever` 不接受 scope allowlist，只接受 TenantContext 与可选 Project/Agent 上下文；服务先在 RLS 事务中确认上下文存在，再以 SQL 过滤 Active、未过期和授权 scope，按每条 Memory 的最佳 chunk cosine distance、Memory ID 稳定排序，并附带 GrowthSource 摘要。Team、跨租户 context、Candidate、Invalidated、Expired 和 Deleted 均不能进入结果。

## Skill 发布与解析

SkillVersion 不是一段自由文本，而是带 JSON Schema、结构化步骤、精确工具引用、验证规则和失败模式的不可变执行知识。验证检查 Schema、步骤图、工具存在性/状态、来源闭合、权限不扩张和测试用例。发布只允许已通过验证且获批的版本。

稳定版本由 Skill `current_version_id` 指向。1–99% 灰度通过独立 deployment 覆盖特定 project/agent scope，以 Run ID 的稳定 Hash 选择版本。同一优先级的重叠 deployment 被拒绝。推广到 100% 时切换稳定指针；回滚切回历史 Published 版本或退役灰度，不更改任何 SkillVersion。

## 当前阶段边界

- F1：本文、ADR-0010 与实施计划已冻结。
- F2：Memory、Skill、SkillVersion、GrowthSource、Evaluation、Approval、SkillDeployment 已落库；运行角色对全部新表启用 `FORCE RLS`。复合外键闭合同租户来源，触发器拒绝非终态来源、直接发布、Hash 错配、非法状态转换、正式内容改写和物理删除。
- F3：确定性 chunk/embed、版本化 MemoryChunk、`vector(384)`、HNSW cosine 索引、幂等索引服务和 scope-first 检索已完成；本地 embedding 是可复现基线，不声称外部语义模型质量。
- F4：只读构造有界、递归脱敏、Hash 稳定的终态 TrajectorySnapshot；`ReflectionProvider` 只接收冻结 DTO。确定性基线反思器按成功/失败结果生成四类 MemoryCandidate 与结构化 SkillVersion draft，策略服务控制 scope/TTL，并在 Run 行锁下幂等写入 GrowthSource、Event 和 Audit。反思器没有 ORM、Session、Secret、工具或发布能力，重复/并发生成不会复制候选。
- F5：`GrowthValidator` 只接收冻结的来源/subject DTO；确定性基线验证内容 Hash、Schema、步骤、精确工具状态/来源、scope 与终态轨迹。Evaluation 按 evaluator/version/content 幂等且终态不可变；Approval 要求最新终态 Evaluation 为 pass，禁止请求者自审，并支持批准、拒绝、取消、过期和重新申请。Memory 发布在一个事务内重新校验 Hash/来源/评价/批准、失效旧 Active 版本、激活新版本并写入确定性向量；修订创建同 key 的新 Candidate，失败不影响旧 Active。显式失效、到期与 tombstone 均保留来源、chunk、Event 和 Audit，但 SQL 召回立即排除。
- F6 起实现 SkillVersion 发布、灰度解析、禁用与回滚；F7 才开放 Goal F REST API。
- Goal G 才能启用 Team scope；Goal H 才能由 Plugin 注册反思器/evaluator；Goal K 才提供正式身份和细粒度审批授权。
