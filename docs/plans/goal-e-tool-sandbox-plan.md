# Goal E：Tool Gateway 与 Sandbox 实施计划

- 状态：In Progress
- 日期：2026-07-17
- 基线：Goal D commit `5fdc691`
- Hermes 参考：本地 `/home/node7/xpk/hermes-agent`，版本 `0.18.2`，支持 stdio/HTTP MCP

## 范围

本阶段实现平台唯一的 Tool Gateway、版本化 ToolDefinition、不可变 ToolCall、权限交集、Schema 校验、Secret 引用、幂等、超时、受控重试、取消、审计，以及文件读写、报告输出、HTTP 只读、只读数据库查询和 Python 容器沙箱。Runtime Provider 的工具意图必须经过该边界；Hermes 仅允许通过按 Run 配置的 Nico MCP Server 使用平台工具，原生工具集继续默认关闭。

Memory/Skill、Team/Workflow、Plugin 安装与动态加载、量化策略/交易、正式 API Key/JWT、SSE、SDK 和业务 Web Console 不进入 Goal E。尚未实现的 Role/Plugin 权限层不得用 Prompt 冒充；核心工具只依据租户策略与不可变 AgentVersion 工具策略授权，未来权限层只能进一步收窄。

## Tool 合同

1. `ToolDefinitionSpec` 使用稳定名称和语义版本，冻结 input/output JSON Schema、权限、超时、重试、隔离、风险和输出限制。
2. Registry 只按精确 `name@version` 查找实现；数据库 ToolDefinition 是租户可授权的不可变快照，代码实现与快照 Hash 不一致时失败关闭。
3. ToolCall 是每次逻辑调用的权威记录，唯一绑定 RunStep、ToolDefinition 和幂等键；记录调用者、脱敏参数、状态、每次尝试、结果/错误、资源使用和时间。
4. 已终结 ToolCall 不更新、不删除；相同 Run、工具版本、幂等键和相同参数返回既有终态，不重复副作用；键相同但参数不同返回稳定冲突。
5. 参数在授权前只做大小/形状检查，授权后按 input Schema 校验；结果在提交前按 output Schema 与字节上限校验。
6. 稳定错误不得包含 Secret、任意响应正文、数据库连接串、主机路径或 Python 环境变量。

## 权限与 Secret 不变量

1. 默认拒绝。允许集合是租户策略、AgentVersion `tool_policy` 和当前 Run 冻结版本的交集；未知层、空层、通配符和未实现的 Role/Plugin 引用都不能扩大权限。
2. 高风险与外部写操作默认关闭。本阶段 HTTP 仅 GET/HEAD，数据库仅单条只读查询，文件写入仅 Run 工作区，Python 无网络。
3. Secret 只以引用进入策略，由 Gateway 执行时通过 `SecretResolver` 解析并按定义注入；API、Prompt、AgentVersion、ToolCall、Event、Audit、普通日志和轨迹不保存明文。
4. MCP 会话凭据短时绑定 tenant/run/lease，放在子进程环境而非命令行；Gateway 每次调用重新校验 Run、租约和 Tool 权限。
5. 取消、租约丢失或 Run 终态后不得开始新 ToolCall；迟到结果不能覆盖权威终态。

## 隔离策略

### 文件与报告

- 每个调用只见 `<workspace_root>/<tenant_id>/<run_id>`，路径先按 POSIX 相对路径解析，再执行 `resolve`、父目录和符号链接复核。
- 拒绝绝对路径、`..`、NUL、设备文件、FIFO、socket、硬链接越界和符号链接逃逸。
- 写入采用同目录临时文件、`fsync` 和原子替换；限制单文件、调用输出和工作区总量。
- 报告工具只生成 UTF-8 Markdown/JSON，使用同一工作区边界。

### HTTP 只读

