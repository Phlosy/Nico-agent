# Goal F completion audit

审计基线：任务书 Goal F、`docs/plans/goal-f-memory-skill-plan.md` 的 8 条不变量和 5 类验收出口。结论只使用当前代码、测试与 `verify.log`/E2E 响应作为证据。

| 要求 | 权威证据 | 结论 |
| --- | --- | --- |
| 四类 Memory 与安全 scope | `test_growth_reflection.py`、失败 Run working-memory 集成、`test_growth_persistence.py` scope/RLS 测试；team scope 合同测试失败关闭 | 已证明 |
| 同租户终态来源、轨迹脱敏与 Hash | `test_growth_candidate_generation.py` 覆盖非终态拒绝、Run/Step/Tool/Runtime 来源、递归 Secret 脱敏和并发幂等；迁移复合外键/trigger | 已证明 |
| 确定性切片、embedding 与 pgvector | `test_memory_primitives.py`；真实集成覆盖 `vector(384)`、HNSW cosine、scope-first 排序、来源、失效排除和 chunk 不可变 | 已证明 |
| Candidate 不能直接使用 | E2E `memory-search-before-review.json=[]`、`memory-premature-publish.json`、`skill-premature-resolve.json`；`http-statuses.txt` 记录两个 400 | 已证明 |
| Evaluation 与独立 Approval | 单元 validator 检查；真实集成覆盖异常脱敏、最新评价、审批全终态/过期/重申请/并发；E2E 自审 403、独立 reviewer 批准 | 已证明 |
| Memory 发布、版本、失效、删除与召回 | `test_growth_review_lifecycle.py` 覆盖原子发布/索引、修订/supersede、失效/到期/tombstone、失败回滚；E2E 发布后真实召回含来源 | 已证明 |
| SkillVersion 比较、验证、发布、灰度和回滚 | `test_skill_release_lifecycle.py` 覆盖工具漂移、scope、revision、并发 canary、推广/弃用/禁用/历史回滚及数据库绕过；E2E 完成 v1/v2→canary→promote→rollback | 已证明 |
| 数据库迁移、RLS 与不变量 | `verify.log` 中 Alembic head→base→head 成功；58 项真实 PostgreSQL/pgvector 集成含 FORCE RLS、复合外键、trigger 与双租户 | 已证明 |
| REST API、OpenAPI 与敏感字段 | 172 单测中的 OpenAPI 路径/Header/Schema 检查；`test_growth_api.py` 完整 HTTP；E2E `openapi.json` 和来源响应无 snapshot/vector/arguments/token | 已证明 |
| Compose E2E 与第二租户不可观察 | `e2e-goal-f/` 保存终态 Run、候选、评价、审批、检索、两个版本、deployment、解析及 foreign 404/空列表/空检索响应 | 已证明 |
| 全量回归和可重复验收 | `verify.log`：Ruff、172 单测、7 前端测试+build、58 真实集成、Goal C/D/E/F E2E 全绿；`scripts/verify-goal-f.sh` 可重复执行 | 已证明 |
| 范围纪律 | 没有 Team/Workflow/Plugin/量化交易/正式认证/SDK/SSE/业务 Console 实现；team scope 明确失败关闭，UI 记为不适用 | 已证明 |

## 审计结论

Goal F 计划中的代码、迁移、RLS、服务/API、单元/集成/E2E、证据和接续文档均有直接证据；没有以窄测试代替广泛要求。Goal F 可从 In Progress 更新为 Verified。后续范围在 Handoff 中保持为 Goal G+，不构成 Goal F 缺项。
