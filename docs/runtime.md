# Runtime Provider 与持久化 Worker

## 稳定边界

`AgentRuntimeProvider` v1 与 `AgentRuntimeProviderV2` 是执行后端共同遵循的异步协议。v2 用 `execute` 返回 terminal 或 suspended outcome，并只接收显式的 Tool、Coordination、Artifact、UserInput capability handler；Provider 不接收 ORM、数据库 Session 或控制面服务。v1 Provider 由兼容包装层转换为 terminal outcome，历史版本无需数据回写。

能力通过 `RuntimeProviderDescriptor.capabilities` 明示，`implementation` 区分 native、adapter 与 test，`compatibility` 声明可恢复的 Provider/协议版本和执行模式。缺少能力必须抛出稳定的 `RUNTIME_CAPABILITY_UNSUPPORTED`，不能返回伪成功。事件使用单调 `sequence` 和平台枚举；应用服务检查连续性、幂等忽略已提交事件，再映射为 RunStep/Event/Audit。

## Run lifecycle authority

Runtime 控制的 Run 状态变化统一调用 `runtime/lifecycle.py`。它接收调用方的
AsyncSession、TenantContext 和已锁定 Run，在同一事务验证 source/target、revision
与可选租约 owner/token，然后原子保存 coarse status、reason、脱敏 metadata、
revision、Event、Audit 和领取语义。它不创建、提交或回滚事务。

数据库中的 `claim_next_run()` 保留 0026 的 Conversation queue head、priority 和
异常 head pause 规则。`reconcile_expired_tool_approvals()` 与
`reconcile_coordination_waiters()`、`reconcile_user_input_requests()` 是具名 SQL lifecycle 端口；它们调用与 Python
相同版本的转换/claimability 合同，wake 只在预期等待状态仍成立时成功。SQL 端口
只授予最小权限 claimer role。Runtime Provider v2、冻结 execution manifest、
Tool Gateway 和 PostgreSQL lease 继续保持原有权威边界。

迁移 0027 是 expand-only：旧 Run 自动得到空 lifecycle metadata 和 revision 0，
旧应用可忽略新列。代码 rollback 保留已经写入的 lifecycle reason/Event/Audit；
生产环境使用 forward-fix，不通过 destructive schema downgrade 删除这些事实。
迁移 0031 在该兼容状态之上增加独立 `UserInputRequest` 聚合、Native handler 和
answer/wake/reconcile 后端；它仍不是通用 external wait。CLI/API 交互和 live
`ask_user` Schema 由后续阶段启用。

## Provider 选择

新建 AgentVersion 的正式默认值是 `runtime_provider=nico_native` 和 `execution_mode=direct`。为兼容历史数据，旧版本仍依次读取 `run_config.runtime_provider`、`model_config.runtime_provider`，均缺省时解析为 `mock`；显式选择不会被静默替换。每次解析把 source、legacy 标记和兼容元数据写入 RuntimeSession；legacy 分支追加 `LegacyRuntimeProviderResolved` Event/Audit。生产 Worker 不注册 Mock，避免测试执行器被误用为真实推理。

恢复时，已经持久化的 `RuntimeSession.provider_name/provider_version/protocol_version` 是权威事实。Worker 必须查找同名 Provider，并按 descriptor 的 resume compatibility 校验；缺失、禁用或不兼容会失败关闭，不能换成 Native。历史 RuntimeSession 的 Provider/版本字段不会被 migration 或恢复逻辑重写。

### Provider capability matrix

| Provider | 协议/实现 | Direct | Resume/Cancel/Status | Platform Tool | Planning/Reflection | Coordination/Artifact | Usage |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `nico_native 0.2.0` | v2 / native | 是 | 是 | Tool Gateway | 是 | 是 | ModelCall exact/partial 状态 |
| `mock 1.0` | v1 terminal shim / test | 确定性测试 | 是 | Tool Gateway | 测试合同 | 测试合同 | 确定性测试值 |
| `hermes 0.18.2` | v2 / subprocess adapter | 是 | 是；不声明运行中 pause | 仅 Nico MCP → Tool Gateway | 否 | 否 | CLI 0.18.2 标记为 unavailable，绝不伪造 |

## Nico Native Direct、ReAct 与 Plan-and-Execute

