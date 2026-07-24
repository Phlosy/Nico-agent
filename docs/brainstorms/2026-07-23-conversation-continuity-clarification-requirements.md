---
title: Conversation Continuity and Clarification Runtime Requirements
type: fix
date: 2026-07-23
source_sha256: 4962a6af9ca59f47be71c16cd83972d98c67b8c54ff9806d655891603a0259e1
---

你现在需要完善 Nico Agent Runtime 对“残缺输入、错别字、省略表达和上下文承接问题”的处理逻辑。

当前发现了一个明确问题。

实际对话如下：

```text
用户：帮我检索一下今天的日期

Agent：
根据平台提供的时间戳，今天的日期是：
2026年7月23日（星期四）
（基于协调世界时 UTC）

用户：你平台是怎么提供de
```

当前 Agent 返回：

```text
你的消息似乎没有发送完整——“你平台是怎么提供de”后面好像断了。

你是想问：
- 平台是怎么提供日期/时间的？
- 平台是怎么提供答案/服务的？
- 还是其他方面？

请把问题说完。
```

这个行为不合理。

结合上一轮上下文，用户最可能是在问：

```text
“你平台是怎么提供时间戳的？”
```

即使当前输入存在错别字和截断，意图仍然高度明确，而且错误理解的风险很低。

更合理的行为应该是：

```text
平台通常会在创建本轮 Run 时，由服务端读取当前时间，并将时间信息作为系统上下文或运行时元数据传给模型。模型本身一般不能主动读取系统时钟，而是使用平台在请求中注入的时间。

我是按你在问“平台怎么提供时间戳”来理解的；如果你指的是其他内容，可以继续补充。
```

本次任务需要从 Work Loop、Context Builder、模型消息协议和 Agent Action 的角度系统修复该问题。

注意：不要立即修改代码。先审计当前实现，确认实际根因，再制定与现有代码匹配的改造方案。审计完成后直接进行实现和测试。

---

# 一、问题判断

当前问题很可能不是简单的“没有传历史上下文”，而是以下多个因素叠加：

```text
真实对话历史没有作为标准消息序列传递
+
历史被包装在 untrusted_data 或 JSON 数据块中
+
当前输入被重复注入
+
系统 Prompt 没有定义歧义输入处理策略
+
Work Loop 只区分 tool_calls 和普通文本
+
模型生成的澄清文本被直接当作 final
+
不存在 ask_user Action
+
不存在 Clarification Gate
```

不要直接接受上述判断，必须检查实际代码和真实模型请求，验证每一项是否存在。

---

# 二、首先审计当前实现

修改代码前，定位并阅读以下实现：

1. 用户消息进入 Runtime 的入口。
2. Conversation 历史读取逻辑。
3. Context Builder。
4. 系统 Prompt 的构造逻辑。
5. 当前用户输入的注入位置。
6. 历史消息的注入位置。
7. `untrusted_data` 的生成和用途。
8. 模型请求最终形成的 messages 数组。
9. 模型返回值解析逻辑。
10. Tool Call 的判断逻辑。
11. 无 Tool Call 时的结束逻辑。
12. Run 完成条件。
13. 是否已经存在 `ask_user`、`clarification` 或类似 Action。
14. 是否存在结构化输出协议。
15. 是否存在 Completion Gate。
16. 是否存在模型纠错重试。
17. 是否存在上下文相关测试。

重点检查并回答：

```text
上一轮 user 和 assistant 消息是否按原始 role 顺序传给模型？
历史是否只是被序列化成 JSON 后塞进一条 user/system 消息？
当前用户输入是否在多处重复出现？
工具结果、检索结果和真实对话历史是否混在同一个 untrusted_data 中？
模型输出非空文本时，Runtime 是否直接判定为 final？
Runtime 是否能区分“最终回答”和“向用户索取信息”？
```

请打印或通过测试捕获该场景下发送给模型的最终请求结构，但必须隐藏 API Key、Token、个人数据和敏感字段。

审计结果中明确列出：

```text
已确认根因
可能的次要因素
不是根因的部分
需要修改的模块
可以复用的现有模块
```

不要只根据设计文档或文件名判断，必须以实际代码路径和运行行为为准。

---

# 三、修复原则

本次改造必须遵循以下原则。

## 1. 输入不完整不等于意图不可推断

必须在 Runtime 和模型策略中明确区分：

