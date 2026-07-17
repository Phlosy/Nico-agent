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

`bootstrap.sh` 校验 Compose、拉取固定 Digest 的数据镜像并构建应用镜像。`dev.sh --detach` 在返回前等待 API 与 Web 的 HTTP 及容器级健康检查。

默认发布端口：

| 服务 | 地址 |
| --- | --- |
| Web | `http://localhost:18080` |
| API / OpenAPI | `http://localhost:18000` / `/docs` |
| PostgreSQL | `localhost:15432` |
| Redis | `localhost:16379` |
| MinIO API / Console | `localhost:19010` / `19011` |

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
- `worker` 同时监督依赖并运行有界领取循环；通过最小 claimer 角色领取后，以租户事务执行 Provider、心跳和持久化轨迹。
- `web` 只从 readiness API 读取状态，不存储权威数据。
- PostgreSQL 是 Run 队列、租约、状态与轨迹的权威来源；Redis 和 MinIO 的职责保持 ADR-0002 的边界。

Worker 可通过 `NICO_WORKER_POLL_INTERVAL_SECONDS`、`NICO_WORKER_LEASE_SECONDS`、`NICO_WORKER_HEARTBEAT_SECONDS`、`NICO_WORKER_CONCURRENCY` 和 `NICO_WORKER_ID` 调整。Hermes 命令与工作目录分别由 `NICO_HERMES_COMMAND`、`NICO_HERMES_CWD` 指定；命令通过无 shell 的参数数组启动，Secret 只从 Worker 环境进入 Hermes 进程。

## 清理

```bash
scripts/cleanup.sh            # 停止容器，保留数据卷
scripts/cleanup.sh --volumes  # 同时删除数据卷
scripts/cleanup.sh --all      # 另删除 .venv、node_modules 与前端 dist
```
