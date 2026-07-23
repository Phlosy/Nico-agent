---
title: Agent Runtime 执行架构完善需求基线
type: refactor-requirements
date: 2026-07-23
origin: user-provided architecture plan
---

# Agent Runtime 执行架构完善需求基线

> 本文完整保留用户提供的架构完善计划，作为后续 Goal 实施计划的需求来源和范围基线。实际实施必须先对照当前代码审计，不得把本文对“当前缺失能力”的假设直接当作事实。

你现在需要对现有 Agent Runtime 的执行逻辑进行架构完善。

当前系统已经实现了基础的 Agent 工具调用循环：

```text
构造上下文
  ↓
调用模型
  ↓
模型是否请求工具？
  ├─ 否 → 得到最终回答 → 完成
  └─ 是
       ↓
     权限、风险、Schema、版本检查
       ↓
     是否需要人工确认？
       ├─ 是 → Run 暂停等待审批 → 批准后恢复
       └─ 否 → 执行工具
                    ↓
                 获得工具结果
                    ↓
             加入上下文，再调用模型
```

这套逻辑目前可以正常工作，因此本次任务不是推翻现有实现，也不是重新设计一套完全独立的 Runtime，而是在现有代码和数据结构基础上，将其逐步完善为一个：

* 可持久化
* 可暂停恢复
* 可崩溃恢复
* 可审计
* 可控制预算
* 可扩展模型和工具
* 可支持复杂任务
* 可识别多种 Agent Action

的完整 Agent 执行状态机。

---

# 一、首先审计当前实现

在修改代码前，先完整阅读现有实现，并输出一份简洁但具体的审计结果。

重点定位以下内容：

1. Run 的创建入口。
2. Run 的状态字段和状态变更逻辑。
3. 模型调用入口。
4. 上下文构造逻辑。
5. 模型返回值解析逻辑。
6. Tool Call 的识别方式。
7. 工具注册、解析和执行入口。
8. Schema、权限、风险和版本校验逻辑。
9. 人工审批的创建、暂停和恢复逻辑。
10. 工具结果如何重新加入上下文。
11. 最终回答如何生成。
12. Run、Step、模型调用和工具调用是否持久化。
13. Worker 崩溃或进程重启后是否能够恢复。
14. 是否存在最大循环次数、超时、Token、成本和工具调用次数限制。
15. 当前测试覆盖哪些执行分支。

审计完成后，明确说明：

```text
当前已经具备的能力
当前部分具备但不完整的能力
当前完全缺失的能力
建议复用的现有模块
必须重构的模块
可以暂缓实现的高级能力
```

不要仅根据设计文档判断，必须以实际代码为准。

---

# 二、总体改造原则

本次改造必须遵守以下原则：

## 1. 渐进式改造

不得直接删除或重写当前可工作的主流程。

优先采用：

* 抽象现有逻辑
* 增加状态和事件
* 增加兼容层
* 将内联逻辑提取为独立模块
* 为现有接口保留兼容行为

除非现有实现存在明确缺陷，否则不要进行无关重构。

## 2. 确定性逻辑由 Runtime 控制

模型只负责决定下一步执行意图。

以下逻辑不得交给模型自行决定：

* Run 状态转换
* 权限判断
* 风险判断
* 是否必须审批
* 工具参数 Schema 校验
* 工具超时与重试
* 幂等判断
* 最大执行次数
* Token 和成本预算
* Run 是否取消
* Run 是否超时
* 最终是否满足系统级完成条件
* 失败状态如何持久化

## 3. 所有重要步骤必须可持久化

不能只依赖 Worker 内存保存执行状态。

至少保证以下信息可以持久化：

* Run 当前状态
* 当前执行步骤
* 当前模型调用
* 模型返回的 Action
* Tool Call
* 工具执行结果
* 审批请求
* 用户输入请求
* 错误信息
* 当前预算使用量
* 当前计划版本
* 最终回答
* 生成的 Artifact
* 执行事件和审计记录

