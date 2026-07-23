# Nico CLI

> 当前状态：CLI 同时提供单 Agent 独立 Session 和受管理的多 Agent Project。持久化 chat、ProjectSession、监督、Intervention、`nico exec`、Rich 终端视图和敏感工具审批均使用服务端权威事实。

## 安装

面向本机或受信网络的 Release 安装会同时部署服务栈与 CLI：

```bash
curl -fsSL https://github.com/Phlosy/Nico-agent/releases/latest/download/install.sh | bash
nico --version
nico-service doctor
```

安装器将 CLI 放在 `~/.nico/releases/<tag>/venv` 管理的虚拟环境中，并默认把 `nico` 和
`nico-service` 链接到 `~/.local/bin`。如果该目录不在 `PATH` 中，安装器会
给出提示。CLI 仍是独立的 HTTP 客户端，不会在终端进程中运行 Agent Loop。

当前仓库版本也可以单独安装到开发虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/pip install ./backend
.venv/bin/nico --version
```

单独安装 CLI 时，API 和 Worker 必须已经运行。完整的固定版本、Hermes、
升级与移除流程见[安装与部署](installation.md)。

## 配置第一个 Profile

先从 `scripts/demo.sh` 输出或部署管理员处获得 Tenant ID，然后配置服务地址：

```bash
nico config set local \
  --api-url http://localhost:18000 \
  --tenant-id <tenant-id> \
  --actor-id local-operator
nico config use local
nico doctor
```

`nico doctor` 不只检查 tenant ID 是否已填写，还会向当前 API 验证该 tenant 确实
存在；指向另一套数据库的旧 profile 会以 `tenant_access` 失败显示。

Linux 默认配置路径为 `~/.config/nico/config.toml`；可以用 `nico config path` 查看实际位置。CLI 以 `0600` 原子写入配置文件。配置只保存令牌所在的环境变量名，不保存明文令牌：

```bash
nico config set production \
  --api-url https://nico.example.com \
  --tenant-id <tenant-id> \
  --api-token-env NICO_PRODUCTION_TOKEN
export NICO_PRODUCTION_TOKEN='由安全存储提供的令牌'
```

当前 Alpha 的 `X-Tenant-ID` 是受信网络中的开发上下文，不是正式身份认证。生产端启用 API Key/JWT 后可以继续使用 token 环境变量配置。

## 当前可用命令

```text
nico version [--server]
nico health [--live]
nico doctor
nico setup [--status]
nico provider add|configure|test|list
nico web configure|status|test|disable
nico config path|list|show|set|use|delete
nico project list|get|new|status|members|open|session|timeline|tasks
nico project member-add|member-state|lead
nico project sync|cadence|cycles
nico project guide|escalate|interventions|withdraw|cancel
nico agent list|get|versions|create|capabilities
nico task get
nico run get|runtime|events|watch
nico chat [MESSAGE]
nico exec [PROMPT]
nico conversation list|get|history
```

示例：

```bash
nico health
nico agent list
nico agent versions <agent-id>
nico run get <run-id>
nico run runtime <run-id>
nico run events <run-id>
```

## 首次设置与 Provider 引导

Release 安装后直接运行：

```bash
nico setup
```

命令先显示模型、Web、Agent 能力、Search → Fetch 验证四行权威状态。已 ready 的步骤
直接复用，Web 可明确跳过并得到 partial 状态；只有四项都 ready 才报告 full。单纯
检查不会启动提示或写入状态：

```bash
nico setup --status
```

四项全部 ready 后再次交互运行会显示维护菜单，不会重放凭据输入。自动化可以显式
选择同一维护路径；先前跳过的 Web 也可以直接恢复：

```bash
nico setup --reconfigure model
nico setup --reconfigure web
nico setup --reconfigure capabilities
nico setup --reconfigure verification
nico setup --enable-web
```

最后的在线验证不依赖所选模型自行决定是否调用工具。平台会在一个独立、可恢复的证明
Run 中依次执行固定的 Search、从其结果中选择一个 URL 执行 Fetch，再生成只包含已抓取
来源 URL 的 Final 结果。两次调用仍经过 AgentVersion 冻结策略、Tool Gateway、审批、
来源绑定和审计；普通 `nico chat` 不使用这条专用状态机。

Web 步骤会单独显示 DNS 解析器选择：`system` 使用操作系统 DNS；`cloudflare`
（交互推荐）和 `google` 使用平台固定的 DNS-over-HTTPS 端点。Clash 等 Fake-IP 环境应
选择 DoH。该选择会冻结进 Web Provider policy 和随后发布的 AgentVersion；重新选择时
运行 `nico setup --reconfigure web`，然后按提示发布新的能力版本。

如果当前 Profile 已有一个发布中的 `nico_native` AgentVersion，并且它引用启用、
经过真实 completion 验证的 endpoint，模型步骤会直接复用；旧式未验证 endpoint、
Hermes 路由、draft/superseded 版本或 disabled endpoint 不会跳过向导。模型管理命令为：

```bash
nico provider add openai
nico provider configure anthropic
nico provider test openai
nico provider list
nico provider list --models openai --limit 20
```

向导内置十个常用 Provider 预设与官方地址，优先展示推荐模型，并在协议支持时
执行有界模型发现。可以选择精确模型 ID。新 Key 仅在本机 TTY 隐藏输入，不存在
`--api-key` 参数；已有环境变量或 Secret Store 使用逻辑引用。非交互示例：

```bash
nico --json provider add openai \
  --credential-ref secret:providers/openai \
  --model gpt-5.6-terra \
  --project <project-id> \
  --starter-name nico-assistant \
  --starter-display-name "Nico Assistant" \
  --yes
