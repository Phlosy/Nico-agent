# 状态机基线

状态值在领域枚举中定义，API 不接受未知字符串。每次转换必须校验触发者、前置条件和租户权限，并在同一事务追加 Event 与 AuditRecord。

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

## Task

```mermaid
stateDiagram-v2
    [*] --> Created
    Created --> Assigned: assign Agent/Team
    Assigned --> Running: first Run starts
    Running --> WaitingForReview: execution result submitted
    WaitingForReview --> RevisionRequired: review rejected
    RevisionRequired --> Running: revision Run starts
    WaitingForReview --> Completed: review approved / no review required
    Created --> Cancelled: cancel
    Assigned --> Cancelled: cancel
    Running --> Cancelled: cancel accepted
    Running --> Failed: terminal policy reached
    RevisionRequired --> Failed: retry policy exhausted
```

## Run

```mermaid
stateDiagram-v2
    [*] --> Pending
    Pending --> Planning: worker lease acquired
    Planning --> Running: plan accepted
    Running --> WaitingForTool: tool requested
    WaitingForTool --> Running: tool result recorded
    Running --> WaitingForApproval: gated action requested
    WaitingForApproval --> Running: approved
    WaitingForApproval --> Failed: rejected
    Planning --> Paused: pause requested
    Running --> Paused: pause/checkpoint
    Paused --> Running: resume
    Planning --> Completed: direct result
    Running --> Completed: result committed
    Pending --> Cancelled: cancel
    Planning --> Cancelled: cooperative cancel
    Running --> Cancelled: cooperative cancel
    WaitingForTool --> Cancelled: cancel tool + run
    Planning --> Failed: provider/error
    Running --> Failed: provider/tool/error
    Pending --> TimedOut: deadline
    Planning --> TimedOut: deadline
    Running --> TimedOut: deadline
```

所有终态不可逆。Retry 创建新的 Pending Run，不把 Failed/Cancelled/TimedOut 改回 Running。

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

## 审批

`Requested -> Approved | Rejected | Cancelled | Expired`。终态不可变；重新申请创建新 Approval。

## 共同转换规则

- 客户端提交 `expected_revision` 防止并发覆盖。
- 转换函数返回领域 Event；Repository 负责与状态原子持久化。
- 非法转换返回稳定错误码 `INVALID_STATE_TRANSITION`，不得静默修正。
- 运行中 Worker 失联不立即改变 Run；租约过期后由恢复器决定重新领取或 Failed。
- 状态机变更必须同步更新本文件、迁移、API Schema、SDK 和测试。