## 4. 兼容现有模型协议

不要让平台执行层直接绑定某一家模型的返回结构。

例如 OpenAI、Anthropic、DeepSeek、Hermes 或其他模型返回的内容，应先经过 Provider Adapter，再转换为统一的 Agent Action。

---

# 三、将现有循环升级为 Run 状态机

在当前 Run 模型基础上，补充或统一以下状态。

如果现有状态名称不同，可以保留现有名称，但需要建立清晰的语义映射。

```text
CREATED
QUEUED
PREPARING
PLANNING
RUNNING
WAITING_APPROVAL
WAITING_USER_INPUT
WAITING_EXTERNAL
PAUSED
CANCELLING
COMPLETING
COMPLETED
FAILED
CANCELLED
TIMED_OUT
BUDGET_EXCEEDED
```

推荐状态转换：

```text
CREATED
  ↓
QUEUED
  ↓
PREPARING
  ↓
PLANNING 或 RUNNING
  ↓
RUNNING
  ├─ WAITING_APPROVAL
  │    ├─ 批准 → RUNNING
  │    └─ 拒绝 → RUNNING 或 FAILED
  │
  ├─ WAITING_USER_INPUT
  │    └─ 用户回复 → RUNNING
  │
  ├─ WAITING_EXTERNAL
  │    └─ 外部条件完成 → RUNNING
  │
  ├─ PAUSED
  │    └─ 恢复 → RUNNING
  │
  ├─ COMPLETING
  │    └─ COMPLETED
  │
  ├─ FAILED
  ├─ CANCELLED
  ├─ TIMED_OUT
  └─ BUDGET_EXCEEDED
```

要求：

1. 状态变更必须经过统一方法。
2. 不允许业务代码在任意位置直接修改 Run 状态。
3. 每次状态变更必须生成 Event。
4. 非法状态转换必须被拒绝。
5. 每个暂停状态必须有明确的恢复入口。
6. Worker 重启后可以通过数据库状态重新调度。
7. Run 必须能够被用户取消。
8. 工具执行期间收到取消请求时，需要有明确处理语义。

建议实现类似：

```ts
transitionRunState({
  runId,
  from,
  to,
  reason,
  metadata,
});
```

并定义允许的状态转换表，而不是在代码中散落大量状态判断。

---

# 四、定义统一的 Agent Action 协议

当前实现可能只识别两种情况：

```text
模型返回文本
模型返回 Tool Call
```

需要将其扩展为统一的 Agent Action。

至少支持：

```ts
type AgentAction =
  | FinalAction
  | ToolCallAction
  | AskUserAction
  | UpdatePlanAction
  | WaitAction
  | DelegateAction;
```

参考结构：

```ts
interface FinalAction {
  type: "final";
  content: string;
  citations?: Citation[];
  artifacts?: ArtifactRef[];
}

interface ToolCallAction {
  type: "tool_call";
  callId: string;
  toolName: string;
  toolVersion?: string;
  arguments: unknown;
  rationale?: string;
}

interface AskUserAction {
  type: "ask_user";
  question: string;
  inputSchema?: unknown;
  reason?: string;
}

interface UpdatePlanAction {
  type: "update_plan";
  plan: AgentPlan;
  reason: string;
}

interface WaitAction {
  type: "wait";
  condition: WaitCondition;
  reason?: string;
}

interface DelegateAction {
  type: "delegate";
  agentId: string;
  task: string;
  contextRefs?: string[];
  budget?: AgentBudget;
}
```

要求：

1. 为每一种模型 Provider 编写 Adapter。
2. Provider Adapter 将原始模型响应转换为统一 Action。
3. Runtime 主循环只处理统一 Action。
4. Runtime 主循环不得直接解析某个厂商特有字段。
5. 非法或无法解析的模型输出必须进入结构化错误处理。
6. 对无效输出支持有限次数的纠错重试。
7. 每次原始模型响应和解析后的 Action 都应持久化。

本阶段可以先完整实现：

