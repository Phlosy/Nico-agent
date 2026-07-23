# 开发与运行

## 运行方式

当前实现支持两条路径：

1. 推荐的本地开发路径：数据依赖由 Compose 提供，应用服务从源码运行；
2. 用于容器与部署验证的 Docker Compose 全栈路径。

## 推荐：本地开发

直接运行：

```bash
make run
```

To include the optional loopback-only SearXNG dependency while keeping API, Worker, CLI and
frontend on local source, run:

```bash
make run WEB_SEARCH=searxng
```

This uses the pinned `web-search-local` Compose profile and does not build an application
image. The generated development configuration points Nico at the exact local SearXNG JSON
endpoint; normal fetched pages still use strict public-network policy.

`make run` 会按 Python/Node 版本及依赖清单变化同步 editable Python 包、CLI
entrypoint 和前端依赖；首次运行时还会创建 `.venv`，并把开发 CLI 链接到
`NICO_BIN_DIR`（默认 `~/.local/bin`）下的 `nico`。如果希望只准备依赖而不启动服务，可运行：

```bash
make dev-setup
```

这个目标执行以下有界流程：

1. 同步 editable backend/CLI，并安装本地 `nico` 命令；
2. 检查并使用 `docker compose up --detach --no-build` 补齐 PostgreSQL、Redis、MinIO；
3. 停止仍在运行的 Compose 应用容器，同时保留基础服务、数据卷和可选 fake-model；
4. 等待依赖健康并执行 Alembic migration；
5. 从宿主机启动 Sandbox Runner、API、Worker 和 Vite Web；
6. 创建或校验源码数据库中的 development tenant/project，并写入隔离的 CLI profile；
7. 全部就绪后保持前台运行，`Ctrl-C` 一次停止所有源码进程。

因此修改 Python 或前端代码时不会构建 `nico-agent-*` 镜像。API 和 Sandbox
Runner 使用 Uvicorn reload；Worker 代码变化后重启 `make run`。基础数据容器会
保留，后续启动可直接复用。

另一个终端可直接运行可编辑安装的 CLI；它默认连接源码 API `:8000`：

```bash
nico chat
nico --version
```

开发 CLI 固定使用 `.nico/dev/config/cli.toml` 和 `development` profile，不会读取或
改写 Release 安装使用的全局 `local` profile。Provider Key 保存在权限为 `0600`
的 `.nico/dev/config/model-secrets.env`；配置新 Provider 时，前台 supervisor 会
自动重启源码 Worker 以加载密钥，不会重启已安装版本的容器。

开发版输出同时包含正式包版本和 `branch-commit[-dirty]` 标识，例如
`nico 0.2.0 (dev-bc21ef0-dirty)`。若 `~/.local/bin` 不在 `PATH`，按启动日志提示
加入 `PATH`，或继续使用 `make cli ARGS=chat`。

CLI 不是常驻服务，因此不放进 Compose。开发生命周期命令为：

```bash
make run    # 启动或补齐基础服务，并从源码运行 Nico
make stop   # 只停止 Sandbox Runner、API、Worker 和 Web
make clean  # 停止全部开发服务并移除容器，保留数据卷
```

只启动或停止 Compose 基础服务时仍可使用：

```bash
make infra-up
make infra-down
```

本地源码端口为 API `8000`、Web `5173`、Sandbox Runner `8090`；PostgreSQL、
Redis、MinIO 继续使用下表中的 Compose 宿主端口。`make stop` 不调用 Compose；
`make infra-down` 和 `make clean` 都保留数据卷，避免普通服务清理误删开发数据。

源码模式不会产生应用镜像。需要显式构建开发镜像时使用 `make dev-images`；三个
镜像共用 `branch-commit[-DTN_SUB][-dirty]` Tag。正式发布镜像仍只使用 `vX.Y.Z`。

## Compose 全栈验证

```bash
cp .env.example .env
scripts/bootstrap.sh
scripts/dev.sh --detach
```

`bootstrap.sh` 校验 Compose、拉取固定 Digest 的数据镜像与 Python sandbox 镜像，并构建 API、Worker、Sandbox Runner 和 Web。它用于验证 Dockerfile、镜像和完整容器拓扑，不应作为每次代码修改后的开发内环。`worker-state-init` 只负责把两个专用状态卷设为 `nico` 用户的 `0700` 目录；Worker 仍以非 root 运行。`dev.sh --detach` 在返回前等待 API 与 Web 的 HTTP 及容器级健康检查。

默认发布端口：

| 服务 | 地址 |
| --- | --- |
| Web | `http://localhost:18080` |
| API / OpenAPI | `http://localhost:18000` / `/docs` |
| PostgreSQL | `localhost:15432` |
| Redis | `localhost:16379` |
| MinIO API / Console | `localhost:19010` / `19011` |
| Sandbox Runner | 仅 Compose 内网 `sandbox-runner:8090`，不发布宿主端口 |

所有值都可在 `.env` 覆盖。`.env` 不进入 Git；`.env.example` 仅含开发默认值，生产环境必须更换密码并使用 Secret 管理。

## 进程边界

- `api` 执行迁移后启动 FastAPI。
- `worker` 同时监督依赖并运行有界领取循环；通过最小 claimer 角色领取后，以租户事务执行 Provider、Tool Gateway、心跳和持久化轨迹；不挂载 Docker socket。
- `sandbox-runner` 只接受 bearer token 认证的固定 Python 执行合同；它是唯一挂载 Docker socket 的服务，不接收数据库/模型 Secret、镜像名、命令、挂载或网络参数。
- `web` 从只读 API 展示 readiness 和脱敏 Run Inspector，不存储权威数据也不提供写操作。
- PostgreSQL 是 Run 队列、租约、状态与轨迹的权威来源；Redis 和 MinIO 的职责保持 ADR-0002 的边界。

Worker 可通过 `NICO_WORKER_POLL_INTERVAL_SECONDS`、`NICO_WORKER_LEASE_SECONDS`、`NICO_WORKER_HEARTBEAT_SECONDS`、`NICO_WORKER_CONCURRENCY` 和 `NICO_WORKER_ID` 调整。默认 Worker 不安装、注册或挂载 Hermes。显式 Hermes AgentVersion 需先停止默认 Worker，再使用 `docker compose --profile hermes up --detach --build worker-hermes`。该可选镜像固定 `hermes-agent[mcp]==0.18.2`；命令、工作目录与隔离状态根分别由 `NICO_HERMES_COMMAND`、`NICO_HERMES_CWD`、`NICO_HERMES_STATE_ROOT` 指定。平台只生成 `nico` MCP 配置，短期 token 不进入命令行。不要让默认 Worker 与 Hermes Worker 共用未分区队列同时运行。

工作区、HTTP、数据库和 Sandbox 限制均可通过 `NICO_WORKSPACE_*`、`NICO_HTTP_*`、`NICO_DATABASE_TOOL_*`、`NICO_SANDBOX_*` 收紧。生产必须更换 `NICO_SANDBOX_RUNNER_TOKEN`；sandbox 镜像必须保留 `@sha256:` 固定摘要。只读数据库 DSN 通过环境 Secret 引用提供，例如租户策略引用 `env:NICO_TOOL_SECRET_RESEARCH_DATABASE_DSN`，AgentVersion 只声明需要的 Secret 名，不能保存 DSN。

## 清理

```bash
scripts/cleanup.sh            # 停止容器，保留数据卷
scripts/cleanup.sh --volumes  # 同时删除数据卷
scripts/cleanup.sh --all      # 另删除 .venv、node_modules 与前端 dist
```
