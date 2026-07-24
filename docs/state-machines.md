# 状态机

状态值在领域枚举中定义，API 不接受未知字符串。每次转换必须校验触发者、前置条件和租户权限，并在同一事务追加 Event 与 AuditRecord。

## Project、Membership 与 Session

```mermaid
stateDiagram-v2
    [*] --> ActiveProject
    ActiveProject --> ArchivedProject: archive + revision
    state ActiveProject {
        [*] --> ActiveMember
        ActiveMember --> PausedMember: pause
        PausedMember --> ActiveMember: restore
        ActiveMember --> RemovedMember: remove
        PausedMember --> RemovedMember: remove
    }
```

active shared Project 由数据库部分唯一索引保证恰有一个 active Lead。Lead 替换在
单事务中降级旧 Lead、提升新 Lead 并推进 Project revision。成员暂停/移除会阻止
新 Task、Session 写入和委派，但不删除历史；Project 归档会归档所有稳定 Session
并取消活动监督 Run 树。

ProjectSession 使用 `active -> paused -> active`，Project 归档时进入 `archived`。
成员恢复复用同一 Session；AgentVersion 变化只轮换 current Conversation 指针。

## ProjectSupervisionCycle

```mermaid
stateDiagram-v2
    [*] --> Pending
    Pending --> Claimed: database claim + lease
    Claimed --> Running: Lead Task/Run materialized
    Claimed --> Pending: lease expired
    Running --> Completed: facts + optional narrative committed
    Pending --> Cancelled: Project archive
    Claimed --> Cancelled: Project archive
    Running --> Cancelled: Run tree cancelled
    Claimed --> Failed: materialization error
    Running --> Failed: terminal execution error
```

`Completed`、`Failed`、`Cancelled` 不可回退。同一 Project/cadence slot 唯一；手动
sync 通过 Idempotency-Key 防止逻辑重复。

## RunIntervention

```mermaid
stateDiagram-v2
    [*] --> Pending
    Pending --> Consumed: safe model boundary
    Pending --> Withdrawn: operator + revision
    Pending --> Rejected: validation/runtime failure
```

`Consumed`、`Withdrawn`、`Rejected` 均为终态。Worker 先把 Intervention ID、内容
Hash 和模型边界键冻结进 RuntimeSession/checkpoint，再与 ContextSnapshot/Event/Audit
同事务消费；崩溃恢复使用同一冻结集合，不会重复向模型注入。

## Agent

```mermaid
stateDiagram-v2
    [*] --> Draft
    Draft --> Ready: publish valid AgentVersion
    Ready --> Running: active Run starts
    Running --> Ready: no active Runs
    Running --> Paused: operator/provider pause
    Paused --> Running: resume
    Draft --> Archived: archive
    Ready --> Archived: archive
    Paused --> Archived: archive
    Running --> Error: unrecoverable agent configuration error
    Error --> Ready: publish/fallback valid version
    Archived --> Ready: restore valid version
```

Agent 的 Running 是派生/协调状态，不授权调用工具；Run 和 Tool Policy 才是执行权限来源。

## AgentVersion

```mermaid
stateDiagram-v2
    [*] --> Draft
    Draft --> Published: publish
    Published --> Superseded: publish replacement
    Superseded --> Published: rollback
```

发布后的配置行不提供修改 API；回滚重新激活既有不可变版本。

## Project

```mermaid
stateDiagram-v2
    [*] --> Active
    Active --> Archived: archive
    Archived --> Active: restore (reserved)
```

API 实现创建、读取、更新与归档。`Archived -> Active` 已由领域规则保留，但当前没有 Project 恢复路由。

## Conversation 与 ConversationTurn

```mermaid
stateDiagram-v2
    [*] --> Active
    Active --> Archived: archive with no active Run
```

Conversation 归档后不可重新激活，也不能创建新 Turn；历史仍可读取。Conversation 创建时固定一个 Published AgentVersion。该版本之后变为 Superseded 仍可供旧会话执行，但 Agent 必须保持 Ready；不得静默切换到当前版本。