```text
final
tool_call
ask_user
update_plan
```

`wait` 和 `delegate` 如果当前系统没有实际需求，可以先定义协议和扩展点，不必一次实现全部执行能力。

---

# 五、重构 Agent 执行循环

将当前逻辑重构为统一的执行循环。

参考流程：

```text
加载 Run
  ↓
验证 Run 当前是否可执行
  ↓
检查取消、超时和预算
  ↓
构造本轮上下文
  ↓
调用模型
  ↓
持久化 ModelInvocation
  ↓
解析 AgentAction
  ↓
持久化 Action
  ↓
根据 Action 分发
  ├─ final
  ├─ tool_call
  ├─ ask_user
  ├─ update_plan
  ├─ wait
  └─ delegate
  ↓
更新 Run / Step
  ↓
决定继续、暂停、完成或失败
```

建议将主循环控制在较高抽象层，不要把所有细节堆积在一个函数中。

可以拆分为：

```text
RunOrchestrator
ContextBuilder
ModelGateway
ActionParser
ActionDispatcher
ToolGateway
ApprovalService
PlanService
CompletionValidator
BudgetManager
RunRepository
EventStore
```

参考伪代码：

```ts
async function executeRun(runId: string): Promise<void> {
  const run = await runRepository.getForExecution(runId);

  await runGuard.assertExecutable(run);
  await budgetManager.assertWithinBudget(run);

  const step = await stepService.startStep(run);

  try {
    const context = await contextBuilder.build(run);

    const invocation = await modelGateway.invoke({
      run,
      context,
    });

    const action = await actionParser.parse(invocation);

    await actionDispatcher.dispatch({
      run,
      step,
      invocation,
      action,
    });
  } catch (error) {
    await runFailureHandler.handle({
      run,
      step,
      error,
    });
  }
}
```

不要机械照抄伪代码，应根据现有项目结构进行合理落地。

---

# 六、完善 Tool Gateway

保留现有的：

* 权限检查
* 风险检查
* Schema 检查
* 版本检查
* 人工审批

并将工具执行统一收口到 Tool Gateway。

完整管线建议为：

```text
ToolCall
  ↓
解析工具定义
  ↓
检查工具是否存在
  ↓
解析工具版本
  ↓
参数反序列化
  ↓
Schema 校验
  ↓
参数规范化和默认值补全
  ↓
调用者权限校验
  ↓
资源作用域校验
  ↓
风险分级
  ↓
策略判断
  ↓
是否需要审批
  ↓
幂等键生成
  ↓
创建 ToolExecution
  ↓
执行工具
  ↓
超时、重试和取消处理
  ↓
结果标准化
  ↓
结果安全处理
  ↓
持久化 Observation
  ↓
加入下一轮上下文
```

统一工具结果格式，例如：

```ts
interface ToolResult {
  callId: string;
  toolName: string;
  toolVersion?: string;

  status:
    | "success"
    | "partial_success"
    | "validation_error"
    | "permission_denied"
    | "business_error"
    | "timeout"
    | "transport_error"
    | "cancelled"
    | "unknown";

  output?: unknown;

  error?: {
    code: string;
    message: string;
    retryable: boolean;
    details?: unknown;
  };

  artifacts?: ArtifactRef[];

  metrics: {
    startedAt: string;
    finishedAt: string;
    durationMs: number;
    retryCount: number;
  };
}
```

## 工具错误处理原则

工具失败后，不应全部无脑返回模型。

Runtime 应先进行确定性处理：

```text
网络异常
  → 满足条件时自动重试

超时
  → 根据工具策略决定重试或失败

Schema 错误
  → 不执行工具，将结构化错误返回模型

权限不足
  → 禁止重试，记录审计事件

版本不匹配
  → 尝试解析兼容版本，否则失败

幂等写操作结果未知
  → 先查询已有执行状态，避免重复写入

部分成功
  → 明确保存成功项和失败项

用户取消
  → 尽可能中止工具，并将 Run 转为取消流程
```

