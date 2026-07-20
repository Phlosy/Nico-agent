# Tool Gateway 与 Sandbox

## 唯一执行边界

Runtime 只能产生规范化 tool intent，不能直接 import Executor、访问数据库、打开网络、读取 Run 工作区或管理容器。Worker 为当前 `RunClaim` 创建 `GatewayRuntimeToolHandler`；Mock 直接调用该 handler，Hermes 则经每 Run 的 Nico MCP stdio 子进程、`0600` Unix socket 和随机 token 到达同一个 handler。最终只有 `ToolGateway.execute()` 能开始实际工具执行。

每次调用顺序固定为：

1. 校验 tenant、run、worker owner、lease token、expiry 与 Run 活跃状态；
2. 按精确 `name@version` 取得 Registry 实现并核对 Definition/implementation Hash；
3. 读取首次领取时冻结的租户策略与 AgentVersion 策略交集；
4. 校验并同事务保存 Native ReAct schema-v2 或 Plan schema-v3 的副作用前 checkpoint；
5. 创建或恢复幂等 ToolCall/RunStep，并在拒绝时同样留下审计事实；
6. medium/high 工具先创建 ToolApprovalRequest、发布 `ApprovalRequested` 并挂起 Run；批准后才继续；
7. 校验 input Schema，解析允许的 Secret 引用，执行 timeout/retry/cancel；
8. 校验 output Schema 与字节上限；
9. 仅在同一有效 Run 租约下提交 ToolCall、RunStep、Event、Audit 与 usage。

终态 ToolCall 由数据库 trigger 阻止更新和删除。同一 Run、工具版本、幂等键和参数返回已有终态；同键不同参数返回 `TOOL_IDEMPOTENCY_CONFLICT`。ReAct 与 Plan 模式都在副作用前持久化带完整性 Hash 的 checkpoint；Plan 工具 RunStep 还会关联对应的 Plan 执行父步骤。MCP/Runtime 幂等键由稳定调用 ID、工具版本和执行位置计算，因此子进程或 Worker 恢复不会把重放变成新副作用。

默认审批风险集合为 `medium/high`。请求、脱敏参数 Hash、期限和决定保存在 PostgreSQL；批准范围是当前调用的 `once`，或当前 Run 中同一 ToolDefinition 的 `run`。CLI 断线或 Worker 重启不会丢失请求。拒绝、超时和 Run 取消都会终态化请求及待执行 ToolCall；Worker 每次领取前通过最小权限数据库函数回收超时请求。没有 Native pre-action checkpoint 的 Adapter 调用会失败关闭，不能以一次本地确认绕过恢复保护。

## 权限配置

权限默认拒绝，通配符不受支持。当前有效允许集合是 Tenant `settings.tool_policy` 与 AgentVersion `tool_policy` 的交集。AgentVersion 是不可变版本，Run 首次领取后再冻结一次；后续修改 Tenant 不改变历史 Run。Role/Plugin 权限层尚未实现，AgentVersion 包含 `plugin_refs` 时平台会清空工具授权并记录策略错误。

租户配置示例：

```json
{
  "tool_policy": {
    "allow": [
      "file.read@1.0.0",
      "file.write@1.0.0",
      "http.read@1.0.0",
      "database.read@1.0.0",
      "python.execute@1.0.0"
    ],
    "permissions": [
      "filesystem.read",
      "filesystem.write",
      "network.http.read",
      "database.read",
      "code.python.execute"
    ],
    "secret_refs": {
      "database_url": "env:NICO_TOOL_SECRET_RESEARCH_DATABASE_DSN"
    },
    "tools": {
      "http.read@1.0.0": {
        "allowed_domains": ["example.com", ".sec.gov"],
        "max_redirects": 2,
        "max_response_bytes": 262144
      },
      "database.read@1.0.0": {
        "source": "research",
        "max_rows": 200,
        "statement_timeout_ms": 3000
      },
      "python.execute@1.0.0": {
        "wall_time_seconds": 5,
        "memory_bytes": 67108864,
        "pids_limit": 8,
        "output_bytes": 16384
      }
    }
  }
}
```

AgentVersion 必须重复声明允许集合和权限；Secret 只声明逻辑名，不复制引用或值：

```json
{
  "tool_policy": {
    "allow": ["http.read@1.0.0", "database.read@1.0.0"],
    "permissions": ["network.http.read", "database.read"],
    "secrets": ["database_url"]
  }
}
```

配置值不能扩大租户限制：数字取更小值，列表取交集，布尔值取 AND，映射只保留双方共有键。Agent 未提供某工具配置时使用租户配置；Secret 引用只允许 `env:NICO_TOOL_SECRET_<NAME>`，并在 Gateway 执行时解析。引用和值都不会进入 Prompt；ToolCall/Event/Audit/错误和轨迹对敏感键和值递归脱敏。

## 内置工具

| 精确引用 | 权限 | 风险/隔离 | 默认超时 | 行为边界 |
| --- | --- | --- | ---: | --- |
| `file.read@1.0.0` | `filesystem.read` | low / workspace | 10s | 只读当前 tenant/run 的 UTF-8 文件 |
| `file.write@1.0.0` | `filesystem.write` | medium / workspace | 10s | 原子写当前 Run 相对路径 |
| `report.write@1.0.0` | `report.write` | medium / workspace | 10s | 只写 Markdown 或 canonical JSON |
| `http.read@1.0.0` | `network.http.read` | medium / network | 30s | 仅 GET/HEAD、默认 HTTPS、域名白名单 |
| `database.read@1.0.0` | `database.read` | medium / read-only DB | 30s | 单条参数化 SELECT/WITH、只读角色 |
| `python.execute@1.0.0` | `code.python.execute` | high / container | 40s | 一次性固定镜像、无网络的受限 Python |