- 只允许 `https`，测试环境可显式允许 loopback HTTP；仅 GET/HEAD，禁止用户覆盖 Host。
- URL 解析后对每次 DNS 结果检查 loopback、private、link-local、multicast、reserved、unspecified 和 IPv4-mapped IPv6。
- 每次重定向重新执行 scheme、host、port、域名白名单和 DNS/IP 检查；连接目标与校验结果绑定，防止 DNS rebinding。
- 禁止代理环境继承；限制重定向次数、连接/读取超时、响应字节、内容类型和解压大小。

### 数据库只读

- 工具只连接管理员配置的只读数据源引用，不接受 DSN、用户名、Schema 或表名权限扩张。
- 只接受一条参数化 SELECT/WITH 查询；拒绝多语句、注释逃逸、DDL/DML、锁、COPY、事务与危险函数。
- 数据库角色必须只读；设置 statement timeout、idle timeout、只读事务、行数和序列化输出上限。

### Python Sandbox

- 每次调用进入一次性、无网络、只读根文件系统、非 root、drop all capabilities、no-new-privileges 的固定摘要镜像。
- 限制 CPU、内存、PID、墙钟时间、tmpfs、stdout/stderr 和结果大小；不挂载 Docker socket、宿主目录、Worker Secret 或数据库凭据。
- 生产边界是独立 Sandbox Runner；Worker 不持有 Docker socket。开发验收使用同一 Runner 合同与固定镜像。
- Runner 只接受 Python 源码和非敏感 JSON 输入，不能选择镜像、命令、挂载、网络或环境变量。

## Runtime 与 Hermes 边界

1. Runtime 协议升级时以 capability 明确表达平台工具支持；Provider 的规范化 tool intent 包含 call ID、精确工具版本、参数和幂等键。
2. Worker 通过应用服务把 intent 交给 Gateway，ToolCall/RunStep/Event/Audit 同事务持久化，再把脱敏结果返回 Provider；Provider 不接收 ORM。
3. Mock Provider 提供确定性的工具调用、失败、超时、取消与恢复合同。
4. Hermes 0.18.2 通过每 Run 的 stdio MCP Server 发现 Nico 授权工具；配置目录和凭据临时生成，CLI 只启用该 MCP toolset，不能启用 terminal/web/file 等 Hermes 原生工具集。
5. 若 Hermes MCP 边界无法在没有真实模型凭据的环境中执行完整推理，只验证真实 MCP 握手/调用和 Fake Hermes 配置边界，并如实记录外部依赖缺口。

## 验收出口

- 单测：Registry/Hash、Schema、默认拒绝、权限交集、Secret 脱敏、幂等冲突、超时/重试/取消和所有工具安全规则。
- 真实 PostgreSQL：迁移回放、RLS、跨租户、ToolCall 原子性/不可变性、并发同键唯一、失败恢复和迟到结果拒绝。
- HTTP 故障：私网/混合 DNS、IPv6、重定向到私网、DNS rebinding、超时、超限和错误正文脱敏。
- 文件故障：绝对路径、遍历、符号链接、竞态替换、超限与跨 Run/租户隔离。
- Python：无网络、只读根、非 root、资源/进程/时间/输出限制和清理。
- Runtime：Mock 工具链完整 E2E；Hermes MCP Server 的 initialize/tools/list/tools/call 边界可重复验证。
- `scripts/verify-goal-e.sh` 运行 Goal D 全量回归、Goal E 单元/集成/安全测试和 Compose E2E，并归档环境、日志与验收摘要。

## 阶段增量

1. E1：冻结合同、ADR、状态与计划；
2. E2：ToolDefinition/ToolCall、迁移、RLS、Registry 和 Schema；
3. E3：Gateway 权限/Secret/幂等/超时/重试/取消/审计；
4. E4：工作区文件和报告工具；
5. E5：HTTP 只读与 SSRF 防护；
6. E6：只读数据库与独立 Python Sandbox Runner；
7. E7：Runtime tool intent、Mock、Hermes MCP、API 与 E2E；
8. E8：全量回归、证据、文档、审查、清理和 Goal F Handoff。

只有全部出口实际通过，Goal E 才能从 In Progress 更新为 Verified。