`nico_native` 是 Nico 自带、无需 Hermes 的 Runtime。Direct 模式构造一个确定性 ContextSnapshot，调用一次 OpenAI-compatible 流式模型接口，合并有界输出增量，保存 ModelCall/usage/cost/checkpoint，并以结果或稳定错误终结 Run。

ReAct 模式执行有界的“模型推理 -> Tool Gateway -> 工具观察 -> 下一轮推理”循环。模型只能调用冻结策略中授权的精确 `name@version` 工具；每轮受 `max_iterations`、`max_tool_calls` 和 token budget 限制。模型协议错误、无权限工具、空最终输出和预算耗尽都会以稳定错误失败关闭。Direct 模式仍不执行 Tool Call，收到此类输出会以 `MODE_CAPABILITY_VIOLATION` 失败。

RuntimeSession 的 execution manifest 会冻结 Run 首次开始的 UTC 时间，并把它作为平台
上下文加入 Native 各阶段 Context。普通日期或时间问题默认直接换算这个时间戳，避免
不必要的网络往返；当用户明确要求独立核验、当前外部来源或比 Run 开始时刻更高的精度
时，仍可按正常授权调用工具。ReAct 指令同时要求使用完成任务所需的最少工具：简单事实
通常在一个相关来源成功后终结，不因已有证据之外的单个来源失败重复等价搜索。普通
Conversation 默认再以 12 轮、8 次工具调用约束该行为；显式任务预算仍可覆盖。

### Conversation 上下文消息合同

新领取的 Conversation Run 冻结 schema v2 Conversation context，并把最近已完成
Turn 投影为有界、按 sequence 排序的标准 `user` / `assistant` 消息。每条投影同时
保存 `source_ref`、`source_kind=conversation_turn`、`trust=untrusted_data`、
内容 Hash 和截断标记；消息 role 只恢复对话结构，不提升内容信任级别，也不能修改
Tool grant、审批模式、风险、预算或平台指令。

Native 的渲染顺序是：系统约束、非 Conversation 数据 envelope、历史 Conversation
消息、当前 `user` 消息，最后才是当前 Run 已产生的 assistant/tool trajectory。
Conversation summary、Memory/Skill、Tool observation、Artifact reference 和外部数据
继续留在带来源标签的 untrusted envelope 中，不冒充历史对话消息。当前
`Task.input.message` 只在最后一个 conversational `user` 位置出现；task envelope
保留 Conversation/Turn/sequence 等审计元数据，但删除重复的完整文本。

该合同使用 ContextSeed schema v3 和 ContextSnapshot schema v2。恢复时
RuntimeSession 中冻结的 Conversation selection 是权威输入：v2 selection
确定性重建相同角色消息；已存在的 schema v1 selection 继续使用旧扁平渲染，以免
已持久化 snapshot 发生内容 Hash 漂移。ConversationTurn 的原始 user input 和
assistant output 均不因投影、归一化或截断而改写。

### 残缺输入与上下文承接指导

所有 Native system context（Direct、ReAct、Planner、Plan Step、Reflection 和修复
轮次）共享同一份版本化 continuity policy。该策略要求模型先使用最近 Conversation
处理错别字、混合语言、截断、省略主语/宾语和上下文指代；表达形式不完整本身不等于
意图歧义。若上下文给出一个占优、低风险且可逆的解释，模型应直接回答，必要时简短
说明假设，不展开冗长的低概率选项。

只有多个解释仍同样合理、答案会实质不同或缺少不可替代信息时，指导才允许提出一个
阻塞问题；如果安全部分可以先回答，应先提供该部分。删除、写入、付款、资金、权限、
安全或外部发送等副作用在目标或意图不明确时必须先获得明确确认。该 Prompt 不授予
权限、不改变风险或预算，Runtime 与 Tool Gateway 仍是权威边界。

非 Tool 的 live Action Schema 按实际 capability 构造：`final` 始终启用；
`ask_user` 只有在 Clarification Gate 已启用且本次执行注入了 UserInput handler
时才出现。端点支持 structured output 时通过同一个 `response_format` 发送，否则
system policy 要求等价 JSON；没有 UserInput handler 时，结构化 `ask_user` 仍以
`ACTION_KIND_UNSUPPORTED` fail closed。Native Direct、ReAct 和 Plan Step 不再把
legacy plain-text final 当作可完成结果：它只会成为一个 compatibility candidate，
经过一次结构化纠错后，若仍未返回有效 Action envelope，就稳定失败。

