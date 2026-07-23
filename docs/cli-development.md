# Nico CLI 开发说明

## 架构边界

CLI 位于 `backend/src/nico_agent/cli/`，即使和服务端安装在同一个 distribution 中，也只能通过 REST/SSE 调用 Nico。CLI 代码不得导入 ControlPlaneService、SQLAlchemy model、Worker、Runtime Provider 或 Tool Executor 来执行业务操作。

当前模块职责：

```text
cli/app.py       Typer 命令树和命令参数
cli/config.py    platformdirs、TOML profile、环境覆盖、0600 原子写入
cli/client.py    httpx REST、租户 header、token、错误和 request ID
cli/output.py    Rich human、JSON、no-color 和 stderr 错误输出
cli/errors.py    稳定 CLI 错误合同与退出码
cli/sse.py       增量 SSE 分片解析、续读游标和事件合同
cli/chat.py      Conversation 选择、slash command、附件、compact 和下载
cli/chat_controls.py Conversation 权限模式和持久队列控制
cli/chat_session.py 交互 TTY 的异步 composer、后台 SSE、审批草稿和 footer
cli/execution.py exec/watch 与私有原子结果文件写入
```

prompt_toolkit 用于滚动式 chat 输入、私有历史、Alt+Enter 多行、动态 footer 和安全的
后台输出；Rich 继续承担人类可读输出。交互控制器使用 `prompt_async`，阻塞 httpx/SSE
由独立 client 在后台线程观察，所有 buffer 变化回到主事件循环。CLI 不建立第二套执行
状态机，Conversation queue、审批模式和 ConversationTurn 执行状态都由服务端投影。

## 本地运行

```bash
.venv/bin/pip install -e 'backend[dev]'
.venv/bin/nico --help
.venv/bin/nico --json health
```

配置测试环境时不要把真实 token 写入命令参数或仓库文件。Profile 只能保存环境变量名称：

```bash
.venv/bin/nico --config-file /tmp/nico-test.toml config set local \
  --api-url http://localhost:18000 \
  --tenant-id <tenant-id> \
  --api-token-env NICO_TEST_TOKEN
```

## 测试

CLI 单元测试覆盖配置、API client、输出和命令解析：

```bash
.venv/bin/pytest backend/tests/unit/test_cli_*.py
```

基础真实 API smoke 会启动或复用 Compose 服务，运行确定性 Mock Runtime Demo，再通过安装后的 `nico` 子进程检查配置、health、doctor、Project、Agent、Task、Run、Runtime 和 Event：

```bash
scripts/e2e-cli-goal-b.sh
```

Conversation/chat E2E 会重建实际 API/Worker 镜像，执行两个 Turn、resume/continue/history、JSON 无 ANSI，并向前台 CLI 发送真实 SIGINT 后验证服务端 Run/Turn 已取消：

```bash
scripts/e2e-cli-goal-c.sh
```

Goal E 的附件、summary 与 ContextSnapshot 验收：

```bash
scripts/e2e-cli-goal-e.sh
scripts/verify-cli-goal-e.sh
```

## 新增命令规则

1. 优先扩展 `NicoApiClient` 的稳定方法，不在命令函数中散落 URL 和 header。
2. 所有命令经过 `CliError` 映射，不能回显响应中的 Secret 或 Python traceback。
3. human 与 JSON 必须来自同一事实；不得为终端展示写死 mock 数据。
4. JSON 的 stdout 只能包含结果文档，错误写 stderr；非 TTY 和 `NO_COLOR` 不依赖 ANSI。
5. 写操作必须由服务端执行 revision、幂等、权限、Event 和 Audit；CLI 不持有最终状态。
6. 使用 `httpx.MockTransport` 做 client 单测，使用 Typer `CliRunner` 做参数与输出测试，再以真实 Compose API 做 E2E。
7. 新 slash command 必须有真实服务端合同；`/approvals`、`/approve` 和 `/reject` 只调用 ToolApprovalRequest API，不在 CLI 内伪造状态。
8. SSE 只能展示持久化 Run Event；断线按最后 sequence 设置 `Last-Event-ID`，客户端去重，流结束后再读取 Turn 终态。
9. chat 输入历史只能保存用户输入，不保存 API token；目录/文件分别保持 `0700`/`0600`。
10. `/attach` 只上传已读取的本地字节，HTTP 请求不得包含本地绝对路径；`/download` 必须原子写入 `0600` 文件并拒绝最终符号链接。
11. 交互 TTY 可以在 Run 活动时创建后续 Turn；输入队列不得保存在 CLI 内存中，也不得让后台线程直接修改 prompt buffer。
12. footer 只能消费 `ExecutionProgress` 的安全投影和服务端 queue/runtime facts；不得拼接模型 delta、原始 payload、工具参数或内部 ID。
13. 临时回答面板只消费 Runtime 明确标记为 `assistant` 的输出增量；`internal` 或缺少可见性标记的增量必须隐藏，Turn 终态仍以持久化 `assistant_output` 校准。

## 配置优先级与安全

有效值优先级为 CLI option → 环境变量 → 当前 profile → 默认值。`NICO_API_TOKEN` 或 profile 指向的 token 环境变量只在创建 HTTP client 时读取，`config show` 只显示 `api_token_available`，不会显示令牌。

配置目录尽量收紧为 `0700`，配置文件使用同目录临时文件、`fsync`、原子替换和 `0600`。`nico doctor` 会把过宽的现有配置权限标为失败。

当前服务端尚无正式 API Key/JWT，开发 `X-Tenant-ID` header 不能描述成身份认证。CLI 已保留 Bearer token 传输能力，但是否接受和如何解析由服务端认证专项决定。
