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
nico setup
nico provider add|configure|test|list
nico config path|list|show|set|use|delete
nico project list|get|new|status|members|open|session|timeline|tasks
nico project member-add|member-state|lead
nico project sync|cadence|cycles
nico project guide|escalate|interventions|withdraw|cancel
nico agent list|get|versions
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

## Provider 引导

Release 安装后直接运行：

```bash
nico setup
```

如果当前 Profile 已有一个发布中的 `nico_native` AgentVersion，并且它引用启用、
经过真实 completion 验证的 endpoint，命令会直接报告 ready；旧式未验证 endpoint、
Hermes 路由、draft/superseded 版本或 disabled endpoint 不会跳过向导。管理命令为：

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
路径。成功后可以直接运行裸 `nico chat`；旧的显式 Project/Agent 入口仍兼容。

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
`/interventions` 和 `/withdraw ID [REASON]`。活动 Run 存在时，普通输入会先要求
明确选择 local guidance、project change、等待或取消；非交互调用必须通过显式
命令表达动作。Intervention 只作为有界、不可信文本在安全模型边界消费，不能
改变工具、Secret、预算、成员或协调权限。

时间线只展示持久化计划、决策理由摘要、步骤、工具、Artifact、测试、阻塞和
结构化结果；不会请求或显示模型原始私有思维链。监督输出把数据库权威 metrics
与可选 Lead narrative 分栏，模型摘要失败不会丢失事实。

## 持续对话操作

不带 `MESSAGE` 时进入普通滚动式交互，适合 SSH 和日志复制。输入历史保存在平台默认用户数据目录，目录权限为 `0700`、文件为 `0600`。快捷键语义如下：

- `Alt+Enter`：插入换行；
- `Ctrl+C`：输入时清空当前输入，执行时请求服务端取消当前 Run；
- `Ctrl+D`：退出 CLI，不删除服务端 Conversation、Task 或 Run；
- `/help`：按会话、状态、检查和控制分组显示全部已实现命令；
- `/history`、`/conversations`、`/new`、`/continue`、`/resume ID`、`/title TEXT`：管理和切换持久化会话；
- `/status`、`/agent`、`/version`、`/runtime`、`/usage`、`/context`：查看当前运行上下文；
- `/plan`、`/steps`、`/tools`、`/children`、`/messages`、`/artifacts`、`/audit`、`/inspect`：读取服务端持久化执行事实；
- `/approvals`：查看当前 Run 的审批请求及终态；
- `/approve ID once|run`：批准一次调用，或允许同一 Run 后续调用相同工具版本；
- `/reject ID [REASON]`：拒绝待处理调用；
- `/guide TEXT`、`/interventions`、`/withdraw ID [REASON]`：在 Project Session 中管理一次性运行指导；
- `/escalate TEXT`：把范围、优先级或跨 Agent 依赖变化送到 Lead Session；
- `/cancel`、`/retry`：通过 revisioned API 控制当前 Turn；
- `/attach PATH`：读取本地文件字节并暂存到下一轮；服务端不会读取本地路径；
- `/compact`：排队执行独立摘要 Run，并在完成后更新覆盖序号、输入 Hash 和 ModelCall 引用；
- `/download ARTIFACT_ID [PATH]`：只下载当前 Conversation 已引用的 Artifact，以 `0600` 原子写入并拒绝符号链接目标；
- `/exit` 或 `/quit`：退出。

`/attach` 上传成功后保持 `staged`，下一条消息创建 Turn 时由服务端在同一事务内将所有未过期暂存件物化为该 Run 拥有的 Artifact。附件按 SHA-256 内容寻址，默认单件不超过 10 MiB、最多 16 件、累计不超过 25 MiB，24 小时未消费则过期。文本附件只把有界摘要和引用放入上下文，正文不会默认展开。

`/compact` 不删除任何 Turn。它创建一个普通 Task/Run，由 Worker 使用 Conversation 冻结的 AgentVersion 生成摘要；Nico 保存覆盖序号、摘要输入 Hash 和摘要 ModelCall。下一轮按“平台与 AgentVersion 指令 → 当前输入 → 已发布 Memory/Skill → 最近完整 Turn → summary → 有界 Artifact 摘要/引用”选择输入，实际选择写入 ContextSnapshot。同一 Run 恢复时复用 RuntimeSession 中冻结的选择，不重新选择历史。

## 敏感工具审批

默认情况下，风险等级为 `medium` 或 `high` 的平台工具需要人工确认。模型提出调用后，Tool Gateway 先保存 pre-action checkpoint、待执行 ToolCall 和 ToolApprovalRequest，再把 Run 置为 `waiting_for_approval`。CLI 收到 `ApprovalRequested` 后显示工具精确版本、风险、过期时间和服务端已经脱敏的参数：