### Clarification Gate

Native 使用版本化 `clarification-v1` 策略评估结构化 `AskUserAction.intent`，不搜索
自然语言中的“确认”“是否”等词。策略只读取候选置信度/差值、ambiguity、missing
information、safe partial 标记、冻结的权威风险和当前 obligations。低风险候选的
默认占优阈值为 `confidence >= 0.75` 且与次选差值 `>= 0.25`，ambiguity 不高于
`0.5`；多个接近候选、不可缺少信息、非低风险效果或未履行 obligation 可允许阻塞。

Gate 的结果是 `allow_ask_user`、`answer_with_assumption` 或
`continue_with_partial_answer`。允许时复用唯一 UserInputRequest/Action cursor
进入等待；回答恢复为内部 `trust=untrusted` Observation 后再交给模型。拒绝时，
Runtime 把源 AskUserAction 标记为 blocked，持久化 policy reason 与源 batch 关系，
并增加且只增加一次无 Tool 权限的 correction ModelCall。correction 只收到模型自己
声明的 dominant interpreted intent 和确定性 policy observation；Runtime 不拼接
业务答案。模型再次提出仍被拒绝的问题时以
`CLARIFICATION_CORRECTION_EXHAUSTED` 稳定终止，不产生第三次调用。

Direct 与 ReAct 共享这套 Gate、修正关系和 UserInput transport。ReAct 的 schema-v2
checkpoint 额外保存是否已经修正、源 batch Hash 和有界 observation；在拒绝落库后
崩溃会恢复同一个具名 correction call，在用户问题挂起后崩溃会恢复同一个源 Action，
均不重复模型计费或创建问题。Plan Step 保留 phase-specific output 和原有 acceptance
检查，同时也必须先通过下述共享 Completion Gate。

### Semantic Completion Gate

Native Direct、ReAct 和 Plan Step 的每个 `FinalAction` 候选都先持久化，再由版本化
`semantic-completion-v1` 策略检查 `answered_user_intent`、
`requires_user_response`、compatibility mode 和当前未决 UserInputRequest 数量。
未回答意图、仍需用户输入、互相矛盾的 completion metadata、旧式纯文本或未决用户
输入都会阻止 Run 完成。策略只返回结构化 verdict 和 correction observation，不根据
答案正文猜测“是否完成”，也不替模型编写业务答案。

第一次拒绝会把候选 Action 标记为 blocked，并持久化一条
`semantic_final_correction` 源关系；纠错调用没有 Tool 权限。纠错可以返回通过 Gate
的 FinalAction，或返回 AskUserAction 进入正常 Clarification Gate。第二个无效 Final
以 `SEMANTIC_FINAL_CORRECTION_EXHAUSTED` 稳定终止。具名纠错 ModelCall 和已提交
Action 批次可以在崩溃后直接恢复，不重复 Provider 调用或 usage。

Action envelope 的流式文本统一标记为 `visibility=internal`，因此 metadata、兼容输入
和被拒绝的伪答案都不是用户可见的权威结果；只有通过 Gate 且 dispatcher 成功完成的
FinalAction content 才通过 `RUN_COMPLETED` 发布。Plan 随后仍执行原有 required field、
JSON Schema、确定性 acceptance 和可选独立 judge。

这是 parent RH-H 的共享扩展点，目前只实现“已回答意图/是否仍需用户回复/未决用户
输入”的语义底线；Tool、approval、Artifact、预算和其他数据库 obligation 仍由
parent RH-H 后续单元负责，不能把本节视为完整 obligation-aware Completion Gate。

### Conversation continuity evaluation

`backend/evals/conversation_continuity_cases.json` 是版本化、provider-neutral 的
AE1–AE11 数据集，覆盖时间戳承接、低风险省略、代词、错别字、真实歧义、高风险删除、
低风险中等置信度、Clarification correction、无限循环保护、来源隔离和 provider
可移植性。同一数据集既驱动确定性 fake provider，也可通过环境配置驱动任意
OpenAI-compatible Native endpoint；Runtime policy 不包含 DeepSeek 或其他模型的
专用分支。