ConversationTurn 是 Run 状态的持久化投影，不是独立的执行状态机：

```mermaid
stateDiagram-v2
    [*] --> Queued: Turn + Task + Pending Run committed
    Queued --> Running: Run planning/running/tool/subagent/paused
    Queued --> WaitingForApproval: Run waits for approval
    Running --> WaitingForApproval: Run waits for approval
    WaitingForApproval --> Running: Run resumes
    Queued --> Completed: direct completion
    Running --> Completed: Run completed
    Queued --> Failed: Run failed/timed out
    Running --> Failed: Run failed/timed out
    Queued --> Cancelled: Run cancelled
    Running --> Cancelled: Run cancelled
    WaitingForApproval --> Cancelled: Run cancelled
```

Run 是权威事实。数据库投影在同一事务把终态 output、usage、error 和 artifact refs 写入 Turn；取消调用统一 Run 树取消服务。Failed/TimedOut 的最新 Turn 可用 Run ID + revision 创建新 attempt，Turn 原子切换到该 Run 并保持冻结 AgentVersion；Cancelled 不可重试。一个 Conversation 同时只允许一个非终态 Run，Turn、Task、首次 Run 与相关 Event/Audit 在一个事务提交。

## Task

```mermaid
stateDiagram-v2
    [*] --> Created
    Created --> Assigned: assign Agent
    Assigned --> Running: first Run starts
    Running --> WaitingForReview: execution result submitted
    WaitingForReview --> RevisionRequired: review rejected
    RevisionRequired --> Running: revision Run starts
    WaitingForReview --> Completed: review approved / no review required
    Created --> Cancelled: cancel
    Assigned --> Cancelled: cancel
    Running --> Cancelled: cancel accepted
    Running --> Failed: terminal policy reached
    Failed --> Running: explicit retry creates new Run
    RevisionRequired --> Failed: retry policy exhausted
```

## Run

Run 的粗粒度状态由 `RunLifecycleAuthority` 统一写入。调用者必须传入已有事务和
已经 `FOR UPDATE` 锁定的 Run；authority 不开启或提交嵌套事务。每次成功转换同时
推进 `revision`/`lifecycle_revision`，保存有界且脱敏的 reason/metadata，并追加一条
Run Event 和 AuditRecord。非法转换、终态后的转换和旧租约持有者写入在任何事实
改变前失败。

```mermaid
stateDiagram-v2
    [*] --> Pending
    Pending --> Planning: worker lease acquired
    Planning --> Running: plan accepted
    Running --> WaitingForTool: tool requested
    WaitingForTool --> Running: tool result recorded
    Running --> WaitingForSubagent: delegation checkpoint committed
    WaitingForSubagent --> Running: all direct children terminal
    Running --> WaitingForApproval: gated action requested
    WaitingForApproval --> Running: approved
    WaitingForApproval --> Failed: rejected
    Running --> WaitingForUserInput: durable request + checkpoint
    WaitingForUserInput --> Running: answered / expired + wake
    WaitingForUserInput --> Failed: recovery policy
    WaitingForUserInput --> Cancelled: Run cancellation wins
    Planning --> Paused: pause requested
    Running --> Paused: pause/checkpoint
    Paused --> Running: resume
    Planning --> Completed: direct result
    Running --> Completed: result committed
    Pending --> Cancelled: cancel
    Planning --> Cancelled: cooperative cancel
    Running --> Cancelled: cooperative cancel
    WaitingForTool --> Cancelled: cancel tool + run
    WaitingForSubagent --> Cancelled: cancel Run tree
    Planning --> Failed: provider/error
    Running --> Failed: provider/tool/error
    Pending --> TimedOut: deadline
    Planning --> TimedOut: deadline
    Running --> TimedOut: deadline
```

所有终态不可逆。Failed/TimedOut 的 Retry 创建新的 Pending Run，不把旧 Run 改回 Running；Cancelled 同时终止 Task，不可重试。