```

CLI 先轮询 Worker 探测，再显示服务端生成的完整预览；activation 必须携带该
preview hash。取消、超时、`Ctrl+C`、预览过期或激活失败都会走同一个本地回滚
路径。随后选择 Minimal、Web Research、Developer 或 Custom；Custom 支持全选当前
可用项、清空和逐项选择，不可用项保留原因。medium/high 风险在发布前合并确认，
实际调用时仍由 Tool Gateway 审批。也可稍后复用同一选择器：

```bash
nico agent create <name>
nico agent capabilities <agent-id>
```

成功后可以直接运行裸 `nico chat`；旧的显式 Project/Agent 入口仍兼容。能力发布会
生成新 AgentVersion，既有 Conversation 不变，必须新建 Conversation 才会使用新能力。

## 独立 Session

裸 `nico chat` 是用户与一个 ready Agent 的独立会话。只有一个 ready Agent 时直接
使用；有多个时在终端选择，脚本模式必须传精确名称或 ID。服务端在内部幂等解析
当前 actor 的隐藏 Personal Project，但 CLI 不把它作为用户概念展示：

```bash
nico chat
nico chat --agent researcher "请分析这项任务"
nico chat --continue "继续上一轮"
```

同一 Tenant 中不同 `actor_id` 的 Personal Project 和默认恢复互相隔离。Profile 只
记录最近使用的 Agent ID，服务端每次仍重新校验 ready 状态和 Tenant 边界。
`--version` 可显式固定一个已发布 AgentVersion；兼容入口仍可使用
`nico chat --project <project-id> --agent <agent-id>`。

## Project 工作区

Project 是多 Agent 的共享任务边界，不是某个 Agent 的所有物。每个 active Project
恰有一个可替换 Lead；Lead 和 Member 都有稳定 ProjectSession。AgentVersion 发布
新版本时只轮换该 Session 的当前 Conversation，旧 Turn/Task/Run 保持可读：

```bash
nico project new launch --goal "发布并验证新版本" --lead coordinator \
  --member researcher --member reviewer --yes