评测从 Runtime 的结构化 Action batch、等待/终态、ModelCall 数量和 usage、Tool
完成事件及计时生成 direct-answer、unnecessary-clarification、wrong-intent、
unsafe-high-risk-action、extra-call、Token 和 latency 指标。每个 rate 都保留
numerator/denominator；timeout、失败、partial usage、missing usage 和 skipped case
不会静默消失。逐例输出只包含 case ID、检查结果和聚合事实，不保存 Conversation
正文、问题、答案、Prompt 或凭据。

hermetic 报告同时运行一个明确标记为 `baseline-v0` 的确定性反事实：它接受脚本的
首个 Action，不执行 Clarification/Completion correction。该基线用于比较策略开销
与行为差异，不代表历史真实模型质量或线上测量。真实 endpoint 使用
`NICO_CONTINUITY_EVAL_BASE_URL`、`NICO_CONTINUITY_EVAL_MODEL`、
`NICO_CONTINUITY_EVAL_CREDENTIAL_REF` 和可选
`NICO_CONTINUITY_EVAL_PROVIDER_LABEL`；缺少任一配置或引用的环境 Secret 时，报告
11 个 `skipped` case、`credential_status=unavailable` 和
`verification_status=unverified`，绝不生成伪造的 credentialed pass。

当前 hermetic AE1–AE11 全部通过，说明协议、Gate、恢复和指标路径闭合。由于没有外部
凭据，现有证据不足以证明需要额外 Intent Resolver；暂不增加该组件。后续只有在
credentialed wrong-intent 或 unnecessary-clarification 指标显示可复现缺口时，才应
建立独立 follow-up plan。

### AgentAction 持久化边界

Native Direct、ReAct、Plan Step 和引用修复的可执行模型响应会在任何
final/Tool/Artifact/Delegation 副作用之前，归一化为完整的 `final`、`tool_call`
或已识别但尚未启用的 `ask_user` AgentAction 批次并事务落库。批次绑定同一个 Tenant/Run 的
RuntimeSession、RunStep、ContextSnapshot 和已终态 ModelCall；多 Tool 响应要么提交
全部连续 ordinal，要么一条也不提交。统一 dispatcher 只读取已提交批次；原有 Tool
Gateway、Artifact Service 和 Coordination Service 继续拥有各自领域结果，dispatcher
不复制或取代这些权威服务。

每个 ordinal 在调用领域处理器前先从 `pending` 提交为 `dispatched`。领域结果提交后，
Action 才成为 `succeeded`/`failed`/`blocked`，批次 cursor 才能向前；最终 `final`
同样通过该状态机后才可完成 Run。恢复会跳过 cursor 之前已成功的 Action，并从第一个
未决 ordinal 开始。若接管时发现 `dispatched` 而没有权威终态，结果按 unknown
fail-closed，后续 ordinal 不执行；工具审批在副作用前挂起属于明确例外，dispatcher
把该 ordinal 安全释放回 `pending`，批准后的 Worker 可继续原有调用。

ModelCall 继续独占脱敏原始响应。AgentAction 表只保存确定性 Action/批次 Hash、
Provider call ID、工具名、完成元数据、去文本化的 Intent 风险/置信度投影，以及
content/arguments/question/reason Hash；不复制 Prompt、答案、Tool 参数、凭据或用户
受保护回复。`GET /api/v1/runs/{run_id}/agent-actions` 只返回这一安全投影和有界修复
关系，受 TenantContext 与 PostgreSQL `FORCE ROW LEVEL SECURITY` 双重约束。

若 Worker 在 ModelCall 终态提交后、Action 批次提交前丢失租约，接管者会核对原请求
Hash，从已提交响应重建同一批次并追加一次 `post_model_call_commit` 修复关系。若批次
已经存在但 checkpoint 尚未提交，接管者同样复用原 ModelCall 响应重建内存 DTO，再按
持久化 cursor 恢复分发。两条路径都不再次调用 Provider、不新增 ModelCall，也不重复
usage/cost；不匹配的恢复请求会 fail closed。checkpoint 记录最近批次 Hash，数据库
trigger 禁止 dispatch cursor 后退。

ReAct 在工具外部作用前保存带完整性 Hash 的 schema v2 checkpoint，工具成功后先提交 ToolCall/RunStep/Event/Audit，再保存观察 checkpoint。Worker 接管过期租约时会把未终结 ModelCall 标为 `interrupted`，使用新的 replay call key 重试；ToolCall 使用稳定 idempotency key 查询权威结果，已成功的文件、Python 或其他副作用不会再次执行。每个执行尝试使用独立的进程内 session id，旧租约持有者的迟到 cancel/release 不会误伤接管者。

