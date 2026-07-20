# Configuration

Nico uses Pydantic settings with the `NICO_` prefix. Docker Compose reads the root `.env` file for service credentials, published ports, and selected Worker settings.

Copy the example before starting:

```bash
cp .env.example .env
```

The checked-in values are local defaults. Change all passwords and the Sandbox Runner token before using a shared host or network.

## Compose configuration

These variables are present in `.env.example` and consumed by `docker-compose.yml`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `COMPOSE_PROJECT_NAME` | `nico-agent-platform` | Compose resource prefix；本地 Release 演练使用 `nico-agent-local-release` |
| `NICO_BACKEND_IMAGE` | `nico-agent-backend:local` | API、Native Worker 与辅助服务镜像；Release 安装器固定为版本 Tag |
| `NICO_HERMES_IMAGE` | `nico-agent-hermes:local` | 可选 Hermes Worker 镜像；Release 安装器固定为版本 Tag |
| `NICO_WEB_IMAGE` | `nico-agent-web:local` | Console 镜像；Release 安装器固定为版本 Tag |
| `NICO_RUNTIME` | `native` | `nico-service` 选择的已安装 Worker Profile：`native` 或 `hermes` |
| `NICO_PULL_POLICY` | `always` | Release 服务镜像拉取策略；`make install` 的本地演练固定为 `never` |
| `POSTGRES_DB` | `nico_agent` | Platform database |
| `POSTGRES_USER` | `nico` | Initial database owner |
| `POSTGRES_PASSWORD` | `nico-change-me` | Initial database password |
| `POSTGRES_PORT` | `15432` | Published PostgreSQL port；本地 Release 演练使用 `25432` |
| `REDIS_PORT` | `16379` | Published Redis port；本地 Release 演练使用 `26379` |
| `MINIO_ROOT_USER` | `nico-minio` | MinIO administrator name |
| `MINIO_ROOT_PASSWORD` | `nico-minio-change-me` | MinIO administrator password |
| `MINIO_BUCKET` | `nico-artifacts` | Provisioned private bucket |
| `MINIO_API_PORT` | `19010` | Published MinIO API port；本地 Release 演练使用 `29010` |
| `MINIO_CONSOLE_PORT` | `19011` | Published MinIO Console port；本地 Release 演练使用 `29011` |
| `API_PORT` | `18000` | Published Nico API port；本地 Release 演练使用 `28000` |
| `WEB_PORT` | `18080` | Published Console port；本地 Release 演练使用 `28080` |
| `NICO_LOG_LEVEL` | `INFO` | Python log level |
| `NICO_DEPENDENCY_TIMEOUT_SECONDS` | `2` | Health-probe dependency timeout |
| `NICO_ARTIFACT_MAX_BYTES` | `10485760` | Maximum bytes accepted for one Artifact |
| `NICO_CONVERSATION_ATTACHMENT_TTL_SECONDS` | `86400` | Unconsumed Conversation attachment lifetime |
| `NICO_CONVERSATION_ATTACHMENT_MAX_COUNT` | `16` | Maximum staged files per Conversation |
| `NICO_CONVERSATION_ATTACHMENT_MAX_TOTAL_BYTES` | `26214400` | Maximum cumulative staged bytes |
| `NICO_WORKER_HEALTH_INTERVAL_SECONDS` | `15` | Worker dependency-check interval |
| `NICO_WORKER_POLL_INTERVAL_SECONDS` | `1` | Empty queue poll interval |
| `NICO_WORKER_LEASE_SECONDS` | `30` | Run lease duration |
| `NICO_WORKER_HEARTBEAT_SECONDS` | `10` | Run lease heartbeat interval |
| `NICO_WORKER_CONCURRENCY` | `1` | Execution loops in the Worker process |
| `NICO_WORKER_ID` | `nico-worker` | Worker identity prefix |
| `NICO_TOOL_APPROVAL_REQUIRED_RISKS` | `["medium","high"]` | Tool risk levels that require a durable human decision; use `[]` only in controlled compatibility tests |
| `NICO_TOOL_APPROVAL_TTL_SECONDS` | `900` | Requested approval lifetime before Worker reconciliation expires and wakes the Run |
| `NICO_HERMES_COMMAND` | `hermes` | Hermes CLI command parsed by the adapter |
| `NICO_HERMES_ENABLED` | `false` | Explicitly register the optional Hermes adapter |
| `HERMES_AGENT_SPEC` | `hermes-agent[mcp]==0.18.2` | Package spec used only when building the optional `hermes` profile |
| `OPENROUTER_API_KEY` | unset | 仅显式传给 Hermes Worker 的可选 Provider 凭据 |
| `OPENAI_API_KEY` | unset | 仅显式传给 Hermes Worker 的可选 Provider 凭据 |
| `ANTHROPIC_API_KEY` | unset | 仅显式传给 Hermes Worker 的可选 Provider 凭据 |
| `NICO_MODEL_ENDPOINT_WRITES_ENABLED` | `false` | Enable trusted-control-plane model endpoint writes; Compose development defaults to `true` |
| `NICO_MODEL_SECRETS_FILE` | `./config/model-secrets.env` | Owner-only env file mounted only into the Native Worker for guided Provider setup |