重试必须有限制，并采用明确策略，不能无限重试。

---

# 七、完善审批机制

当前已经支持“需要审批时暂停 Run”，需要进一步明确审批对象和恢复语义。

ApprovalRequest 至少包含：

```text
approvalId
runId
stepId
toolCallId
toolName
toolVersion
原始参数
规范化参数
资源作用域
风险等级
风险原因
预计副作用
是否可逆
审批状态
审批人
审批时间
审批意见
修改后的参数
```

审批结果至少支持：

```text
APPROVED
REJECTED
APPROVED_WITH_CHANGES
EXPIRED
CANCELLED
```

恢复规则：

```text
APPROVED
  → 使用原参数恢复执行

APPROVED_WITH_CHANGES
  → 对修改后参数重新进行 Schema、权限、风险检查
  → 检查通过后恢复

REJECTED
  → 将审批拒绝作为 Observation 返回模型
  → 允许模型选择替代方案
  → 不应默认直接让整个 Run 失败

EXPIRED
  → 根据策略转为 WAITING_APPROVAL、FAILED 或 CANCELLED
```

同一个 Tool Call 恢复时不得重复创建多个无意义的审批请求。

---

# 八、增加预算与循环保护

为 Run 增加执行预算，并在每轮模型调用和工具调用前后更新。

至少支持：

```text
maxIterations
maxModelCalls
maxToolCalls
maxWallClockTime
maxInputTokens
maxOutputTokens
maxTotalTokens
maxCost
maxRetriesPerTool
maxConsecutiveErrors
maxRepeatedActions
```

需要检测重复行为，例如：

```text
连续多次使用相同工具和相同参数
连续生成相同 Tool Call
连续得到同类错误但策略没有变化
连续更新相同 Plan
```

检测到重复后：

1. 生成明确的 Runtime Observation。
2. 告诉模型该动作已经重复。
3. 要求模型改变策略。
4. 超过阈值后终止 Run。
5. 将 Run 状态设为 FAILED 或 BUDGET_EXCEEDED，并记录明确错误码。

不要只使用一个笼统的“最大循环次数”。

---

# 九、完善上下文构造

不要在每一轮无限追加全部历史。

将上下文拆分为：

```text
Immutable Input
- System Prompt
- 原始用户请求
- AgentVersion
- ModelVersion
- ToolVersion
- PromptVersion
- SkillVersion
- 固化的运行策略

Working Context
- 当前目标
- 当前 Plan
- 当前步骤
- 已确认事实
- 未解决问题
- 当前预算
- 当前可用工具

Recent Trajectory
- 最近若干次模型调用
- 最近工具调用
- 最近工具结果
- 最近错误和审批结果

Compressed History
- 更早历史的结构化摘要

External References
- 大型工具输出
- 文件
- Artifact
- 数据集
- 外部结果引用
```

定义上下文优先级：

```text
P0：系统安全和平台约束，不可裁剪
P1：用户当前目标，不可裁剪
P2：当前 Plan 和当前步骤
P3：最近关键 Observation
P4：历史摘要
P5：原始长输出，可裁剪或外置
```

要求：

1. 上下文构造逻辑集中在 ContextBuilder。
2. 不允许各模块随意向 Prompt 拼接内容。
3. 大型工具结果不要重复完整注入。
4. 工具原始结果应持久化，模型上下文中可以只放摘要和引用。
5. 压缩前后都要保留原始轨迹，不能因为上下文压缩丢失审计能力。
6. 记录每轮上下文的 Token 估算和实际使用量。

---

# 十、增加 Plan 机制，但避免过度规划

不是所有请求都必须先规划。

可以根据以下条件决定是否进入 Planning：

```text
任务是否需要多个工具
任务是否包含多个依赖步骤
任务是否有高风险操作
任务是否需要生成多个 Artifact
任务是否需要子任务
任务是否预计超过指定执行轮次
用户是否明确要求计划
Agent 配置是否强制规划
```

简单任务可以直接进入 RUNNING。