Web 工具沿用同一恢复协议。`web.search` 的成功 ToolCall ID 会进入模型观察，模型只有
把这个平台 ID 传给 `web.fetch` 才能读取对应来源；checkpoint/Worker 接管不会改变
该关联。Native 累积成功 Search/Fetch 观察中的规范化 URL，并在终结前执行确定性
引用校验。引用缺失时最多增加一次不带工具权限的修复 ModelCall。Hermes 自带的 Web
能力保持关闭，只能经 Nico MCP 调用相同工具，因此没有第二套网络或授权路径。

medium/high ToolCall 在副作用前创建 ToolApprovalRequest，并让 Native ReAct/Plan 返回 suspended outcome。Worker 保存同一 checkpoint、清除租约并进入 `waiting_for_approval`。approved/rejected/expired 决定把 Run 置回可领取状态；恢复时 approved 调用继续执行，其他终态作为失败工具观察返回给模型。决策可能早于 Worker 最后一笔 suspension 写入，因此挂起逻辑会重读持久化审批状态，避免丢失快速唤醒。审批行锁不参与 Run 锁顺序，防止操作者决策与挂起事务死锁。

### UserInputRequest 后端

显式允许的 `AskUserAction` 先进入 `dispatched`，再由 Native UserInput handler 在
同一事务锁定 Run、保存 pre-action checkpoint、创建唯一 `UserInputRequest`，并把
Run 置为 `waiting_for_user_input`。请求绑定 Tenant、Run、RuntimeSession、Action
batch 和单一 Action，保存有界 question/reason/JSON Schema、request Hash、到期时间、
revision 与稳定 wake key。第一次等待转换暂时保留握手租约；Provider 返回 suspended
outcome 后，Worker 保存 trajectory/usage 并释放租约。相同 Action 的创建重试返回
原请求，不产生第二个等待事实。

答案事务始终先锁 Run，再锁请求和 Action 边界。答案必须通过请求的 Draft 2020-12
JSON Schema、expected revision 和幂等键校验；成功后只提交一次受保护
`answer_payload`、answer Hash/reference，把同一 Action 标为 succeeded、推进 batch
cursor，并把 Run 唤醒为可领取的 `running`。恢复读取 answer 时才构造
`trust=untrusted` 的内部 Observation；普通 Event、Audit、Action 投影和
`UserInputRequestRead` 都不包含答案正文。

到期请求由最小权限 reconciler 标为 expired，并把未决 Action/batch 终态化后唤醒
Run；answered 但唤醒事务未完成的崩溃窗口也由同一端口幂等修复。Run 先进入终态时，
数据库 trigger 取消仍为 requested 的请求并阻止迟到答案复活 Run。用户输入挂起与
工具审批不同：请求本身就是权威副作用，因此 dispatcher 保留 Action 的
`dispatched` 状态，答案事务负责完成它；恢复不会重复创建问题。

UserInput 现在公开独立的 tenant-scoped list/get/answer 资源。answer 必须携带
expected revision、`Idempotency-Key` 并通过请求自己的 JSON Schema；终态、过期、
跨租户和格式错误使用稳定错误码，公开响应仍不含答案正文。CLI 在启动或 SSE 收到
`UserInputRequested` 时读取同一资源，把 Agent question 作为独立 interrupt 恢复；
回答直接写 UserInput answer endpoint，不创建 ConversationTurn，也不会被 Tool
approval prompt 消费。问题、审批、排队 Turn 和普通 composer 采用确定性单一输入
owner，当前草稿和光标在 interrupt 前后保持。

`ask_user` 已由 Clarification Gate 与 UserInput handler 的联合 capability gate
启用；API/CLI 仍只消费持久化 UserInputRequest，不从普通对话文本猜测问题。

Plan-and-Execute 使用结构化 Planner 生成最多由预算约束的 DAG，每次初始规划或 Replan 都追加独立 Plan revision，旧 revision 不覆盖。每个 PlanStep 产生独立 ModelCall、RunStep 与确定性 step validation；步骤可以调用冻结策略授权的精确版本工具，并复用同一个 Tool Gateway、权限交集、幂等键和审计链。工具副作用前强制保存 schema v3 checkpoint，工具 RunStep 挂到对应 Plan 执行步骤。失败后 Reflection 只能返回 `retry`、`replan` 或 `fail`。Replan 会终结旧 revision 并创建新 revision，不修改已经完成的步骤事实。