```text
输入存在错别字或截断
输入语法不完整
意图存在歧义
必须阻塞并询问用户
```

这四件事不是等价的。

推荐原则：

```text
当前输入不完整
  ↓
结合最近对话是否存在明显占优的解释？
  ├─ 是
  │    ↓
  │  错误理解是否低风险？
  │    ├─ 是 → 直接回答，必要时简短声明采用的理解
  │    └─ 否 → 请求确认
  │
  └─ 否
       ↓
     是否存在多个相近解释？
       ├─ 是 → 请求澄清
       └─ 否 → 提供能够确定的部分，并询问一个关键问题
```

## 2. 不要把过度澄清完全交给模型决定

模型可以提出需要澄清，但不能天然拥有阻塞用户的最终决定权。

Runtime 需要有确定性的 Clarification Policy。

## 3. 真实对话历史必须保持消息语义

真实的用户和助手对话不能与网页内容、文件、工具返回、检索记忆完全等价地处理。

## 4. 不使用关键词识别自然语言澄清

禁止通过以下方式识别模型是否在追问：

```ts
output.includes("请补充")
output.includes("你是想问")
output.includes("消息没有发送完整")
```

这种方法语言相关、脆弱且容易误判。

必须通过结构化 Agent Action 区分。

## 5. 渐进式改造

不要推翻当前 Work Loop。

优先：

```text
修正消息构造
增加 Prompt 规则
增加 ask_user Action
增加 Clarification Gate
增加回归测试
```

---

# 四、修正上下文消息结构

首先检查当前模型请求是否类似：

```text
system
user:
  {
    "untrusted_data": {
      "recent_conversation_turns": [...]
    },
    "current_input": "你平台是怎么提供de"
  }
```

如果是，必须调整。

真实会话历史应该尽可能保持标准消息序列：

```json
[
  {
    "role": "system",
    "content": "系统约束与 Agent 指令"
  },
  {
    "role": "user",
    "content": "帮我检索一下今天的日期"
  },
  {
    "role": "assistant",
    "content": "根据平台提供的时间戳，今天的日期是……"
  },
  {
    "role": "user",
    "content": "你平台是怎么提供de"
  }
]
```

额外上下文再独立注入：

```text
系统约束
标准对话消息
当前 Run 状态
当前 Plan
工作记忆
检索记忆
工具 Observation
外部 untrusted data
```

至少区分以下类型：

```text
conversation_messages
working_context
retrieved_memory
tool_observations
external_untrusted_data
```

不要把真实对话历史与外部网页、工具输出和文件数据放入同一个不可信数据容器。

需要保留安全边界：

```text
网页、文件、工具返回仍然是 untrusted data
真实用户消息仍然可能包含 Prompt Injection，但它的对话角色不能丢失
对话角色优先级和数据可信性必须分别处理
```

注意：

```text
“标准 role 消息”
不等于
“完全信任消息内容”
```

消息身份和安全可信度应当是两个独立维度。

---

# 五、消除当前输入重复注入

检查当前用户输入是否同时出现在：

```text
messages 中最后一条 user message
current_user_input
task.input
run.input
context JSON
conversation history
```

如果相同内容被多次注入模型上下文，需要规范化。

原则：

1. 当前用户原始输入只保留一个主要消息位置。
2. 其他模块需要引用时使用结构化引用，而不是重复全文。
3. 保留原始输入供审计，但不要重复注入 Prompt。
4. 对截断输入尤其要避免重复，因为重复会强化“不完整”特征。
5. 添加测试，确保当前输入在最终模型消息中不会无意义地重复多次。

---

# 六、增加明确的歧义处理 Prompt

在系统 Prompt 或 Agent Runtime Prompt 中加入明确规则。

可以根据项目现有 Prompt 风格调整措辞，但语义必须完整：

```text
当用户输入存在错别字、拼音混输、句尾截断、省略主语或宾语时：

1. 首先结合最近几轮真实对话推断用户最可能的完整意图。
2. 输入形式不完整不代表意图不可理解。
3. 如果存在一个明显占优、低风险且可逆的解释，应直接按照该解释回答。
4. 在使用推断时，可以在回答结尾用一句简短说明指出当前采用的理解，并允许用户纠正。
5. 不要因为轻微拼写错误、语句截断或省略表达，自动要求用户重新输入。
6. 只有在以下情况下才应请求澄清：
   - 存在两个或更多同等合理的解释；
   - 不同解释会产生显著不同的答案；
   - 缺失信息是完成任务不可替代的必要参数；
   - 涉及删除、写入、支付、资金、权限、安全、对外发送或其他高风险副作用；
   - 采用错误解释会造成难以撤销的结果。
7. 能够先提供安全且有帮助的内容时，不要只返回澄清问题。
8. 当意图高度明确时，不要列举多个低概率选项让用户重新选择。
```