nico project open launch
nico project session launch --agent researcher
nico project status launch
nico project members launch
nico project timeline launch --agent researcher
nico project tasks launch
```

Project、Agent 参数接受精确名称或 UUID；无匹配或重名会失败，不会任意选择。
成员 paused/removed 或 Project archived 后 Session 自动只读，历史仍可查询。

Lead 同步和 cadence 使用 PostgreSQL 持久化周期，不依赖 CLI 持续在线：

```bash
nico project sync launch
nico project cycles launch
nico project cadence launch 30m
nico project cadence launch off
```

运行中指导绑定成员当前 Run 和 revision；普通项目范围变化改投 Lead：

```bash
nico project guide launch --agent researcher "先运行回归测试"
nico project escalate launch --agent researcher "将范围缩小到 API"
nico project interventions launch --agent researcher
nico project withdraw launch <intervention-id> --agent researcher
nico project cancel launch --agent researcher
```

Project Session 交互中也可用 `/guide TEXT`、`/escalate TEXT`、
`/interventions` 和 `/withdraw ID [REASON]`。活动 Run 存在时，普通输入与独立
Session 一样成为下一条持久 Turn；只有显式 `/guide` 才绑定当前 Run，显式
`/escalate` 才创建 Project 范围变化。Intervention 只作为有界、不可信文本在安全
模型边界消费，不能改变工具、Secret、预算、成员或协调权限。

时间线只展示持久化计划、决策理由摘要、步骤、工具、Artifact、测试、阻塞和
结构化结果；不会请求或显示模型原始私有思维链。监督输出把数据库权威 metrics
与可选 Lead narrative 分栏，模型摘要失败不会丢失事实。

## 持续对话操作

不带 `MESSAGE` 时进入普通滚动式交互，适合 SSH 和日志复制。模型执行期间输入框
保持可用；按 Enter 会立即把文本保存为下一条服务端 Turn，而不等待当前回答。
Conversation 同一时刻只执行一个 Run，后续消息严格按 sequence 串行领取；退出 CLI
不会删除队列。普通提交不额外写入滚动区；只有消息确实在等待时，composer 才在
`you ›` 上方固定显示 `next ›` 和下一条排队输入的单行截断预览。用户输入使用蓝色
身份，Nico 回答使用带金色边框和标题的面板，即使关闭颜色也能区分双方内容。整个
composer 固定在终端下沿的状态栏上方，不会跟在上一条回答后停留在屏幕中部。
底部状态栏持续显示完整或确定性缩写后的模型、当前 Run 冻结权限、
下一 Run 权限、执行阶段和排队数量。输入历史保存在平台默认用户数据目录，目录权限
为 `0700`、文件为 `0600`。快捷键语义如下：

- `Alt+Enter`：插入换行；
- `Ctrl+C`：输入时清空当前输入，执行时请求服务端取消当前 Run；
- `Ctrl+D`：退出 CLI，不删除服务端 Conversation、Task 或 Run；
- `/help`：按会话、状态、检查和控制分组显示全部已实现命令；
- `/history`、`/conversations`、`/new`、`/continue`、`/resume ID`、`/title TEXT`：管理和切换持久化会话；
- `/status`、`/agent`、`/version`、`/runtime`、`/usage`、`/context`：查看当前运行上下文；
- `/plan`、`/steps`、`/tools`、`/children`、`/messages`、`/artifacts`、`/audit`、`/inspect`：读取服务端持久化执行事实；
- `/approvals`：查看当前 Run 的审批请求及终态；
- `/permissions [ask|auto-medium|auto-all]`：查看或修改当前 Conversation 后续 Run 的审批模式；扩大自动批准范围需要确认；
- `/queue`：查看活动、暂停和排队 Turn；`/queue cancel TURN` 取消指定队列项，`/queue resume` 显式恢复异常后续执行；
- `/approve ID once|run`：批准一次调用，或允许同一 Run 后续调用相同工具版本；
- `/reject ID [REASON]`：拒绝待处理调用；
- `/guide TEXT`、`/interventions`、`/withdraw ID [REASON]`：在 Project Session 中管理一次性运行指导；
- `/escalate TEXT`：把范围、优先级或跨 Agent 依赖变化送到 Lead Session；
- `/cancel`、`/retry`：取消活动队首，或只重试导致队列暂停的 Turn；
- `/attach PATH`：读取本地文件字节并暂存到下一轮；服务端不会读取本地路径；
- `/compact`：排队执行独立摘要 Run，并在完成后更新覆盖序号、输入 Hash 和 ModelCall 引用；
- `/download ARTIFACT_ID [PATH]`：只下载当前 Conversation 已引用的 Artifact，以 `0600` 原子写入并拒绝符号链接目标；
- `/exit` 或 `/quit`：退出。

`/attach` 上传成功后保持 `staged`，下一条消息创建 Turn 时由服务端在同一事务内将所有未过期暂存件物化为该 Run 拥有的 Artifact。附件按 SHA-256 内容寻址，默认单件不超过 10 MiB、最多 16 件、累计不超过 25 MiB，24 小时未消费则过期。文本附件只把有界摘要和引用放入上下文，正文不会默认展开。

`/compact` 不删除任何 Turn。它创建一个普通 Task/Run，由 Worker 使用 Conversation 冻结的 AgentVersion 生成摘要；Nico 保存覆盖序号、摘要输入 Hash 和摘要 ModelCall。下一轮按“平台与 AgentVersion 指令 → 当前输入 → 已发布 Memory/Skill → 最近完整 Turn → summary → 有界 Artifact 摘要/引用”选择输入，实际选择写入 ContextSnapshot。同一 Run 恢复时复用 RuntimeSession 中冻结的选择，不重新选择历史。

## 敏感工具审批

新 Conversation 默认使用 `ask`：风险等级为 `medium` 或 `high` 的平台工具需要人工
确认。`auto-medium` 自动批准已经获得 AgentVersion 与 Tenant 授权的 medium 调用，
`auto-all` 自动批准已经授权且未被部署策略锁定的 medium/high 调用。模式只决定是否
等待人工确认，不会添加工具、Skill、Secret、网络或文件权限。活动 Run 在首次创建
RuntimeSession 时冻结模式，所以运行中变更会在 footer 中显示为 current → next，
只影响尚未开始的 Run。

需要人工确认时，Tool Gateway 先保存 pre-action checkpoint、待执行 ToolCall 和
ToolApprovalRequest，再把 Run 置为 `waiting_for_approval`。CLI 收到
`ApprovalRequested` 后保存当前草稿与光标，显示工具精确版本、风险、过期时间和
服务端已经脱敏的参数：

```text
[1] Allow once
[2] Allow for this Run
[3] Reject
```

选择会通过带 revision 和 `Idempotency-Key` 的 API 写入服务端，之后原草稿和光标
原样恢复。批准后 Worker 重新 claim Run，从同一个 checkpoint 和 ToolCall 幂等键
继续；拒绝或超时会暂停 Conversation 队列并保留后续消息。`run` scope 只适用于
当前 Run、同一 ToolDefinition，不扩张 AgentVersion 或租户策略。policy 自动批准也
会保留 ToolApprovalRequest、Event 和 Audit，并明确记录决定来源。

CLI 断开不会取消审批，也不会隐式批准。再次运行 `nico chat --resume <conversation-id>` 时，CLI 会读取该 Run 尚未决定的请求并重新展示。非 TTY、`--json`、`nico exec` 和 `nico run watch` 遇到审批时会安全停止观察并返回 `approval_required` 标识，绝不会自动授权；可稍后进入交互 chat，或使用 `/approve`、`/reject` 完成决定。

请求超过 `NICO_TOOL_APPROVAL_TTL_SECONDS` 后由 Worker 的数据库协调器原子标记为 `expired` 并唤醒 Run。Run 取消时请求转为 `cancelled`。requested 之后只能产生一次 approved、rejected、expired 或 cancelled 终态，所有请求与决定均有 Event 和 AuditRecord。

## Web 搜索配置

Web 能力默认不对 Agent 授权。本地安装已启用 Provider 写入并启动 SearXNG，首选
`nico setup` 先做 provider-only 激活，再在能力步骤只授权选中的 Agent。独立维护也可
通过 CLI 探测 Provider、预览 tenant policy 与新 AgentVersion，并在确认后原子发布：

```bash
nico web configure searxng --dns-resolver cloudflare --project <project-id> --agent <agent-id>
nico web configure brave --dns-resolver system --project <project-id> --agent <agent-id>
nico web status
nico web test
nico web disable --agent <agent-id>
```

Brave 的 API key 使用隐藏提示和本地 Secret transaction；也可传
`--credential-ref env:NICO_TOOL_SECRET_<NAME>`，但不能把 key 值放在命令行。
`web test` 只创建持久化 probe，不发布版本。`web disable` 先显示 projection，再发布
移除 Search/Fetch 权限的新 AgentVersion；旧 Run 仍按首次领取时冻结的策略完成或由
operator 取消。`nico web status` 区分未配置、未授权、Secret 不可用、Provider
不可达与最近 probe 失败。`nico doctor` 同时给出 Web 配置和本地 Secret 可用性诊断。

SearXNG 是本机默认项且无需搜索 Key；Brave 不运行本地搜索服务，但需要受保护的 API
Key。两种路径最终都使用相同的 `web.search`、来源绑定的 `web.fetch`、审批和审计边界。
DNS 解析器不是模型参数：模型只能提交搜索结果 URL，不能选择或修改 DoH endpoint。

聊天运行中在输入框下方的固定 footer 显示 `Web search`、`Reading source`；
终结时把
成功搜索、已读来源和可恢复失败压缩成一条 `Web research` 摘要。单个来源的
`WEB_FETCH_SOURCE_DENIED`、不可达或 HTTP 错误不会用连续红色行淹没回答，但仍完整
保存在 ToolCall/Event/Audit 中。审批面板只展示截断 query 或 URL origin。原始 query、
页面正文、Provider payload、内部事件名与 checkpoint 不进入 human 滚动区。需要完整
持久化事实时使用 `/tools`、`/audit` 或 `--json`。

恢复指定会话、继续最近的活跃会话或只读检查：

```bash
nico chat --resume <conversation-id>
nico chat --continue
nico chat --continue "继续上一轮"
nico chat --resume <conversation-id> --read-only
nico conversation list --limit 20
nico conversation get <conversation-id>
nico conversation history <conversation-id>
```

`--continue` 可配合 `--project`、`--agent` 缩小最近会话范围；`--resume` 与 `--continue` 互斥。已有 Conversation 固定创建时的 AgentVersion，发布新版本不会静默改变旧会话。要切换版本，应新建 Conversation。

每条消息在一个数据库事务中创建 ConversationTurn、Task 与 Pending Run。默认最多
保留 20 条未开始 Turn；取消队列项不会复用 sequence。同一 Conversation 的数据库
领取条件只允许最早可执行 Run 被一个 Worker 领取。正常完成自动推进；失败、超时、
当前 Turn 取消、工具拒绝或审批过期会暂停后续执行，直到 `/retry`、取消队列项或
`/queue resume` 明确处理。Worker 仍是唯一执行者；CLI 通过可续传 SSE 接收持久化
Event，按 sequence 去重，并在流结束后通过 Turn API 校准最终结果。终端断线不改变
服务端权威状态。

普通 Conversation Turn 默认最多 12 轮模型迭代和 8 次工具调用；显式 API 预算可以
覆盖该值。Worker 在首次领取时冻结 Run 开始 UTC 时间，模型可直接换算用户请求的
时区，作为普通日期和时间问题的快路径。用户明确要求独立核验、当前外部来源或更高
精度时，仍可按 Agent 授权正常使用 Web 工具。

human 模式不会把持久化事件流原样打印到对话中。普通 Task、Run、RuntimeSession、
计划、步骤、ContextSnapshot、ModelCall 和 checkpoint 生命周期只用于审计与显式
检查命令。Runtime 必须把增量显式标记为 `assistant` 或 `internal`；交互式 chat
只把 `assistant` 增量流式写入临时 Nico 回答面板，内部规划、反思、评估和结构化
步骤输出继续隐藏。Turn 结束后，CLI 清除临时面板并用权威 `assistant_output`
落下一次最终回答。`--json` 仍返回完整事件数组供自动化消费。

### 执行中的反馈

`nico exec`、`nico run watch` 和单轮 chat 在等待服务端结果时只保留一行临时状态，
例如：

```text
⠋ Queued · 0:00
⠙ Thinking · 0:08
⠹ Running http_read@1 · 0:12 | reconnecting 1/3
```

交互式 TTY chat 使用同一安全阶段投影。输入框固定在终端下沿、footer 上方；footer
持续显示模型、权限和 `Preparing`、`Thinking`、`Running Web search`、
`Running Reading source`、`Running Reading file` 等当前阶段。有排队消息时，
`next ›` 预览位于当前输入上方。已提交的 `you ›` 输入保留在滚动历史中，Nico 最终
回答使用带标题的面板呈现；回答生成期间，同一位置逐段更新明确标记为用户可见的
模型输出。后台 SSE 使用独立连接，工具结果和最终回答通过
prompt_toolkit 的滚动输出写入，不会覆盖正在编辑的草稿。状态会在准备、规划、思考、
运行工具、反思和整理答案之间切换，并显示本次等待的经过时间。连接暂时中断时，同一
行显示有界重连次数；连接恢复后自动消失。

滚动区只保留有长期价值的结果：非 Web 工具每个调用至多一条终态，Web 尝试按 Run
汇总；另外保留已保存的 Artifact、审批面板、Run 失败或取消，以及最终回答。能够由
同一工具调用的开始和结束事件精确关联时才显示耗时。只有 Runtime 明确标记为
`assistant` 的回答增量可进入临时面板；私有推理、内部增量、内部事件名、sequence、
内部 ID、原始工具参数和返回 payload 都不会出现在 human 输出中；需要审计时使用
`/inspect`、`/plan`、`/steps`、`/tools`、`/audit` 等显式命令。

输出重定向或非 TTY 环境不播放 spinner，也不连续打印阶段变化：只输出一次初始等待提示、有界重连提示和上述持久结果。`--json` 合同不变，仍只输出一个 JSON 文档及真实事件数组；临时状态和连接提示不会被伪造成事件写入 JSON。

## 一次性执行与 detached Run

`nico exec` 为脚本、定时任务和 CI 提供非交互入口。默认创建一条独立 Conversation/Turn，并持续读取 Run SSE 直到终态：

```bash
nico exec "生成结论" --project <project-id> --agent <agent-id>
nico exec "生成结论" --project <project-id> --agent <agent-id> --version <version-id>
```

结构化输入必须是一个 JSON 对象，只接受 `prompt`、`project_id`、`agent_id`、`agent_version_id` 和 `title`。命令行的 Project/Agent/Version 覆盖文件字段；不能同时提供位置 PROMPT 与文件中的 `prompt`：

```bash
nico exec --input task.json --output result.json --json
nico exec "生成日报" --project <project-id> --agent <agent-id> --detach --json
nico run watch <run-id> --json
nico run watch <run-id> --after <event-sequence> --json
```

`--detach` 在 Turn、Task 和 Run 已原子落库后立即返回 `conversation_id`、`turn_id`、`task_id` 和 `run_id`。`run watch` 只观察 Run；按 Ctrl+C 会离开观察器，不会取消服务端执行。`--after` 使用持久化 Event sequence，可用于断线续读。

`--output` 以 `0600` 原子写入完整结果 JSON，并拒绝最终路径为符号链接。`--json` 可以放在顶层或 `exec`/`run watch` 子命令中；stdout 始终只有一个 JSON 文档，不混入 ANSI、Logo 或进度文本。

所有命令支持顶层连接覆盖：

```bash
nico --profile local --json agent list
nico --api-url http://localhost:18000 --tenant-id <tenant-id> run get <run-id>
NO_COLOR=1 nico health
```

`--json` 保证 stdout 是单个 JSON 文档，错误以 JSON 写到 stderr；JSON 模式不输出 ANSI、表格或 Logo。资源命令缺少 Tenant ID 时以退出码 2 和 `TENANT_CONTEXT_REQUIRED` 失败，不会发出无上下文请求。

环境覆盖包括 `NICO_CONFIG_FILE`、`NICO_PROFILE`、`NICO_API_URL`、`NICO_TENANT_ID`、`NICO_ACTOR_ID` 和仅驻留进程内存的 `NICO_API_TOKEN`。

## 终端小猫标识

`chat` 与 human-mode `exec` 的 header 使用三行代码原生小猫，与 Agent、Version、Runtime、Project 和 Tool 摘要并排显示：

```text
 /\_/\
( o.o )
 > ^ <
```

标识只使用 ASCII 字符，不依赖图片协议、emoji 或特殊块字符；蓝色用于轮廓，金色用于眼睛和鼻子。`NO_COLOR`、`--no-color`、`TERM=dumb` 和输出重定向保留同一轮廓但不带 ANSI，`--json` 完全不显示 Logo。