所有步骤结束后先按 Task acceptance（包括 `non_empty`、required fields 和可选 JSON Schema）执行确定性 Completion Evaluation。只有确定性检查通过，部署才可按 AgentVersion 的 `run_config.completion_model_judge=true` 启用独立模型 judge；judge 使用单独 ContextSnapshot、ModelCall、usage/cost 和 RuntimeEvaluation，生成结果的 ModelCall 不能给自己证明完成。最终 Run 结果同时包含 `result` 和 `execution_summary`。

Plan 模式使用紧凑、带完整性 Hash 的 schema v3 checkpoint，只保存当前 revision、步骤游标、完成键、当前步骤的有界工具状态、最新输出、恢复指令、Reflection 决策与 usage。完整 Plan 历史在 `plans`、`plan_steps` 和 `runtime_evaluations` 中；Worker 接管时从这些权威事实重建 active Plan，通过 replay call key 防止把已提交模型调用再次计费，并以稳定工具幂等键复用崩溃前已经产生的副作用。

## 动态多 Agent 协作

ReAct 在协调策略启用时获得保留工具 `delegate_agent`，可在同一模型轮次提出多个并行委托。每个请求都经 Coordination handler 校验目标版本、父子深度、数量/并行度、重复与循环，并以事务锁定 Parent 的 `RunBudgetLedger` 后预留 Child 预算。Child 的模型、工具、Secret 引用和继续委托能力只能取 Tenant、Parent 冻结快照、Child AgentVersion 与本次限制的交集。

委托成功后 Parent 保存包含 Child Run/Delegation 引用的 checkpoint，以 suspended outcome 进入 `waiting_for_subagent` 并释放 Worker 租约。Child 终结事务写入 result message、核销预算、附带可见 Artifact 引用，并在所有直接 Child 终结时唤醒 Parent。新 Worker 从 PostgreSQL 恢复消息并确认消费；通知丢失时由 reconciler 修复。取消 Parent 会幂等取消整棵 Run 树。

Child 可使用保留工具 `store_artifact` 把有界文本或 Base64 内容交给 Artifact handler。Provider 不接触 MinIO 凭据；服务完成临时上传、SHA-256 内容寻址、元数据提交与可选 direct-Parent share。Parent 恢复时只收到结构化结果和 `artifact:<id>` 引用。

模型端点按 Tenant 保存版本行，仅保存 `credential_ref`，不保存 Secret 值。Worker 发起请求时解析 `env:NICO_MODEL_SECRET_*`，默认强制 HTTPS，解析并固定连接到已校验 IP，拒绝 redirect、loopback、link-local、metadata 和私网地址。私网模型只能由部署级可信主机 allowlist 放开，AgentVersion 无权自行降低策略。

确定性 Mock 配置示例：

```json
{
  "runtime_provider": "mock",
  "mock": {
    "steps": ["plan", "execute"],
    "output": {"result": "ok"},
    "delay_seconds": 0,
    "fail": false
  }
}
```

Mock 的行为只由上述配置决定，支持暂停、恢复、取消、检查点、事件和轨迹，不能用于声明真实模型质量。

Mock 还支持用于确定性验收的 `mock.tool_calls`。每项必须给出精确工具名称、版本和参数；Mock 只生成规范化 tool intent，实际执行仍由 Worker 注入的 Tool Gateway handler 完成，不能直接调用 Executor。

## Hermes Adapter（可选）

Hermes 不是 Nico Native 的运行依赖。只有显式设置 `NICO_HERMES_ENABLED=true` 时 Worker 才注册 `HermesRuntimeProvider`；它直接返回协议 v2 terminal outcome，保留 `run()` 仅作为 v1 调用方的弃用 shim。Adapter 面向 Hermes `0.18.2`，采用 CLI 子进程而非 Python 内部类：

