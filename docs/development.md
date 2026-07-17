# 开发与运行

## 运行方式

当前实现支持两条路径：

1. 推荐的 Docker Compose 全栈路径；
2. API/Web 在宿主机运行、数据依赖由 Compose 提供的本地开发路径。

## Compose 全栈

```bash
cp .env.example .env
scripts/bootstrap.sh
scripts/dev.sh --detach
```

`bootstrap.sh` 校验 Compose、拉取固定 Digest 的数据镜像与 Python sandbox 镜像，并构建 API、Worker、Sandbox Runner 和 Web。`worker-state-init` 只负责把两个专用状态卷设为 `nico` 用户的 `0700` 目录；Worker 仍以非 root 运行。`dev.sh --detach` 在返回前等待 API 与 Web 的 HTTP 及容器级健康检查。

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

## 宿主机开发

先启动依赖并安装环境：

```bash
docker compose up -d postgres redis minio minio-init
python3 -m venv .venv
.venv/bin/pip install -e 'backend[dev]'
npm --prefix frontend ci
```

API 使用配置中的本地默认地址；若使用 `.env.example` 的发布端口，需要显式覆盖：

```bash
NICO_DATABASE_URL=postgresql+asyncpg://nico:nico-change-me@localhost:15432/nico_agent \
NICO_REDIS_URL=redis://localhost:16379/0 \
NICO_MINIO_URL=http://localhost:19010 \
.venv/bin/uvicorn nico_agent.main:app --app-dir backend/src --reload
```

另一个终端运行 Web：

```bash
npm --prefix frontend run dev
```

Vite 的 `/api` 与 `/openapi.json` 开发代理默认指向 `localhost:8000`。

## 进程边界

- `api` 执行迁移后启动 FastAPI。
- `worker` 同时监督依赖并运行有界领取循环；通过最小 claimer 角色领取后，以租户事务执行 Provider、Tool Gateway、心跳和持久化轨迹；不挂载 Docker socket。
- `sandbox-runner` 只接受 bearer token 认证的固定 Python 执行合同；它是唯一挂载 Docker socket 的服务，不接收数据库/模型 Secret、镜像名、命令、挂载或网络参数。
- `web` 只从 readiness API 读取状态，不存储权威数据。
- PostgreSQL 是 Run 队列、租约、状态与轨迹的权威来源；Redis 和 MinIO 的职责保持 ADR-0002 的边界。

Worker 可通过 `NICO_WORKER_POLL_INTERVAL_SECONDS`、`NICO_WORKER_LEASE_SECONDS`、`NICO_WORKER_HEARTBEAT_SECONDS`、`NICO_WORKER_CONCURRENCY` 和 `NICO_WORKER_ID` 调整。Hermes 命令、工作目录与隔离状态根分别由 `NICO_HERMES_COMMAND`、`NICO_HERMES_CWD`、`NICO_HERMES_STATE_ROOT` 指定；Hermes 必须以 `hermes-agent[mcp]==0.18.2` 或等价固定安装提供 MCP client。平台只为 Hermes 生成 `nico` MCP 配置，短期 token 不进入命令行。

工作区、HTTP、数据库和 Sandbox 限制均可通过 `NICO_WORKSPACE_*`、`NICO_HTTP_*`、`NICO_DATABASE_TOOL_*`、`NICO_SANDBOX_*` 收紧。生产必须更换 `NICO_SANDBOX_RUNNER_TOKEN`；sandbox 镜像必须保留 `@sha256:` 固定摘要。只读数据库 DSN 通过环境 Secret 引用提供，例如租户策略引用 `env:NICO_TOOL_SECRET_RESEARCH_DATABASE_DSN`，AgentVersion 只声明需要的 Secret 名，不能保存 DSN。

## 清理

```bash
scripts/cleanup.sh            # 停止容器，保留数据卷
scripts/cleanup.sh --volumes  # 同时删除数据卷
scripts/cleanup.sh --all      # 另删除 .venv、node_modules 与前端 dist
```
