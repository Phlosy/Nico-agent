# Goal H Handoff：Nico Native ReAct、Checkpoint 与故障恢复

## 1. 本阶段目标

在 Goal G 独立 Native Direct Runtime 基础上，实现 U4/U5：让 `nico_native` 能通过现有 Tool Gateway 完成有界 ReAct 多轮循环，并在 Worker 崩溃、租约过期和模型流中断后从已提交边界恢复，不重复已经成功的工具副作用。

## 2. 实际完成内容

- Native ReAct loop：模型推理、精确版本 Tool Call、观察回填和下一轮推理；
- `RuntimeServices.tool_handler`、Tool spec/intent/outcome 合同和唯一 Tool Gateway 执行边界；
- max iterations、max tool calls、token budget、非法/无权限 Tool Call 和空输出的稳定失败；
- schema v2、执行清单绑定、完整性 Hash 校验的 pre-action/post-observation checkpoint；
- 稳定 Tool idempotency key、成功结果缓存复用和参数 Hash 冲突拒绝；
- 过期租约接管、未终结 ModelCall `interrupted`、新的 replay call key 与 replay relation；
- 每次 Native 执行尝试独立 session id，防止旧 lease 的迟到 cancel/release 误伤接管者；
- RunStep 的 step key/type/iteration/parent/context/model refs，以及 ModelCall/ContextSnapshot/ToolCall/Event/Audit 的匹配投影；
- 模型 Tool Call 协议解析失败时终结 ModelCall，不遗留 `streaming` 记录；
- migration 0011、单元/真实 PostgreSQL 集成、Compose Worker SIGKILL 故障注入和 Goal H 总验收脚本。

## 3. 阶段状态

Goal H 状态为 `Implemented`，完成比例 95%。Hermetic fake-model、完整单元/集成/Compose 回归和真实 Worker 故障注入均通过；由于没有操作者提供的真实模型 endpoint、model 和 `env:NICO_MODEL_SECRET_*` credential ref，计划规定的 credentialed real-model Tool E2E 未执行，因此不能标记为 `Verified`。

Goal G 的同一外部凭据缺口仍然存在，但不阻止 Goal I 基于已完成的 U5 继续开发。

## 4. 关键恢复语义

```text
模型产生 Tool Calls
        |
保存 waiting_for_tool checkpoint
        |
Tool Gateway 同事务保存 checkpoint + ToolCall/RunStep
        |
执行外部作用并提交权威 ToolCall 终态
        |
保存 observing checkpoint -> 下一轮模型

若任一 Worker 在中间退出：
- 新 Worker 取得过期 lease；
- streaming ModelCall 标记 interrupted，并以 replay relation 重试；
- 成功 ToolCall 按稳定 idempotency key 返回缓存结果；
- 旧 lease 的事件、取消和终态提交被 fencing 或 attempt session 隔离。
```

## 5. 数据库与 API 变化

- `runs`、`runtime_sessions` 增加 checkpoint schema/revision/hash；
- `model_calls` 增加同 tenant/run 的 replay self-reference；
- `run_steps` 增加 step key/type/iteration、parent step、ContextSnapshot 和 ModelCall 引用；
- 新约束保证 step type、正 iteration、step key 唯一以及所有复合外键不跨 tenant/run；
- Run、RuntimeSession、RunStep 和 ModelCall 查询响应公开上述非敏感审计 metadata；
- migration 已完成全链路 head -> base -> head 重放。

## 6. 测试与证据

最终总验收命令：

```bash
NICO_EVIDENCE_DIR=artifacts/goals/goal-h/<UTC> scripts/verify-goal-h.sh
```

覆盖内容：Ruff/format、205 后端单测、7 前端测试与构建、63 项真实依赖集成、完整 Alembic 往返、基础 Compose、Goal G Direct 回归，以及 Goal H 的 Worker SIGKILL/租约接管/零重复 file.write/Python sandbox/连续事件/Secret 扫描。

最终证据目录：`artifacts/goals/goal-h/20260718T175454Z/`。目录内 `manifest.sha256` 已通过 `sha256sum -c` 完整校验。

## 7. 已知限制

- 真实外部模型 ReAct 尚未执行；fake model 只证明协议、治理、恢复和持久化，不证明模型质量、盈利能力或广泛 Provider 兼容性；
- `plan_and_execute`、Plan revision、Reflection 和 Completion Evaluation 属于 Goal I；
- Delegation、Child Run、消息、Artifact 和多 Agent 协调属于 Goal J；
- checkpoint 内观察内容有单项截断和全局迭代/工具预算，但 Artifact 化的大内容引用要在后续 Artifact 能力完成后进一步收口；
- 正式 API 身份认证和多副本生产部署验收仍未完成。

## 8. 下一阶段禁止重复实现的内容

- 不建立第二套 Tool executor、idempotency cache、模型 Gateway 或 checkpoint 存储；
- 不让 Planner、Reflection 或未来 Child Agent 绕过 Tool Gateway；
- 不复用 Native 进程内 session 作为恢复事实，PostgreSQL checkpoint/ModelCall/ToolCall 才是权威；
- 不用模型 iteration 直接充当全局 RunStep sequence；
- 不覆盖 interrupted ModelCall、历史 checkpoint、ToolCall 或旧 Plan 事实；
- 不把量化/科研 Team 与业务 Workflow 引入 Nico core。

## 9. 下一阶段入口

Goal I 执行迁移计划 U6：实现 `plan_and_execute`、不可变 Plan revisions、执行验证、Reflection/Replan 和 Completion Evaluation。开始前读取本 Handoff、迁移计划、`docs/runtime.md`、`docs/state-machines.md`、Goal Status 和 Feature Matrix；必须复用本阶段的 ReAct loop、checkpoint、ModelCall、RunStep 和 Tool Gateway 恢复边界。