- 兼容：首次创建 session 前运行一次 `hermes version` 并缓存结果；非 0.18.2 或无法解析时 fail-closed；
- 执行：`hermes chat -q <envelope> -Q`，按配置映射 `--model`、`--provider` 和 `--resume`；有平台工具时只传 `--toolsets nico`；
- session：解析 stderr 的 `session_id:` 机器行，并在事件/RuntimeSession 中持久化；
- 轨迹：`hermes sessions export - --format jsonl --session-id <id> --redact`；
- 取消：新建进程组，先 TERM、超时后 KILL；
- Secret：剥离 `NICO_*`、数据库、Redis、MinIO、Docker 与外部 Hermes 配置环境；模型 Provider 凭据按管理员配置继承，MCP token 只写入 `0600` 的 Run 配置环境段，不进入参数、Prompt、事件或轨迹；
- pause：Hermes 0.18.2 不声明运行中 pause；历史 session resume 用于租约恢复，不冒充 pause/resume 对称能力。

每个 Run 使用独立 `HERMES_HOME/<tenant>/<run>`。配置将 platform CLI toolsets 限定为 `nico`，并禁用 terminal、web、browser、file、memory、skills 和 delegate；Nico MCP stdio 子进程再通过 `0600` Unix socket 和随机 token 回到当前 Worker。broker 每次 list/call 都复核 Run 租约和冻结权限。终态导出、取消、超时或启动失败会清理含 token 的 Run 目录。

默认 Compose Worker 镜像不包含 Hermes，也没有 Hermes 状态卷。可选
`hermes` profile 使用 `backend/Dockerfile.hermes` 构建固定
`hermes-agent[mcp]==0.18.2` 的独立 Worker。GitHub Release 安装器接受
`--runtime hermes`，release overlay 和 `nico-service` 会停止 Native Worker
并只激活 Hermes Profile；源码部署仍需在切换前手工停止默认 Worker，避免
不同 Provider 集合竞争同一队列。`goal-l` profile 只运行仓库内 fake CLI
合同，不是用户部署方式。

仓库已完成 Hermes 0.18.2 CLI/MCP 发现、协议 v2、禁用失败关闭、成功/失败/取消/resume、Secret 脱敏和 Compose contract E2E；真实模型推理仍未执行，不能据此声称模型质量、外部 Provider 或生产部署可用。

## Legacy Provider 弃用窗口

- `0.2.x`：继续读取旧 `run_config.runtime_provider`、`model_config.runtime_provider` 和全空时的 Mock 缺省；每次命中均记录 telemetry。
- `0.3.x`：继续执行但在迁移检查中报告遗留 AgentVersion，部署方应审核后把 Provider 固化到正式字段。
- `0.4.0`：计划移除 AgentVersion 的隐式 legacy resolver。显式 `runtime_provider=mock/hermes` 仍按环境与 profile 执行；历史 RuntimeSession 保持原 Provider/版本，不做静默回写。

## 租约与恢复

1. `nico_worker_claimer` 不能直接查询业务表，只能执行固定 search path 的 `claim_next_run`。
2. 函数按 Task priority 和 Run 创建时间使用 `FOR UPDATE SKIP LOCKED` 领取 Pending 或租约过期的 Planning/Running Run。
3. Worker 回到 `nico_runtime` + TenantContext 事务加载 Task/AgentVersion，创建或锁定 RuntimeSession。
4. 心跳和每个事件/终态提交都校验 worker owner、lease token 和未过期时间。
5. API 取消先提交 Run/Task/RuntimeSession Cancelled 并清空租约；Provider 的迟到结果被拒绝。
6. 恢复只对 descriptor 明确声明 `resume` 的 Provider 开放；缺 session、能力不支持或 Provider 不匹配时以稳定错误失败。
7. Nico Native ReAct 从已提交的 schema v2 checkpoint 恢复，并校验 provider、protocol、execution mode、执行清单 Hash 和 checkpoint Hash；不兼容或终态 checkpoint 禁止恢复。
8. 接管时中断中的 ModelCall 被终态化并建立 replay relation；ToolCall 通过稳定 idempotency key 复用，旧 lease 的事件和终态提交继续由租约 fencing 拒绝。
9. 已终态且声明应生成 Action 的 ModelCall 复用原响应：缺批次时补写一次修复关系，已有批次时按持久化 cursor 恢复；两者均不发起 replay ModelCall 或重复计费。
10. Nico Native Plan-and-Execute 从 schema v3 checkpoint 和最新持久化 Plan revision 恢复；Plan 定义不塞入 checkpoint，已提交 Planner/Reflection/Step/Judge ModelCall 不重复计费。
11. 每次领取前执行最小权限 `reconcile_expired_tool_approvals()`，原子终态化到期请求、待执行 ToolCall/RunStep 并唤醒 Run；Run 树取消则把 requested 审批置为 cancelled。
12. 同一领取边界执行 `reconcile_user_input_requests()`，修复到期请求和 answered-but-unwoken 窗口；UserInput answer 只从受保护请求行恢复为内部 untrusted Observation。

