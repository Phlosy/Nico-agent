# Goal F Handoff：受控 Memory 与不可变 Skill 成长

## 1. 本阶段目标

在 Goal E 的终态 Run/RunStep/ToolCall/Runtime 轨迹之上建立受控成长闭环：只生成 Candidate/Draft，保留同租户来源；通过确定性验证与独立人工审批后发布四类 Memory 或不可变 SkillVersion；Memory 使用 scope-first pgvector 检索，Skill 支持灰度、推广、禁用与历史回滚。

## 2. 实际完成内容

- 四类 Memory（working/episodic/semantic/procedural）和 tenant/project/agent 三类可验证 scope；team 合同保留但在 Goal G 前失败关闭；
- Memory/Skill/SkillVersion/GrowthSource/Evaluation/Approval/MemoryChunk/SkillDeployment 持久化、复合租户外键、FORCE RLS、唯一索引和数据库 trigger；
- NFKC 文本规范化、确定性段落窗口切片、384 维 feature-hashing embedding、pgvector HNSW cosine 索引与 scope-first 召回；
- 只读、有界、递归脱敏、Hash 稳定的终态 TrajectorySnapshot，以及无工具/Session/发布能力的 ReflectionProvider；
- 成功 Run 生成 episodic/semantic/procedural 与 Skill Draft，失败/中断 Run 生成 episodic/working；并发与重复生成幂等；
- provider-neutral GrowthValidator、不可变 Evaluation、禁止申请者自审的 Approval，以及批准/拒绝/取消/过期/重新申请；
- Memory 原子发布+索引、不可变修订链、supersede、显式失效、到期和 tombstone；
- SkillVersion 八区差异、精确工具引用、双闸门发布、稳定指针、project/agent canary、真实 Run 稳定分桶解析、推广、退役、弃用、禁用和历史回滚；
- 完整成长域 REST API/OpenAPI，来源响应不公开 snapshot、向量、原始工具参数、Provider 内部状态或凭据；
- 单元、真实 PostgreSQL/pgvector 集成、完整 HTTP 集成、Compose E2E、一键总验收及独立证据目录。

阶段提交：

- `d5ad4c5`：冻结 Memory/Skill 成长合同、ADR 与计划；
- `bfeb8e3`：成长持久化、迁移、RLS 与数据库不变量；
- `90345ba`：确定性 chunk/embed 与 scope-first pgvector 检索；
- `40442fe`：终态轨迹快照与幂等候选生成；
- `b6560e0`：验证、独立审批与 Memory 生命周期；
- `e8acbcd`：SkillVersion 发布、灰度、推广、禁用与回滚；
- `355a2f8`：成长域 REST API、OpenAPI 与 HTTP 集成；
- `92ddf48`：Goal F Compose E2E 与总验收脚本。

## 3. 未完成内容

Goal F 范围内无未完成项。以下内容按任务书留给后续 Goal：Team/Role/Membership/Workflow（G）、Plugin loader 与扩展注册（H）、量化策略及交易工具（I）、业务 Web Console（J）、正式身份/细粒度授权/SSE/SDK/最终安全治理（K）。

本地 feature-hashing embedding 是离线、可复现的检索基线，不代表外部语义模型质量；没有真实模型凭据，因此不声称 Hermes 自主反思质量、策略盈利或在线学习效果。

## 4. 新增文件

- `backend/migrations/versions/20260717_0006_memory_skill_growth.py`；
- `backend/migrations/versions/20260717_0007_memory_vector_index.py`；
- `backend/migrations/versions/20260717_0008_growth_review_idempotency.py`；
- `backend/migrations/versions/20260717_0009_skill_release_guards.py`；
- `backend/src/nico_agent/growth/` 下合同、快照、反思、候选、验证、审批和读服务；
- `backend/src/nico_agent/memory/` 下合同、规范化/切片、embedding、检索和生命周期；
- `backend/src/nico_agent/skills/` 下合同、比较、发布、部署与解析服务；
- `backend/src/nico_agent/growth_api.py`、`growth_api_schemas.py`；
- Goal F 单元与真实依赖集成测试；
- ADR-0010、Goal F 计划、本 Handoff；
- `scripts/e2e-goal-f.sh`、`scripts/verify-goal-f.sh`；
- `artifacts/goals/goal-f/20260717T084217Z/` 验收证据。

## 5. 修改文件

主要修改 `domain/models.py`、`domain/states.py`、`database.py`、`api.py`、`README.md`，以及 architecture、domain model、state machines、API、testing、Memory/Skill 边界、Goal Status 和 Feature Matrix。Goal E 的 Runtime/Tool Gateway 未被绕过或重复实现。

## 6. 数据库变更

- 新增 `memories`、`memory_chunks`、`skills`、`skill_versions`、`growth_sources`、`evaluations`、`approvals`、`skill_deployments`；
- Memory 使用逻辑 key+递增版本；只有 Active、未过期行可索引/召回，chunk 保存文本位置、content hash、chunker 与 embedding profile 及 `vector(384)`；
- Skill 使用稳定 identity、不可变 SkillVersion、`current_version_id` 和独立 deployment；
- GrowthSource 以同租户复合外键闭合 Run/Step/ToolCall/Runtime/AgentVersion；
- 运行角色对全部成长表启用 `FORCE ROW LEVEL SECURITY`；
- trigger 拒绝非终态来源、直接发布、Hash/tenant/scope 错配、正式内容改写、终态记录更新/删除、自审发布、带 active canary 的非法停用/指针切换；
- 迁移已完成 head→base→head 全量重放，58 项真实依赖测试通过。