`waiting_for_user_input` 只能由 UserInput service 在已经提交的 AskUserAction、
pre-action checkpoint 和唯一 `UserInputRequest` 同时存在时进入；通用 Run
transition API 不能写入该状态。答案、到期和取消都先锁 Run，再解析请求边界。
答案只唤醒一次；终态 Run 的触发器会取消 requested 行，迟到答案不能复活它。
CLI/API 交互和模型侧 live `ask_user` Schema 仍由后续阶段启用。

详细的 RuntimeLoopState 与 Run 映射如下：

| RunStatus | RuntimeLoopState |
| --- | --- |
| planning | initializing, planning |
| running | reasoning, executing, searching, fetching, observing, delegating, reflecting, finalizing, cancelling |
| waiting_for_tool | waiting_for_tool |
| waiting_for_approval | waiting_for_approval |
| waiting_for_user_input | waiting_for_user_input |
| waiting_for_subagent | waiting_for_subagent |
| paused | paused |
| completed | completed |
| failed | failed, budget_exhausted |
| cancelled | cancelled |
| timed_out | timed_out |

approval、user-input 和 subagent 的持久等待不进入普通领取集合；对应 wake 只允许
成功一次并清除旧租约。`waiting_for_tool` 表示仍在作用边界内，只有持有租约的
Worker 可以完成；租约过期后才允许恢复领取。SQL claim、approval expiry、
user-input reconcile 和 child
reconciler 是具名例外端口，并与 Python 转换矩阵和 claimability 函数做 PostgreSQL
一致性测试。取消与 wake 并发时，最终取消不可被迟到 wake 覆盖。

## UserInputRequest

```mermaid
stateDiagram-v2
    [*] --> Requested: dispatched AskUserAction + checkpoint
    Requested --> Answered: schema + revision + idempotency accepted
    Requested --> Expired: deadline reconciler
    Requested --> Cancelled: Run becomes terminal
```

`Answered`、`Expired`、`Cancelled` 均不可改写。requested 行在每个 Run/Action 边界
唯一；answer payload 只存于受保护字段，公开投影、Event 和 Audit 只包含 ID、状态、
request/answer Hash、revision 与到期时间。Answered 同事务完成原 AskUserAction 和
推进 batch cursor；Expired/Cancelled 阻断 Action 和 batch，不会执行任何 Tool
副作用。

## RunStep

```mermaid
stateDiagram-v2
    [*] --> Pending
    Pending --> Running: start
    Pending --> Cancelled: cancel
    Running --> Waiting: wait
    Waiting --> Running: resume
    Running --> Completed: commit output
    Running --> Failed: commit error
    Running --> Cancelled: cancel
    Waiting --> Failed: terminal error
    Waiting --> Cancelled: cancel
```

Completed、Failed、Cancelled 都是不可逆终态。RunStep 的状态、输出/错误、时间戳、Event 和 AuditRecord 在同一事务提交。

## Plan 与 PlanStep

Plan revision 使用 `Active -> Superseded | Completed | Failed`。目标、步骤、依赖、revision、来源 ModelCall 和 content hash 在创建后不可修改；需要修正时必须追加下一 revision。Superseded、Completed、Failed 都是不可逆终态。

PlanStep 使用 `Pending -> Running -> Completed`，验证失败时本次 RunStep 终结为 Failed，而 PlanStep 在 Reflection 决定前保留可恢复执行状态；若 Plan 被 Replan/Supersede，未完成 PlanStep 终结为 Failed。PlanStep 定义不可修改，已终结步骤不可覆盖。Reflection 和 Completion 不复用 Growth 域的 Evaluation，而是追加独立、不可修改的 RuntimeEvaluation。

## Delegation、Message 与 Artifact

Delegation 使用 `Proposed -> Accepted -> Running -> Completed|Failed|Cancelled`；也可从 Proposed 进入 Rejected/Cancelled。目标、父子 Run、预算、策略、权限快照和幂等指纹创建后不可改写，任何终态不可修改或删除。