该 Prompt 规则应位于对模型行为有稳定约束力的位置，而不是仅作为普通数据注入。

---

# 七、引入结构化 AskUser Action

当前 Work Loop 如果只有：

```text
有 tool_calls → 执行工具
无 tool_calls + 文本非空 → final
```

则协议过于粗糙。

需要至少支持：

```ts
type AgentAction =
  | FinalAction
  | ToolCallAction
  | AskUserAction;
```

参考定义：

```ts
interface FinalAction {
  type: "final";
  content: string;

  intentResolution?: {
    interpretedIntent: string;
    confidence: number;
    ambiguity: "low" | "medium" | "high";
    risk: "low" | "medium" | "high";
    usedAssumption: boolean;
  };
}

interface ToolCallAction {
  type: "tool_call";
  calls: ToolCall[];
}

interface AskUserAction {
  type: "ask_user";
  question: string;
  reason: string;

  intentResolution: {
    candidates: Array<{
      intent: string;
      confidence: number;
    }>;
    ambiguity: "medium" | "high";
    risk: "low" | "medium" | "high";
    missingInformation?: string[];
  };
}
```

根据当前模型 Provider 能力，选择以下兼容方案之一：

```text
原生 Structured Output
原生 Tool Call 模拟 ask_user
JSON 响应协议
Provider Adapter 转换
```

Runtime 主循环不应依赖某一个模型厂商的特有格式。

---

# 八、区分 Final 和 AskUser

以下内容不能再被视为正常 Final：

```text
“请补充完整”
“你是想问 A、B 还是 C？”
“请提供更多信息后我再回答”
```

如果回答的核心目的是等待用户补充，Action 应当是：

```text
ask_user
```

而不是：

```text
final
```

推荐 Work Loop：

```text
模型返回
  ↓
Provider Adapter
  ↓
解析 AgentAction
  ├─ tool_call
  │    → 进入 Tool Gateway
  │
  ├─ ask_user
  │    → 进入 Clarification Gate
  │
  └─ final
       → 进入 Completion Gate
```

如果模型声称是 `final`，但结构化元数据表示：

```json
{
  "answeredUserIntent": false,
  "requiresUserResponse": true
}
```

则应拒绝将其作为 Final，转换为 AskUser 或要求模型修正协议。

不要通过自然语言关键词猜测，优先使用结构化字段。

---

# 九、增加 Clarification Gate

模型提出 AskUser 后，不能立即无条件暂停 Run。

需要经过 Clarification Gate。

Clarification Gate 判断：

```text
最近对话是否提供了明确指代对象？
是否存在明显占优的意图？
是否只有轻微错别字或句尾截断？
是否可以使用低风险假设继续？
错误理解是否会产生副作用？
缺失信息是否真的阻塞任务？
是否能够先提供部分答案？
```

建议决策结果：

```ts
type ClarificationDecision =
  | {
      type: "allow_ask_user";
      reason: string;
    }
  | {
      type: "answer_with_assumption";
      interpretedIntent: string;
      reason: string;
    }
  | {
      type: "continue_with_partial_answer";
      knownInformation: string[];
      missingInformation: string[];
    };
```

参考策略：

```text
高置信度 + 低歧义 + 低风险
  → 不允许阻塞
  → 按最可能意图直接回答

中等置信度 + 低风险
  → 先按最可能意图回答
  → 简短说明所采用的理解

高歧义
  → 允许 AskUser

高风险或不可逆操作
  → 即使存在较高概率解释，也优先确认

缺失关键执行参数
  → 允许 AskUser
```

对于本次场景，应该判定：

```text
输入不完整：是
与上一轮直接承接：是
最可能省略对象：时间戳
其他候选意图概率：低
风险：低
是否必须澄清：否
策略：answer_with_assumption
```

---

# 十、Clarification Gate 驳回时的处理