## 7. API 变更

- `POST /api/v1/runs/{run_id}/growth-candidates`：从终态 Run 幂等生成 Candidate；
- `/api/v1/memories`：列表、读取、向量检索、来源、评价、审批、发布、修订、失效、到期和 tombstone；
- `/api/v1/growth-approvals/{id}`：读取、独立决定和取消；
- `/api/v1/skills`：身份/版本读取、来源、评价、审批、修订、发布、比较、deployment、真实 Run 解析、推广、回滚、弃用和禁用；
- 所有租户资源使用 TenantContext+RLS；跨租户与不存在统一 404，自审 403，revision/非法转换 409，Schema 错误 422；
- 不提供任意创建正式 Memory/Skill 或绕过 Evaluation/Approval 的入口。

## 8. 配置变更

Goal F 没有新增必须的环境变量或外部服务。继续复用 PostgreSQL/pgvector、API/Worker、Redis、MinIO 和 Goal E 的 Runtime/Tool 边界。确定性 embedding profile 当前固定为 `nico-feature-hashing@1.0.0`、384 维；未来替换 provider 必须版本化重建索引。

## 9. 测试命令

```bash
scripts/test.sh
scripts/test-integration.sh
scripts/e2e-goal-f.sh
NICO_EVIDENCE_DIR=artifacts/goals/goal-f/20260717T084217Z scripts/verify-goal-f.sh
```

## 10. 测试结果

- Ruff 与格式检查：passed；
- 后端单元：172 passed；
- 前端：7 passed，TypeScript/Vite production build passed；
- 真实依赖集成：58 passed；
- Alembic head→base→head：passed；
- Goal C 控制面/RLS E2E：passed；
- Goal D Runtime/Worker E2E：passed；
- Goal E Tool Gateway/Sandbox E2E：passed；
- Goal F Run→Candidate→Evaluation→Approval→Memory 召回→Skill v1/v2→canary→推广→回滚 E2E：passed；
- 未审批发布/解析为 400，自审为 403，第二租户读/生成均为 404，跨租户列表/召回为空；
- 权威证据：`artifacts/goals/goal-f/20260717T084217Z/`。

## 11. 已知问题

- Team scope 故意失败关闭，必须等待 Goal G 的 Team/Membership 外键和授权 resolver；
- 当前 Development Tenant Header 不是认证，生产身份和细粒度审批权限属于 Goal K；
- 确定性本地 embedding 只保证可复现与真实向量查询，不保证高级语义质量；
- ReflectionProvider/Validator 已可替换，但当前只有确定性基线，没有外部模型质量评测或自动收益反馈；
- 单次成功、收益字段、相似度和模型自评永远不能单独触发发布；
- Goal F 不包含业务 Web UI、SDK、SSE、Plugin 或量化交易能力。

## 12. 当前架构

```text
terminal Run/Steps/ToolCalls/trajectory
                |
        bounded redacted snapshot
                |
     ReflectionProvider (DTO only)
                |
      Candidate Memory / Skill Draft
                |
 deterministic Evaluation -> independent Approval
                |
       +--------+---------+
       |                  |
 Active Memory       Published SkillVersion
 pgvector chunks     stable pointer + canary deployment
       |                  |
 scope-first search   Run-derived stable resolution
```

PostgreSQL/RLS 是权威边界；Reflection/Validation 不持有 ORM、Session、Secret、Hermes 或 Tool 能力；API 只编排应用服务；Runtime 只能消费已发布结果。

## 13. 下一阶段入口

Goal G 开始前读取本 Handoff、`docs/memory-and-skill.md`、`docs/domain-model.md`、`docs/state-machines.md`、ADR-0001/0005/0006/0010、Goal Status 和 Feature Matrix。Goal G 应建立 Team、Role、Membership 与显式 Workflow，再通过可验证 Membership resolver 启用 team Memory/Skill scope。

## 14. 下一阶段禁止重复实现的内容

- 不重建 Memory/Skill/GrowthSource/Evaluation/Approval/SkillDeployment 表或成长状态机；
- 不复制另一套向量库、候选发布、人工审批或 Skill 灰度机制；
- Team scope 必须扩展现有 scope resolver 和复合授权，不能用 Prompt、任意 UUID 或客户端 allowlist；
- Team/Workflow 不得直接执行工具、调用 Hermes、改写正式 Memory/SkillVersion 或绕过 Candidate 双闸门；
- 不提前实现 Plugin、量化业务、正式认证、SDK/SSE 或业务 Web Console。

## 15. 建议下一步任务

1. 冻结 Team/Role/Membership/Workflow 合同、状态、RLS 和 Goal G 验收计划；
2. 实现 Team 与 Membership 复合租户外键及授权 resolver，并用它扩展现有 Memory/Skill team scope；
3. 实现显式委派、执行、审核、退回、汇总状态机，所有转换写 Event/Audit；
4. 让多 Agent Workflow 只创建/关联 Task/Run，不直接调用 Runtime/Tool 或发布成长候选；
5. 建立双团队/双租户、非法委派、审核退回、恢复与 Compose E2E，再生成 Goal G Handoff。