### 文件与报告

工作区物理位置是 `<NICO_WORKSPACE_ROOT>/<tenant_id>/<run_id>`。实现使用目录文件描述符、`O_NOFOLLOW`、链接计数与文件类型复核，拒绝绝对路径、反斜杠、NUL、`.`/`..`、符号链接、越界硬链接、FIFO/socket/device 和父目录竞态。写入在同一目录创建排他临时文件，完成 `fsync` 后原子替换；flock 与配额复核防止并发突破总量。Compose 使用专用 named volume，`worker-state-init` 只将根目录设置为非 root `nico` 用户的 `0700`。

### HTTP 只读

每次请求和每次重定向都重新检查 scheme、host、port、`allowed_domains` 和全部 DNS 结果。loopback、private、link-local、multicast、reserved、unspecified 与 IPv4-mapped IPv6 默认拒绝；连接直接绑定已校验 IP，TLS 仍使用原 hostname 做 SNI/证书验证，从而避免 DNS rebinding。实现不继承代理环境，不允许用户设置 Host/Header，限制连接、读取、重定向、正文、解压后输出和内容类型。仅测试环境可同时由平台配置与策略显式开启 loopback HTTP。

### 数据库只读

调用参数只能选择策略中的逻辑 `source`，不能提交 DSN、用户名或 Schema 权限。查询必须是单条无注释的 SELECT/WITH 和位置参数，拒绝 DDL/DML、COPY、锁、事务控制及危险函数。Executor 开启只读事务、statement/idle timeout，验证当前角色不是 superuser/createdb/createrole/replication/bypassrls，随后套一层 LIMIT 并限制序列化输出。管理员必须自行创建最小只读数据库角色；平台运行数据库凭据不能复用为工具数据源。

### Python Sandbox Runner

Worker 只向内网 Runner 发送源码、非敏感 JSON input 和更严格的资源值。Runner 不接受镜像、命令、挂载、网络或环境变量选择。Docker payload固定为：

- 镜像 `python:3.12.10-alpine@sha256:4bbf5ef9ce4b273299d394de268ad6018e10a9375d7efc7c2ce9501a6eb6b86c`；
- 用户 `65534:65534`、只读 rootfs、`NetworkMode=none`、`CapDrop=ALL`、`no-new-privileges`；
- 独立 PID/CPU/内存/内存交换/墙钟限制和受限 `/tmp` tmpfs；
- 容器内清空环境，限制 file descriptor/core/file size、stdout/stderr/result 字节；
- 完成、超时、OOM、取消与异常后均强制删除容器。

生产必须把 Runner 视为宿主级高权限服务并独立加固。它是唯一挂载 `/var/run/docker.sock` 的 Compose 服务，且不获得 PostgreSQL、Redis、MinIO、模型 Provider 或 Worker Secret。`scripts/bootstrap.sh` 会预拉固定摘要；Docker Engine 不应允许 Runner 从未固定的 registry 内容运行。

## Hermes MCP

Hermes Adapter 兼容固定版本 0.18.2。每个 Run 生成独立 `HERMES_HOME` 和 `0600 config.yaml`，仅注册 `nico` stdio MCP；命令行看不到 socket/token。Hermes 的 terminal、web、browser、file、memory、skills 和 delegate toolset 显式禁用。MCP server 实现 initialize、ping、tools/list、tools/call，并只把当前 Gateway `list_authorized()` 的精确版本映射为 MCP 名称，例如 `file.read@1.0.0` 映射为 `nico__file_read__v1_0_0`。

运行 Hermes 的 Python 环境必须安装官方 MCP client 依赖，例如：

```bash
python -m pip install 'hermes-agent[mcp]==0.18.2'
```

平台开发依赖固定 `mcp==1.26.0` 与修复版本 `starlette==1.0.1`，用于对本地 Hermes 0.18.2 做真实无模型凭据发现测试。该测试只证明 MCP 边界兼容，不证明模型推理质量。

## 查询

- `GET /api/v1/tool-definitions`：当前租户已实例化的工具版本快照；
- `GET /api/v1/runs/{run_id}/tool-calls`：脱敏参数、状态、尝试、结果/错误、usage 和时间；
- `GET /api/v1/runs/{run_id}/events`：`ToolDefinitionRegistered`、`ToolCallStarted/Rejected/Succeeded/Failed/TimedOut/Cancelled`；
- `GET /api/v1/audit`：`tool.definition.register`、`tool.call.start/reject/finish`；
- `GET /api/v1/runs/{run_id}/trajectory`：Runtime 的 `tool.call.started/completed` 规范事件。
- `GET /api/v1/tool-approval-requests`：按 Run/状态读取持久化请求；
- `POST /api/v1/tool-approval-requests/{id}/decision`：批准 once/run 或拒绝，要求 revision 与幂等键。

平台没有直接工具执行 REST API。工具只能由持有有效 Run 租约的 Worker 经 Gateway 调用。

## 当前限制

Plugin 动态加载、Role 权限层、量化交易工具、API Key/JWT 和限额管理尚未实现。工具审批已有持久化与审计边界，但当前操作者身份仍来自受信网络 Header，不等于生产级身份认证。HTTP/DB 是通用只读工具，不能用于实盘下单；任何未来外部写入或交易工具都必须复用 Gateway，并增加更严格的权限与 Approval，不能靠 Prompt 扩权。