## Memory 与 Skill 运行时准备

第一次领取 Run 时，Worker 在同一个租户事务中完成以下冻结：

1. 计算 Tenant settings 与不可变 AgentVersion 的 `memory_policy`/`skill_policy` 交集，包括 top-k、`max_tokens` 和 `max_chars` 双重上限；Child Run 再与 Parent 快照和 delegation restrictions 求交。
2. 先按 scope、状态、过期时间和类型过滤 Memory，再执行 pgvector top-k；Skill 只从显式 allowlist 中解析 Published stable/canary 版本。
3. 将精确 ID、版本、content hash、scope、来源 Hash、解析分支和受字符上限约束的内容写入 RuntimeSession 私有 `knowledge_selection_snapshot`。
4. 以 `untrusted_context` 重建 ContextSeed；ContextSnapshot 只公开 `memory_refs`、`skill_refs` 和效果元数据。
5. 为每个选择建立 RuntimeKnowledgeUsage；Context 和 ModelCall 投影分别绑定首次引用和计数，Run 终态再写入 outcome/result hash/effect metadata。

恢复只读取已冻结选择，绝不重新查询实时 Memory 或重新解析 Skill 指针。发布、失效、灰度切换和 rollback 只改变之后第一次领取的 Run。普通 Runtime API 不返回包含知识正文的 selection snapshot；查询消费事实使用 `GET /api/v1/runs/{run_id}/knowledge-usages`。

## 查询 API

- `GET /api/v1/runs/{run_id}`：权威 Run 当前状态与结果；
- `POST /api/v1/runs/{run_id}/cancel`：乐观锁权威取消；
- `GET /api/v1/runs/{run_id}/runtime`：Provider/session/capability/checkpoint/usage 摘要；
- `GET /api/v1/runs/{run_id}/trajectory`：终态规范化轨迹；
- `GET /api/v1/runs/{run_id}/events`：数据库 Event 回放。
- `GET /api/v1/runs/{run_id}/steps`：读取排序后的 RunStep；供只读 Run Inspector 使用；
- `GET /api/v1/runs/{run_id}/events/stream`：支持 `Last-Event-ID` 的租户隔离 SSE 续传；
- `GET /api/v1/runs/{run_id}/model-calls`：读取脱敏的模型请求/响应、usage、cost 与 provider request ID；
- `GET /api/v1/runs/{run_id}/agent-actions`：读取 Action 批次、连续 ordinal、安全 Intent/Hash 投影、dispatch cursor 和修复关系，不返回 Prompt、答案或 Tool 参数正文；
- `GET /api/v1/runs/{run_id}/contexts`：读取可审计的 ContextSnapshot；
- `GET /api/v1/runs/{run_id}/knowledge-usages`：读取精确知识版本、Context/ModelCall 引用计数与终态效果，不返回知识正文；
- `GET /api/v1/runs/{run_id}/plans` 与 `.../plans/{plan_id}`：按 revision 读取不可覆盖的 Plan 历史；
- `GET /api/v1/runs/{run_id}/plans/{plan_id}/steps`：读取步骤定义、依赖、执行状态、输出 Hash 和证据引用；
- `GET /api/v1/runs/{run_id}/runtime-evaluations`：读取 step validation、Reflection 和 Completion Evaluation；
- `GET /api/v1/model-endpoints`：读取当前租户的模型端点修订；写入接口受部署开关控制。
- `GET /api/v1/runs/{run_id}/tool-calls`：读取脱敏 ToolCall 结果与尝试，不公开执行租约 token；
- `GET /api/v1/tool-definitions`：读取当前租户已经实例化的版本化工具快照。
- `GET /api/v1/tool-approval-requests`：按 Run/状态读取审批请求与脱敏参数；
- `POST /api/v1/tool-approval-requests/{approval_id}/decision`：带 revision 和幂等键批准 once/run 或拒绝。

所有接口继续受 TenantContext 和 PostgreSQL FORCE RLS 约束。普通租户 API 不提供 RuntimeSession 或轨迹物理删除。