Plan 至少包含：

```ts
interface AgentPlan {
  revision: number;
  goal: string;
  steps: PlanStep[];
  createdAt: string;
  reason?: string;
}

interface PlanStep {
  id: string;
  title: string;
  description?: string;
  status:
    | "pending"
    | "running"
    | "completed"
    | "failed"
    | "skipped"
    | "blocked";
  dependencies?: string[];
  expectedTools?: string[];
  completionCriteria?: string[];
}
```

Plan 更新必须：

1. 创建新 Revision。
2. 保留旧版本。
3. 记录修改原因。
4. 校验依赖是否有效。
5. 校验是否新增高风险步骤。
6. 校验是否超出预算。
7. 必要时重新触发审批。

不要让 Plan 成为纯展示文本，Plan Step 应与实际执行 Step 有关联。

---

# 十一、增加 Completion Gate

模型返回 `final` 时，不应立即无条件将 Run 标记为 COMPLETED。

先执行确定性的 Completion Gate。

检查：

```text
是否存在仍在 pending 或 running 的必需步骤
是否存在未完成的强制工具调用
是否存在未处理的审批
是否存在未处理的用户输入请求
是否存在工具执行状态未知
是否已经生成承诺的 Artifact
是否满足输出格式约束
是否超出预算
是否存在未处理的高风险错误
```

如果 Completion Gate 不通过：

1. 不要完成 Run。
2. 生成结构化反馈。
3. 允许模型继续修正。
4. 超过修正次数后失败。

Completion Gate 只负责系统可以确定的事实，不要试图用大量模糊规则判断回答“是否足够聪明”。

对于高风险或复杂任务，可以预留 Verifier 接口，但不要在所有任务中默认增加第二次模型调用。

---

# 十二、完善持久化模型

结合现有数据库结构进行增量设计。

至少需要覆盖以下概念：

```text
Run
RunStep
ModelInvocation
AgentAction
ToolCall
ToolExecution
Observation
ApprovalRequest
UserInputRequest
PlanRevision
Artifact
RunEvent
AuditLog
UsageRecord
```

不一定要求每一个概念都独立建表。

可以根据现有架构选择：

* 独立表
* Event 表
* JSON 快照
* 关联实体

但是必须满足：

1. 可以查询完整执行轨迹。
2. 可以知道当前恢复点。
3. 可以审计每次权限和风险决策。
4. 可以区分模型请求工具与工具实际执行。
5. 可以区分工具原始结果与注入模型的摘要结果。
6. 可以统计 Token、成本和调用次数。
7. 可以从指定 Step 重放或分叉。
8. 可以判断某个写操作是否已经执行。

每个关键实体应具备：

```text
id
runId
stepId
status
input
output
error
createdAt
startedAt
finishedAt
metadata
versionSnapshot
```

根据实际情况进行裁剪，不要机械制造大量无价值字段。

---

# 十三、实现崩溃恢复

这是本次完善的重点之一。

需要分析并实现以下场景：

```text
模型调用前 Worker 崩溃
模型请求已发出但响应未持久化时崩溃
Tool Call 已创建但尚未执行时崩溃
工具执行中 Worker 崩溃
工具已经执行成功但结果未持久化时崩溃
审批通过后恢复前 Worker 崩溃
Run 标记 COMPLETING 后 Worker 崩溃
```

要求：

1. 每个 Step 具有明确状态。
2. 调度器能够扫描可恢复 Run。
3. 对只读工具可以按策略安全重试。
4. 对写操作必须使用幂等键或执行状态查询。
5. 无法确认执行结果时，标记为 `unknown`，不能直接假设失败并重复执行。
6. 恢复过程必须生成 Event。
7. 避免同一个 Run 被多个 Worker 并发执行。
8. 使用数据库锁、租约或等价机制保证单 Run 执行互斥。
9. Worker 租约过期后可以被其他 Worker 接管。
10. 恢复逻辑需要自动化测试。

---

# 十四、可观测性与审计