交互式 Provider 向导不要求手工编辑这些变量。Release 安装会把
`NICO_MODEL_SECRETS_FILE` 写成安装目录中的绝对路径，并建立 `0600` 空文件；
`nico setup` 只在确认本地新 Key 时通过 `nico-service` 原子增加唯一的
`NICO_MODEL_SECRET_*` 名称。已有部署可以直接传入 `env:NICO_MODEL_SECRET_*`
或 `secret:*` 逻辑引用，此时 CLI 不读取值，也不会重建 Worker。
| `NICO_SANDBOX_RUNNER_TOKEN` | local placeholder | Private Worker-to-Runner bearer token |

The lease validator requires the heartbeat interval to be shorter than the lease duration.

The bucket is private. Artifact authorization and metadata live in PostgreSQL; API and Worker receive MinIO credentials from deployment settings, while Runtime providers receive only the bounded Artifact handler.

## Application settings

The following settings exist in `nico_agent.config.Settings`. Compose supplies internal dependency URLs and state paths directly; direct-process deployments may set them through the environment.

### Service and dependencies

| Setting | Application default | Notes |
| --- | --- | --- |
| `NICO_ENVIRONMENT` | `development` | `development`, `test`, or `production`; production rejects local Tenant headers |
| `NICO_API_HOST` | `0.0.0.0` | Direct API process bind address |
| `NICO_API_PORT` | `8000` | Container/direct-process API port; different from published `API_PORT` |
| `NICO_CORS_ORIGINS` | localhost URLs | JSON list when supplied through an environment variable |
| `NICO_DATABASE_URL` | unset | Optional SQLAlchemy URL overriding individual database fields |
| `NICO_DATABASE_HOST` | `localhost` | Compose uses `postgres` internally |
| `NICO_DATABASE_PORT` | `5432` | Compose internal port |
| `NICO_DATABASE_NAME` | `nico_agent` | Database name |
| `NICO_DATABASE_USER` | `nico` | Database user |
| `NICO_DATABASE_PASSWORD` | `nico` | Direct-process default; Compose supplies its configured password |
| `NICO_REDIS_URL` | `redis://localhost:6379/0` | Compose uses service DNS |
| `NICO_MINIO_URL` | `http://localhost:9000` | Compose uses service DNS |
| `NICO_MINIO_ACCESS_KEY` | `nico-minio` | Change for shared deployments; never place in AgentVersion or Task input |
| `NICO_MINIO_SECRET_KEY` | local placeholder | Hidden from Settings repr; production rejects the checked-in default |
| `NICO_MINIO_BUCKET` | `nico-artifacts` | Private Artifact bucket |
| `NICO_ARTIFACT_MAX_BYTES` | `10485760` | Per-upload bound, at most 100 MiB |
| `NICO_CONVERSATION_ATTACHMENT_TTL_SECONDS` | `86400` | Staged attachment lifetime, 60 seconds to 7 days |
| `NICO_CONVERSATION_ATTACHMENT_EXCERPT_CHARS` | `16000` | Maximum UTF-8 text excerpt retained for bounded context |
| `NICO_CONVERSATION_ATTACHMENT_MAX_COUNT` | `16` | Staged attachment count per Conversation |
| `NICO_CONVERSATION_ATTACHMENT_MAX_TOTAL_BYTES` | `26214400` | Cumulative staged bytes per Conversation |

### Runtime and workspace

| Setting | Default | Notes |
| --- | --- | --- |
| `NICO_HERMES_CWD` | unset | Optional Hermes process working directory |
| `NICO_HERMES_STATE_ROOT` | `/tmp/nico-agent-hermes` | Compose uses a private named volume |
| `NICO_WORKSPACE_ROOT` | `/tmp/nico-agent-workspaces` | Compose uses a private named volume |
| `NICO_WORKSPACE_MAX_FILE_BYTES` | `1048576` | Maximum file size |
| `NICO_WORKSPACE_MAX_TOTAL_BYTES` | `10485760` | Maximum per-Run workspace size |