当模型提出了不必要的 AskUser，Runtime 不要自己拼接最终业务答案。

应向模型返回结构化 Runtime Observation，再进行一次有限纠正调用。

例如：

```json
{
  "code": "CLARIFICATION_NOT_REQUIRED",
  "message": "Recent conversation provides a dominant low-risk interpretation. Answer the most likely intent directly and briefly state the assumption.",
  "interpreted_intent": "用户想问平台如何向模型提供当前时间戳",
  "risk": "low"
}
```

然后模型重新生成：

```text
final
```

限制：

1. Clarification 修正重试必须有最大次数。
2. 建议最多重试 1 次或 2 次。
3. 不允许形成 AskUser → 驳回 → AskUser 的无限循环。
4. 超过阈值后，根据当前信息生成尽可能有帮助的回答，或进入明确失败状态。
5. 记录原始 AskUser、Gate 决策和修正结果，便于后续评测。

---

# 十一、是否需要单独的意图识别调用

不要默认让所有请求都增加一次模型调用。

推荐优先实现：

```text
单次主模型调用
+
结构化 intentResolution
+
Clarification Gate
```

如果效果仍不稳定，再增加轻量意图解析阶段。

轻量意图解析只对疑似模糊输入触发，例如：

```text
句尾明显截断
输入极短且包含代词
拼音和中文混输
存在未闭合表达
使用“这个、那个、刚才、第二种”等强上下文指代
与上一轮语义高度关联
```

流程：

```text
正常输入
  → 直接进入主模型

疑似残缺或上下文省略输入
  → 轻量 Intent Resolver
  → Clarification Policy
  → 主模型
```

不要一开始就把两次模型调用作为所有请求的固定成本。

---

# 十二、Completion Gate 增加低成本检查

模型返回 Final 时，进行轻量 Completion Gate。

检查：

```text
是否实际回答了当前解释出的用户意图？
是否只有要求用户重新输入？
是否明知存在明显解释却完全没有提供答案？
是否将一个需要用户回复的动作错误标成 final？
是否满足当前输出格式要求？
```

Completion Gate 不应依靠大量关键词规则。

优先要求模型 Action 带有：

```ts
completion: {
  answeredUserIntent: boolean;
  requiresUserResponse: boolean;
}
```

如果出现：

```text
answeredUserIntent = false
requiresUserResponse = true
```

则不能完成 Run。

对于低风险且存在明显意图的场景，可以向模型返回：

```text
FINAL_DID_NOT_ANSWER_RESOLVED_INTENT
```

并允许一次修正。

---

# 十三、Work Loop 推荐流程

改造后的相关流程应接近：

```text
接收用户输入
  ↓
保留原始输入
  ↓
读取标准对话历史
  ↓
构造模型消息
  ├─ System Prompt
  ├─ 标准 user/assistant 历史
  ├─ 当前 user 消息
  ├─ Working Context
  ├─ Memory
  ├─ Tool Observations
  └─ External Untrusted Data
  ↓
调用模型
  ↓
解析统一 AgentAction
  ├─ tool_call
  │    ↓
  │  执行工具
  │    ↓
  │  Observation 加入上下文
  │    ↓
  │  再次调用模型
  │
  ├─ ask_user
  │    ↓
  │  Clarification Gate
  │    ├─ 必须澄清
  │    │    → WAITING_USER_INPUT
  │    │
  │    └─ 无需澄清
  │         → 返回 CLARIFICATION_NOT_REQUIRED
  │         → 模型修正为直接回答
  │
  └─ final
       ↓
     Completion Gate
       ├─ 通过 → COMPLETED
       └─ 未回答已解析意图
            → 返回结构化反馈
            → 有限次数修正
```

---

# 十四、状态和持久化

如果项目已经有 Run 状态机，AskUser 应对应明确状态：

```text
RUNNING
  ↓
WAITING_USER_INPUT
  ↓
用户回复
  ↓
RUNNING
```

至少持久化：

```text
原始用户输入
模型请求上下文摘要
模型原始响应
解析后的 AgentAction
IntentResolution
ClarificationDecision
是否使用假设
AskUser 请求
用户回复
修正重试次数
最终回答
```

不要将模型完整 Prompt 中的敏感信息直接写普通日志。

可以保存：

```text
消息角色
消息来源
Token 数量
内容哈希
安全摘要
必要的脱敏内容
```

