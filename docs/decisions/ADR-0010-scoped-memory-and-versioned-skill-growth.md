# ADR-0010：作用域先行的 Memory 与不可变 Skill 成长

- 状态：Accepted
- 日期：2026-07-17

## 背景

ADR-0005 已决定所有自动成长必须先成为 Candidate，但尚未冻结候选如何引用轨迹、向量召回如何遵守租户与作用域、Skill 灰度如何不破坏历史版本。Goal F 同时面对成长污染、向量越权、不可复现和 Team 尚未实现四类风险。

## 问题

如何让终态 Run 产生可检索 Memory 和可复用 Skill，同时保证来源闭合、租户隔离、显式审批、确定性验证、灰度和回滚，并不提前实现 Goal G 的 Team。

## 候选方案

1. 反思结果直接写入共享向量库和当前 Skill 文本，由相似度及成功率决定使用。
2. Candidate 与正式内容共用召回路径，仅通过 Prompt 提醒 Runtime 忽略未审批项。
3. Candidate 与可用能力在状态和查询层隔离；来源快照与 content hash 绑定 Evaluation/Approval；Memory 检索先做 scope 过滤；Skill 通过不可变版本、active pointer 和独立灰度 deployment 发布。

## 最终选择

选择方案 3。Memory 使用逻辑 key 加不可变版本行，只有 Active 版本进入 pgvector 召回。Skill 使用稳定身份、不可变 SkillVersion 和可审计部署。所有自动结果从 Candidate/Draft 开始，发布时在一个事务内重新验证来源、内容 Hash、Evaluation 和 Approval。

Team scope 作为稳定枚举保留，但在 Goal G 建立 Team 与 Membership 之前失败关闭；不得以未验证 UUID 代替 Team 归属。

## 选择原因

- 数据库查询和状态约束可以执行安全边界，Prompt 不能。
- scope 过滤先于相似度排序，避免高相似内容跨域泄露。
- 来源、评价和审批绑定 content hash，防止“验证 A、发布 B”。
- active pointer 与 deployment 使发布、灰度和回滚不改写历史版本。
- provider-neutral embedding/reflection 接口保留未来接入模型能力，同时第一版可离线、确定性验收。

## 代价

需要更多表、状态转换、复合外键、RLS、并发控制和清理策略。确定性本地 embedding 的语义质量只作为可复现基线，替换 provider 时需要重建索引。Goal F 不能实际启用 Team 共享记忆，必须等待 Goal G 的归属模型。

## 后续影响

Goal F 实现前三种可验证 scope、四类 Memory、pgvector、候选、Evaluation、Approval、SkillVersion 和灰度/回滚。Goal G 接管 Team scope resolver 并补充 Team/Membership 外键授权；Goal H 的 Plugin evaluator 或 Skill 只能复用同一 Candidate/Approval 边界；Runtime 只能读取已发布解析结果。

## 可逆性

高。Embedding provider 可通过版本化重建 chunk 向量替换；反思器可新增实现；灰度可退役；active pointer 可回滚。已发布 Memory/SkillVersion、来源、Evaluation、Approval 和审计记录保持不可变。