GitHub Release 安装把配置保存在 `~/.nico/config/deployment.env`，权限为
`0600`，并通过 `nico-service` 保证 Native 与 Hermes Worker 二选一。切换
已安装部署时重新运行安装器：

```bash
bash install.sh --runtime hermes --provider openrouter
```

源码部署的默认 Worker 镜像不安装 Hermes CLI，也不挂载 Hermes 状态。要
运行显式配置的 Hermes AgentVersion，先停止默认 Worker，再启动可选镜像：

```bash
docker compose stop worker
docker compose --profile hermes up --detach --build worker-hermes
```

The profile builds `backend/Dockerfile.hermes` with Hermes `0.18.2`. Model-provider credentials remain a separate operator responsibility. Do not run the default and Hermes Workers against the same unpartitioned queue.

### Native model gateway

| Setting | Default | Notes |
| --- | --- | --- |
| `NICO_MODEL_CONNECT_TIMEOUT_SECONDS` | `10` | Model connection timeout |
| `NICO_MODEL_READ_TIMEOUT_SECONDS` | `120` | Streaming read timeout |
| `NICO_MODEL_MAX_RESPONSE_BYTES` | `10485760` | Maximum decoded response size |
| `NICO_MODEL_MAX_ATTEMPTS` | `3` | Bounded attempts before any text/tool output |
| `NICO_MODEL_RETRY_BASE_SECONDS` | `0.2` | Exponential backoff base; jitter is added |
| `NICO_MODEL_ALLOW_HTTP_LOOPBACK` | `false` | Deployment half of local-only loopback permission |
| `NICO_MODEL_TRUSTED_PRIVATE_HOSTS` | `[]` | JSON list of deployment-approved private model hostnames |
| `NICO_MODEL_ALLOW_HTTP_TRUSTED_HOSTS` | `false` | Permit plain HTTP only for deployment-approved private hosts |

Model credentials use references such as `env:NICO_MODEL_SECRET_OPENAI`. Put the referenced `NICO_MODEL_SECRET_*` value in the deployment secret store and never in AgentVersion, endpoint JSON, Task input, logs, or repository files. Production keeps model endpoint write APIs disabled unless the trusted control plane explicitly enables them.

### HTTP and database read tools

| Setting | Default |
| --- | --- |
| `NICO_HTTP_MAX_RESPONSE_BYTES` | `1048576` |
| `NICO_HTTP_CONNECT_TIMEOUT_SECONDS` | `5` |
| `NICO_HTTP_READ_TIMEOUT_SECONDS` | `10` |
| `NICO_HTTP_MAX_REDIRECTS` | `3` |
| `NICO_HTTP_ALLOW_LOOPBACK` | `false` |
| `NICO_DATABASE_TOOL_CONNECT_TIMEOUT_SECONDS` | `5` |
| `NICO_DATABASE_TOOL_STATEMENT_TIMEOUT_MS` | `5000` |
| `NICO_DATABASE_TOOL_MAX_ROWS` | `500` |
| `NICO_DATABASE_TOOL_MAX_OUTPUT_BYTES` | `1048576` |

Loopback HTTP requires both platform configuration and an Agent policy opt-in and is intended only for tests. Database read sources must use separately provisioned least-privilege credentials referenced by policy; the platform database credential must not be reused.

### Python Sandbox Runner

| Setting | Default |
| --- | --- |
| `NICO_SANDBOX_RUNNER_URL` | `http://sandbox-runner:8090` |
| `NICO_SANDBOX_DOCKER_SOCKET` | `/var/run/docker.sock` |
| `NICO_SANDBOX_WALL_TIME_SECONDS` | `10` |
| `NICO_SANDBOX_MEMORY_BYTES` | `134217728` |
| `NICO_SANDBOX_NANO_CPUS` | `500000000` |
| `NICO_SANDBOX_PIDS_LIMIT` | `32` |
| `NICO_SANDBOX_OUTPUT_BYTES` | `65536` |
| `NICO_SANDBOX_MAX_CONCURRENCY` | `4` |

The sandbox image is pinned by digest in application settings. It runs as UID/GID `65534`, with no network, a read-only root filesystem, dropped capabilities, bounded resources, and a temporary filesystem. Docker Socket access still makes the Runner a host-sensitive service; see [security.md](security.md).

## Tool secrets

Tool policy accepts logical references of the form:

```text
env:NICO_TOOL_SECRET_<NAME>
```

The value is resolved inside Tool Gateway only when an authorized Run executes the tool. Do not place secret values in AgentVersion, Task input, Prompt, or `.env.example`.

See [tool-gateway.md](tool-gateway.md) for policy examples and exact tool behavior.