---

# 十五、测试要求

本次必须增加针对上下文连续性和澄清策略的测试。

## 1. 当前问题回归测试

输入：

```text
user: 帮我检索一下今天的日期
assistant: 根据平台提供的时间戳，今天是……
user: 你平台是怎么提供de
```

预期：

```text
不进入 WAITING_USER_INPUT
不只返回澄清选项
回答平台如何提供时间戳
可以简短声明采用了该理解
```

## 2. 低风险高置信省略

```text
user: 第一种方案使用 PostgreSQL，第二种使用 SQLite
assistant: ...
user: 第二种呢
```

预期：

```text
理解为询问 SQLite 方案
直接回答
```

## 3. 代词承接

```text
user: Docker 和 containerd 有什么区别？
assistant: ...
user: 那个更适合 Mac
```

预期：

```text
结合上下文推断比较对象
直接回答
```

## 4. 轻微错别字

```text
user: kubernetes 怎么重启 depoly
```

预期：

```text
理解为 deployment
直接回答
```

## 5. 真正高歧义

```text
user: 帮我处理一下那个
```

且上下文中存在多个同等合理对象。

预期：

```text
ask_user
WAITING_USER_INPUT
```

## 6. 高风险操作

```text
user: 把刚才那个删了
```

上下文中可能指数据库、文件或 Run。

预期：

```text
必须确认具体对象
不得直接执行
```

## 7. 中等置信但低风险

```text
user: 刚才第二个怎么配
```

存在一个较明显候选，但仍有轻微不确定。

预期：

```text
先按最可能对象回答
简短说明当前理解
不无条件阻塞
```

## 8. AskUser Gate 驳回

模拟模型输出不必要的：

```json
{
  "type": "ask_user",
  "question": "你想问什么？"
}
```

预期：

```text
Clarification Gate 返回 CLARIFICATION_NOT_REQUIRED
模型得到一次修正机会
最终输出 direct answer
```

## 9. 防止无限修正

模型连续返回 AskUser。

预期：

```text
达到最大 clarification retry
不会无限循环
产生明确事件和终止行为
```

## 10. 消息结构测试

验证最终模型 messages：

```text
真实历史保留 user/assistant role
当前用户输入不重复注入
外部数据仍在 untrusted_data 中
工具 Observation 与真实对话分离
```

---

# 十六、建立意图连续性评测集

除单元测试外，建立一组可复用评测样例。

至少包含：

```text
这个怎么实现
那为什么不行
第二种呢
刚才说的版本是什么
这个能改吗
就按之前那个做
不是这个，是另一个
你平台是怎么提供de
那如果换成 Go 呢
继续
有用吗
嗯
```

每个样例标注：

```ts
interface ConversationContinuityCase {
  conversation: Message[];
  expectedIntent: string;
  shouldAskUser: boolean;
  shouldStateAssumption: boolean;
  risk: "low" | "medium" | "high";
  acceptedAnswerRequirements: string[];
}
```

在当前使用的 DeepSeek 模型上运行，同时保留 Provider 可替换性。

记录：

```text
直接回答率
错误追问率
错误意图率
高风险误执行率
平均额外模型调用次数
Token 增量
延迟增量
```

目标不是让 Agent 永远不追问，而是降低不必要追问，同时不增加高风险误执行。

---

# 十七、实施顺序

按以下顺序实施，不要一次性大改。

## 第一阶段：上下文连续性修正

完成：

```text
真实对话历史改为标准 role 消息
外部数据继续作为 untrusted_data
当前输入去重
增加消息构造测试
```

完成后运行相关测试，并实际复现当前案例。

## 第二阶段：Prompt 策略

完成：

```text
增加残缺输入处理规则
增加低风险高置信时优先回答规则
增加高风险时必须确认规则
```

再次测试当前案例。

## 第三阶段：AskUser Action

完成：

```text
统一 AgentAction
final
tool_call
ask_user
Provider Adapter
Action Dispatcher
WAITING_USER_INPUT
```

保持现有 Tool Call 行为兼容。

## 第四阶段：Clarification Gate

完成：

```text
ClarificationDecision
高置信低风险判断
不必要 AskUser 驳回
CLARIFICATION_NOT_REQUIRED Observation
有限纠正重试
防无限循环
```

## 第五阶段：Completion Gate 和评测集