```text
[1] Allow once
[2] Allow for this Run
[3] Reject
```

选择会通过带 revision 和 `Idempotency-Key` 的 API 写入服务端。批准后 Worker 重新 claim Run，从同一个 checkpoint 和 ToolCall 幂等键继续；拒绝或超时会产生失败的工具观察，Agent 可以据此调整回答。`run` scope 只适用于当前 Run、同一 ToolDefinition，不扩张 AgentVersion 或租户策略。

CLI 断开不会取消审批，也不会隐式批准。再次运行 `nico chat --resume <conversation-id>` 时，CLI 会读取该 Run 尚未决定的请求并重新展示。非 TTY、`--json`、`nico exec` 和 `nico run watch` 遇到审批时会安全停止观察并返回 `approval_required` 标识，绝不会自动授权；可稍后进入交互 chat，或使用 `/approve`、`/reject` 完成决定。

请求超过 `NICO_TOOL_APPROVAL_TTL_SECONDS` 后由 Worker 的数据库协调器原子标记为 `expired` 并唤醒 Run。Run 取消时请求转为 `cancelled`。requested 之后只能产生一次 approved、rejected、expired 或 cancelled 终态，所有请求与决定均有 Event 和 AuditRecord。

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

每条消息在一个数据库事务中创建 ConversationTurn、Task 与 Pending Run。Worker 仍是唯一执行者；CLI 通过可续传 SSE 接收持久化 Event，按 sequence 去重，并在流结束后通过 Turn API 校准最终结果。终端断线不改变服务端权威状态。

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

## Coin-cat 的视觉来源

README Logo 是透明背景的蓝金白像素猫头像。终端版本提取四个特征，而不是逐像素复制图片：

- 深灰蓝外轮廓和阴影；
- 柔和金色的猫脸主体；
- 白色额头中线与口鼻区域；
- 方块颗粒构成的清晰猫耳、眼睛和脸部轮廓。

终端图案主题是“小猫位于硬币中央”。CLI Goal D 已用 Rich Text 将主图落地，并在 `chat` 与 human-mode `exec` 启动时与 Agent、Version、Runtime、Project 和 Tool 摘要并排显示。

## 候选 A：像素圆章（最终选择）

```text
       ▄██████▄
     ▄█▓▓▓▓▓▓▓▓█▄
    █▓▒▄▀▄▓▓▄▀▄▒▓█
   █▓▒█  ●██●  █▒▓█
   █▓▒█   ██   █▒▓█
   █▓▒▀▄  ▄▄  ▄▀▒▓█
    █▓▒▒▀▄▄▄▄▀▒▒▓█
     ▀█▓▓▓▓▓▓█▀
       ▀████▀
```

优点：圆形硬币明确；猫耳、双眼、白色中线都能辨认；19 列左右适合和 Agent/Project 信息并排。缺点：依赖 Unicode 半块/块字符，必须处理宽度和无 Unicode 降级。

## 候选 B：纯 ASCII 猫币

```text
       .--------.
     .'  /\__/\  '.
    /   / o  o \   \
   |    \  --  /    |
   |     '.__.'     |
    \              /
     '.          .'
       '--------'
```

优点：兼容性最高、日志复制稳定。缺点：像素块感较弱，金白中线也不容易在无颜色时体现，品牌辨识度低于候选 A。

## 候选 C：紧凑徽章

```text
    .-====-.
   / /\_/\ \
  | ( o.o ) |
  |  > ^ <  |
   '-====-'
```

优点：窄终端占用小。缺点：更接近普通 ASCII 猫，硬币的像素质感和 README 关联较弱。

## 最终决策与实现规则

选择候选 A 作为正常交互 header 的唯一主标识；候选 C 的尺寸只作为同一品牌标识的窄终端降级，不作为第二套 Logo。选择依据是：

- 三个方案中，它最清楚地同时表达“像素猫”和“硬币”；
- 轮廓紧凑，不会像大型 ASCII banner 那样挤压对话；
- 四色区域能映射 README 的灰蓝、柔金、暖白和深色轮廓；
- 去掉 ANSI 后仍可辨认，不靠颜色维持基本语义。

当前实现规则：

- muted blue：轮廓和阴影；
- soft gold：硬币外圈与脸部主体；
- warm white：额头中线、口鼻和高光；
- dark slate：眼睛与内部线条；
- 宽度不足时使用 5 行紧凑版；
- `NO_COLOR`、`--no-color` 和 `TERM=dumb` 输出无 ANSI 版本；
- `--json` 完全不显示 Logo；human 输出重定向时使用无 ANSI 紧凑版；
- 字符宽度不可靠时退回纯 ASCII，不使用 emoji。

这是 inspired by README cat style 的终端友好重设计，不声称是原 PNG 的像素级复刻。