为每个 Run 和 Step 增加结构化事件。

建议事件包括：

```text
run.created
run.queued
run.started
run.state_changed
run.paused
run.resumed
run.cancel_requested
run.cancelled
run.completed
run.failed

model.invocation_started
model.invocation_completed
model.invocation_failed
model.action_parsed
model.action_invalid

tool.call_requested
tool.validation_failed
tool.permission_denied
tool.approval_required
tool.execution_started
tool.execution_completed
tool.execution_failed
tool.execution_unknown

approval.created
approval.approved
approval.rejected
approval.expired

plan.created
plan.updated
plan.step_started
plan.step_completed
plan.step_failed

context.built
context.compressed
budget.warning
budget.exceeded
```

日志中必须包含：

```text
runId
stepId
invocationId
toolCallId
toolExecutionId
approvalId
workerId
traceId
```

敏感参数不得直接明文写入普通日志，应进行脱敏或保存安全引用。

---

# 十五、测试要求

本次改造必须同时补充测试，不接受只有实现没有测试。

至少覆盖：

## 基础流程

```text
模型直接返回 final
模型请求一个只读工具后返回 final
模型连续调用多个工具
工具结果正确加入下一轮上下文
```

## 工具校验

```text
工具不存在
工具版本不匹配
参数 Schema 错误
权限不足
高风险操作要求审批
审批通过后恢复
审批拒绝后返回模型
审批参数修改后重新校验
```

## 状态机

```text
非法状态转换被拒绝
WAITING_APPROVAL 可以恢复
WAITING_USER_INPUT 可以恢复
PAUSED 可以恢复
已完成 Run 不可继续执行
已取消 Run 不可继续执行
```

## 预算保护

```text
超过最大模型调用次数
超过最大工具调用次数
超过最大循环次数
超过 Token 预算
超过时间预算
重复动作检测
连续错误检测
```

## 错误处理

```text
模型输出无法解析
工具超时
工具网络失败并成功重试
工具网络失败并超过重试次数
工具部分成功
工具执行状态未知
```

## 崩溃恢复

```text
Step 创建后崩溃
工具执行前崩溃
工具执行成功后持久化前崩溃
审批通过后恢复
Worker 租约过期后被其他 Worker 接管
同一 Run 不会被两个 Worker 同时执行
```

## Completion Gate

```text
存在未完成必需步骤时不能完成
存在待审批请求时不能完成
Artifact 不存在时不能完成
所有条件满足后正常完成
```

---

# 十六、实施顺序

不要一次性大规模重写。

按照以下阶段实施。

## 第一阶段：状态机和持久化基础

实现：

```text
Run 状态转换器
RunStep
统一 Event
执行互斥
取消
暂停和恢复
基础崩溃恢复
```

完成后运行测试并汇报。

## 第二阶段：统一 Agent Action

实现：

```text
Provider Adapter
AgentAction
ActionParser
ActionDispatcher
final
tool_call
ask_user
update_plan
```

保持旧协议兼容。

## 第三阶段：Tool Gateway

实现：

```text
统一工具解析
Schema
版本
权限
资源作用域
风险
审批
幂等
超时
重试
结果标准化
```

## 第四阶段：预算和上下文管理

实现：

```text
BudgetManager
重复动作检测
ContextBuilder
上下文优先级
大型工具结果外置
历史压缩接口
```

## 第五阶段：Plan 和 Completion Gate

实现：

```text
PlanRevision
PlanStep
Completion Gate
复杂任务验证扩展点
```

每个阶段结束后：

1. 运行格式化。
2. 运行静态检查。
3. 运行单元测试。
4. 运行集成测试。
5. 汇报新增能力。
6. 汇报仍未实现的能力。
7. 不要声称未验证的能力已经完成。

---

# 十七、最终交付内容

最终需要输出：

## 1. 当前实现审计

包含：

```text
现有执行流程
关键模块和文件
当前数据模型
已支持能力
缺失能力
主要风险
```

## 2. 实际改造说明

包含：