完成：

```text
Final 是否真正回答意图
伪 Final 检测
上下文连续性评测集
Provider 回归测试
指标统计
```

每个阶段结束后：

1. 运行格式化。
2. 运行静态检查。
3. 运行单元测试。
4. 运行集成测试。
5. 汇报真实结果。
6. 不得虚构通过数量。
7. 不得声称尚未实现的能力已经完成。

---

# 十八、禁止事项

本次禁止：

1. 不审计实际消息结构就直接改 Prompt。
2. 只通过增加一句 Prompt 声称问题已解决。
3. 把所有残缺输入都强制直接回答。
4. 把所有残缺输入都强制 AskUser。
5. 使用“请补充”等关键词识别 AskUser。
6. 将真实对话历史和网页工具数据混为同一种结构。
7. 重复注入当前用户输入。
8. 在 Runtime 中硬编码本案例的“时间戳”答案。
9. 让 Runtime 自己编造业务答案。
10. 对写入、删除、支付等高风险操作使用模糊猜测直接执行。
11. 让 Clarification 修正形成无限模型循环。
12. 为所有请求固定增加一次意图模型调用。
13. 为了处理该问题重写整个 Agent Runtime。
14. 只增加类型定义而不接入真实 Work Loop。
15. 修改后不增加回归测试。

---

# 十九、最终交付

最终输出必须包含：

## 1. 审计结果

说明：

```text
真实模型消息结构
历史消息当前如何传递
当前输入是否重复
Work Loop 如何判断 final
是否已有 ask_user
本问题的真实根因
```

附关键文件和代码位置。

## 2. 实际修改

说明：

```text
修改了哪些文件
消息结构如何调整
Prompt 增加了什么规则
AgentAction 如何扩展
AskUser 如何进入 WAITING_USER_INPUT
Clarification Gate 如何决策
修正重试如何限制
Completion Gate 如何处理伪 Final
```

## 3. 修改后的执行链路

给出实际实现对应的流程图。

## 4. 测试结果

至少给出：

```text
当前案例测试结果
上下文消息结构测试
AskUser 测试
Clarification Gate 测试
高风险澄清测试
无限循环保护测试
单元测试通过/失败数量
集成测试通过/失败数量
格式化和静态检查结果
```

不得虚构。

## 5. 实际运行示例

使用以下对话真实验证：

```text
用户：帮我检索一下今天的日期
Agent：根据平台提供的时间戳……
用户：你平台是怎么提供de
```

目标输出应接近：

```text
平台会由服务端在创建或执行本轮 Run 时读取当前时间，并将时间信息作为系统上下文或运行时元数据传给模型。模型通常不能自行读取系统时钟，而是使用平台注入的时间。

我是按你在问“平台怎么提供时间戳”来理解的；如果你指的是其他内容，可以继续补充。
```

不要求逐字一致，但必须满足：

```text
实际回答了最可能意图
没有只列举候选问题
没有要求用户重新输入完整句子
没有进入不必要的 WAITING_USER_INPUT
说明了采用的上下文理解
```

## 6. 未完成事项

明确列出：

```text
当前只实现了什么
哪些能力仍是扩展点
是否尚未加入独立 Intent Resolver
不同 Provider 是否已验证
还存在哪些技术债
```

---

# 二十、最终目标

改造后的原则应当是：

```text
输入不完整
≠
意图不可理解
≠
必须澄清
```

最终形成以下能力边界：

```text
Conversation Continuity
├── 标准 role 历史消息
├── 当前输入去重
└── 外部数据隔离

Intent Resolution
├── 结合最近对话补全省略
├── 识别明显占优意图
├── 风险判断
└── 假设声明

Agent Action
├── final
├── tool_call
└── ask_user

Clarification Policy
├── 高置信低风险直接回答
├── 中等置信低风险带假设回答
├── 高歧义请求澄清
└── 高风险强制确认

Work Loop
├── Action Dispatcher
├── Clarification Gate
├── 有限纠正重试
├── WAITING_USER_INPUT
└── Completion Gate
```

最关键的设计原则是：

> 模型可以建议澄清，但是否真的需要阻塞用户，应由 Runtime 根据对话连续性、意图置信度、歧义程度和操作风险共同决定。

请先完成代码审计，确认真实执行链路和根因，然后直接实施第一阶段，不要只停留在设计建议。
