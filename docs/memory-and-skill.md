# Memory 与 Skill 边界

Nico 采用“轨迹事实 → Candidate → 验证 → 审批 → 不可变发布 → 受限使用”的成长路径。Memory 与 Skill 生命周期已经通过受控 REST API、真实 PostgreSQL/pgvector 测试和 Compose 端到端流程验证。

## 能力流

```text
terminal Run + terminal Steps + terminal ToolCalls + normalized trajectory
  -> tenant-bound TrajectorySnapshot
  -> ReflectionProvider (candidate DTO only)
  -> Memory candidate / SkillVersion draft
  -> deterministic validation + immutable Evaluation
  -> immutable Approval decision
  -> Active Memory / Published SkillVersion
  -> per-Run policy intersection and frozen ContextSeed
  -> ContextSnapshot / ModelCall consumption and effect facts
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

| Scope | 所有者 | 当前行为 |
| --- | --- | --- |
| `tenant` | 当前 Tenant | 当前租户内显式授权调用方可用 |
| `project` | 已存在且同租户的 Project | 仅该 Project 上下文可用 |
| `agent` | 已存在且同租户的 Agent | 仅该 Agent 上下文可用 |
| `team` | 未实现 | 合同保留，当前创建和检索均失败关闭；团队结构属于领域系统 |

调用者提交的是业务上下文，不提交任意 scope allowlist。服务从 TenantContext、Project、Agent 及未来 Team 关系计算可用 scopes。

## 来源与完整性

每个 Candidate 保存来源快照：Run、至少一个 terminal RunStep、相关 terminal ToolCall、RuntimeSession/trajectory hash、AgentVersion、精确工具版本、代码/生成器版本和生成时间。数据库复合外键确保 tenant/run/step/tool call 闭合；快照正文只使用已持久化且已脱敏的字段。

Candidate、Evaluation、Approval 和发布对象都保存 content hash。发布事务以数据库内容重新计算 Hash，并要求通过 Evaluation 与 Approved Approval 的 subject、action 和 Hash 全部一致。

## 检索

检索只选择当前租户、Active、未过期、非 tombstone 且位于计算所得 scope 内的 Memory chunk，然后使用 pgvector cosine distance 排序。候选、失效、过期和已删除版本在 SQL 层排除。结果附 similarity、命中片段、Memory 版本和来源摘要，便于 Runtime 引用和审计。

第一版 embedding 是版本化、确定性的本地 feature hashing，确保离线可运行和测试可复现。它不是外部语义模型质量的替代声明；替换 provider 必须生成新索引版本，不能把不同维度或模型的向量混排。

当前实现使用 `nico-paragraph-window@1.0.0` 对 NFKC 规范化内容进行段落优先、固定窗口和重叠切片；`nico-feature-hashing@1.0.0` 将词元及 3–5 字符 n-gram 映射为 L2 归一化的 384 维向量。MemoryChunk 同时冻结 Memory/Chunk Hash、offset、chunker 和 embedding profile，使用 pgvector HNSW `vector_cosine_ops`。同一 profile 重复索引返回既有结果，任何 Hash/切片漂移都失败关闭。

公开检索接口只接受 TenantContext 与可选 Project/Agent 上下文；运行时内部还会应用已经冻结的 scope/type allowlist。服务先在 RLS 事务中确认上下文存在，再以 SQL 过滤 Active、未过期和授权 scope，按每条 Memory 的最佳 chunk cosine distance、Memory ID 稳定排序，并附带 GrowthSource 摘要。Team、跨租户 context、Candidate、Invalidated、Expired 和 Deleted 均不能进入结果。

## Skill 发布与解析

SkillVersion 不是一段自由文本，而是带 JSON Schema、结构化步骤、精确工具引用、验证规则和失败模式的不可变执行知识。验证检查 Schema、步骤图、工具存在性/状态、来源闭合、权限不扩张和测试用例。发布只允许已通过验证且获批的版本。

稳定版本由 Skill `current_version_id` 指向。1–99% 灰度通过独立 deployment 覆盖特定 project/agent scope，以 Run ID 的稳定 Hash 选择版本。同一优先级的重叠 deployment 被拒绝。推广到 100% 时切换稳定指针；回滚切回历史 Published 版本或退役灰度，不更改任何 SkillVersion。

## 运行时召回、冻结与效果记录

Worker 第一次领取 Run 时计算 `Tenant policy ∩ AgentVersion policy`。Memory 和 Skill 分别具有显式 `enabled`、scope、top-k、`max_tokens` 与 `max_chars` 双重上限；Memory 还限制类型和最低相似度，Skill 必须列入 `allowed_skill_ids`。token 上限使用与 ContextSnapshot 一致的确定性保守估算，最终取 token/字符两者中更严格的预算。任一侧未启用、配置非法或交集为空时都失败关闭。Child Run 再与 Parent 已冻结策略及 delegation restrictions 求交，只能缩小权限。

召回与 Skill stable/canary 解析和 RuntimeSession 建立在同一个租户事务中完成。`knowledge_selection_snapshot` 冻结精确 Memory/Skill ID、版本、content hash、scope、来源 Hash、解析分支和受上限约束的内容。恢复只重建该快照，不重新查询实时知识；发布、失效、灰度切换或回滚只影响之后首次领取的 Run。该含内容快照不通过普通 Runtime API 返回。

进入模型上下文时，已发布知识位于 `untrusted_context`，明确标记为 `published_memory` 或 `published_skill`，不能授予工具、Secret、网络或委托权限。`ContextSnapshot` 公开可审计的 `memory_refs`、`skill_refs` 和策略/查询 Hash，不把知识当作系统指令。

每个选择写入一条 `RuntimeKnowledgeUsage`。Context 创建和 ModelCall 开始分别绑定首次引用并递增计数；Run 终结后记录 outcome、result hash、usage 和是否真正被模型调用消费。成长快照只携带这些脱敏事实，因此后续 Candidate 可以追溯“哪个版本在什么 Run 中被使用并得到什么结果”，但仍必须重新经过 Evaluation 和独立 Approval，不能自行强化或发布。

## 当前实现

- Memory、Skill、SkillVersion、GrowthSource、Evaluation、Approval 和 SkillDeployment 已持久化，并受 `FORCE RLS`、复合约束和数据库触发器保护。
- Memory 使用确定性切片、本地 384 维 feature-hashing embedding、pgvector HNSW cosine 索引和 scope-first 检索。本地 embedding 是可复现基线，不代表外部语义模型质量。
- `ReflectionProvider` 只接收冻结、脱敏的终态轨迹 DTO，只能生成 Candidate/Draft，没有工具、数据库 Session 或发布权限。
- `GrowthValidator` 检查内容 Hash、Schema、步骤、精确工具状态、来源、scope 与终态轨迹。Evaluation 和 Approval 均不可变，且请求者不能自审。
- Memory 发布在一个事务中重新校验来源、Evaluation、Approval 和 Hash；失效、到期与 tombstone 会立即从 SQL 召回中排除。
- Skill 修订保持不可变来源，支持结构化比较、验证、审批、稳定版本、project/agent canary、推广、弃用、禁用和历史回滚。
- Runtime 首次领取时冻结 Tenant ∩ AgentVersion 的 Memory/Skill 选择；恢复复用相同版本与 Hash，Child 只能进一步收缩。
- `RuntimeKnowledgeUsage` 把选择、ContextSnapshot、ModelCall、终态结果与后续成长轨迹关联起来，不公开所选内容正文。
- API 不提供任意创建正式 Memory 或 Skill 的入口。来源响应不公开内部轨迹快照、向量、原始工具参数或凭据。

当前没有正式身份认证，因此 Approval 的操作者身份只适用于本地/受信环境。Plugin evaluator 注册和 Team scope 不受支持。