```text
修改了哪些文件
新增了哪些模块
状态机如何工作
Action 如何解析
工具调用如何执行
审批如何暂停和恢复
预算如何生效
崩溃如何恢复
上下文如何构造
Completion Gate 如何判断
```

## 3. 数据库变更

包含：

```text
新增表
新增字段
索引
唯一约束
迁移方式
兼容性影响
回滚方式
```

## 4. API 或协议变更

包含：

```text
新增接口
修改接口
废弃接口
兼容层
事件格式
Action 格式
```

## 5. 测试结果

必须给出实际执行结果，例如：

```text
单元测试：通过数量 / 失败数量
集成测试：通过数量 / 失败数量
E2E：通过数量 / 失败数量
静态检查结果
格式化结果
```

不得虚构测试结果。

## 6. 未完成事项

明确列出：

```text
当前暂未实现
当前只预留接口
存在的技术债
建议下一阶段继续完成的内容
```

---

# 十八、禁止事项

本次任务中禁止：

1. 不阅读代码就直接开始重写。
2. 删除当前已经工作的工具调用流程。
3. 为了架构整洁进行无关代码重构。
4. 将权限、风险和预算判断交给模型。
5. 让 Worker 仅依赖内存状态。
6. 在没有幂等保护时自动重试写操作。
7. 将所有完整工具结果无限加入上下文。
8. 将所有任务都强制进入 Planning。
9. 将所有任务都强制调用 Verifier。
10. 只新增状态字段但不实现真实状态转换约束。
11. 只实现暂停但不能恢复。
12. 声称支持崩溃恢复，但实际只能重新从头运行。
13. 忽略旧数据和旧接口兼容性。
14. 修改完成后不运行测试。
15. 用 Mock 结果冒充真实执行验证。

---

# 十九、最终目标

改造后的完整主流程应当接近：

```text
接收任务
  ↓
创建 Run
  ↓
固化 Agent、模型、工具、Prompt、Skill 和策略版本
  ↓
PREPARING
  ↓
输入预处理和上下文构造
  ↓
必要时创建 Plan
  ↓
RUNNING
  ↓
检查取消、超时、预算和重复行为
  ↓
调用模型
  ↓
转换为统一 AgentAction
  ↓
Action Dispatcher
  ├─ final
  │    ↓
  │  Completion Gate
  │    ├─ 通过 → COMPLETED
  │    └─ 不通过 → 返回模型继续修正
  │
  ├─ tool_call
  │    ↓
  │  Tool Gateway
  │    ↓
  │  Schema、版本、权限、作用域、风险检查
  │    ↓
  │  是否需要审批
  │    ├─ 是 → WAITING_APPROVAL
  │    └─ 否 → 执行工具
  │                 ↓
  │              标准化结果
  │                 ↓
  │              持久化 Observation
  │                 ↓
  │              返回下一轮模型调用
  │
  ├─ ask_user
  │    ↓
  │  WAITING_USER_INPUT
  │    ↓
  │  用户回复后恢复
  │
  ├─ update_plan
  │    ↓
  │  创建 PlanRevision
  │    ↓
  │  校验并继续执行
  │
  ├─ wait
  │    ↓
  │  WAITING_EXTERNAL
  │
  └─ delegate
       ↓
     创建受限子 Run
       ↓
     汇总结果后恢复主 Run
```

最终应形成以下架构边界：

```text
Agent Runtime
├── Run Orchestrator
├── Run State Machine
├── Context Engine
├── Model Gateway
├── Provider Adapter
├── Agent Action Protocol
├── Action Dispatcher
├── Tool Gateway
├── Approval Service
├── Budget Manager
├── Planning Service
├── Completion Validator
├── Persistence Layer
├── Event and Audit System
└── Recovery Scheduler
```

请先完成代码审计，再制定与现有代码相匹配的实施方案，然后直接开始第一阶段改造。

不要停留在设计建议层面。最终必须给出真实代码修改、数据库迁移、测试和验证结果。