AgentMessage 使用 `Queued -> Delivered -> Acknowledged`。Result 在 Child 终结事务中入队，Parent 恢复读取后确认；Cancel/SystemNotice 可直接 Delivered。消息正文有界，大内容只保留 Artifact 引用。

Artifact 使用 `Uploading -> Available|Failed`。Available 必须同时具备 SHA-256、size 和私有 object key，终态元数据不可改写。SharedArtifactLink 首版只允许 Child 向直接 Parent 授予 Active 链接；下载时再次校验状态、到期时间与 Run 可见性。

## Skill

```mermaid
stateDiagram-v2
    [*] --> Candidate
    Candidate --> Testing: validation starts
    Testing --> Candidate: tests failed with revision
    Testing --> Approved: tests pass and approval granted
    Approved --> Published: activate version
    Published --> Deprecated: replacement published
    Candidate --> Disabled: reject/disable
    Testing --> Disabled: unsafe/invalid
    Approved --> Disabled: approval revoked
    Published --> Disabled: emergency disable
    Deprecated --> Published: rollback active pointer
```

SkillVersion 使用更严格的不可变版本状态：

```mermaid
stateDiagram-v2
    [*] --> Draft
    Draft --> Testing: validation starts
    Testing --> Published: evaluation passed + approval + publish
    Testing --> Rejected: validation/evaluation failed
```

Rejected 的修订创建新 Draft 版本；Published 行不可更新。Skill 的 `Approved` 表示已有绑定 content hash 的通过 Evaluation 和 Approval，但尚未激活。灰度 deployment 不改变 SkillVersion 状态；推广或回滚只切换 active pointer/退役 deployment。

## Memory

```mermaid
stateDiagram-v2
    [*] --> Candidate
    Candidate --> Active: evaluation passed + approval + publish
    Candidate --> Candidate: failed evaluation retained
    Active --> Invalidated: superseded or operator invalidates
    Active --> Expired: expiry reached
    Active --> Deleted: tombstone
    Invalidated --> Deleted: tombstone
    Expired --> Deleted: tombstone
```

Candidate 不进入正式召回。内容修订创建同一 logical key 的新 Candidate 版本；Active/Invalidated/Expired/Deleted 的内容与来源不可改写。工作记忆必须有过期时间。`team` 是保留的枚举值，但 Nico core 不实现 Team 归属，因此该 scope 失败关闭。

## 成长发布审批

`Requested -> Approved | Rejected | Cancelled | Expired`。终态不可变；重新申请创建新 Approval。

## 工具执行审批

ToolApprovalRequest 与 Memory/Skill 的成长发布 Approval 是不同聚合：

```mermaid
stateDiagram-v2
    [*] --> Requested
    Requested --> Approved: allow once / allow for Run
    Requested --> Rejected: operator rejects
    Requested --> Expired: deadline reached
    Requested --> Cancelled: authoritative Run cancelled
```

Approved 必须保存 `once` 或 `run` scope；其余终态 scope 为 `none`。终态行不可再次修改，决定使用 revision 和幂等键。requested 对应 Pending ToolCall、Waiting RunStep 与 `waiting_for_approval` Run；终态决定唤醒 Run，Worker 从已保存 checkpoint 恢复。

## 共同转换规则

- 客户端提交 `expected_revision` 防止并发覆盖。
- 转换函数返回领域 Event；Repository 负责与状态原子持久化。
- 非法转换返回稳定错误码 `INVALID_STATE_TRANSITION`，不得静默修正。
- 运行中 Worker 失联不立即改变 Run；租约过期后，新 Worker 只有在 Provider 声明 resume 且持久化 RuntimeSession 可用时恢复，否则稳定转为 Failed。
- 首次领取时按 AgentVersion 显式 Provider 选择执行器；历史数据才使用有弃用期的 legacy resolver，并写入 Event/Audit telemetry。
- 恢复时以 RuntimeSession 中的 Provider/version/protocol 为权威事实；Adapter 未启用、缺失或不兼容时失败关闭，不得切换到 Nico Native。
- 状态机变更必须同步更新本文件、迁移、API Schema 和测试。
