# Troubleshooting

## Inspect the stack

```bash
docker compose ps --all
docker compose logs --tail=100 api worker sandbox-runner web
```

All long-running services should be running. PostgreSQL, Redis, MinIO, API, Sandbox Runner, and Web have health checks. The one-shot `minio-init` and `worker-state-init` services should exit with code `0`.

## Readiness is not `ready`

Request the detailed probe response:

```bash
curl --include http://localhost:18000/api/v1/health/ready
```

The response reports PostgreSQL, Redis, and MinIO separately. Check the matching service log and verify that the ports in `.env` are not already in use.

Validate the effective Compose file:

```bash
docker compose --project-directory . --file docker-compose.yml config --quiet
```

## Local Release API is unhealthy after reinstall

旧版 `make uninstall` 会保留 PostgreSQL 数据卷但删除其初始化密码配置，随后重装
会让 API 在 Alembic 阶段报告 `password authentication failed for user "nico"`。
当前 `nico-service up` 会先通过容器网络验证数据库凭据；发现这一历史失配时，
它会在 PostgreSQL 容器内同步角色密码并重新创建失败的 API 容器，不会删除数据卷。

重新生成本地 Release 资产并安装即可恢复：

```bash
make release
make install
```

正常卸载会保留 `~/.nico/config` 和 `~/.nico/state`，后续安装可直接重用。只有明确
不再需要任何本地数据时，才运行 `nico-service purge --yes` 并删除整个
`~/.nico`。

## Docker Socket permission errors

The local user must be able to run Docker commands, and the Docker daemon must expose `/var/run/docker.sock` for the Sandbox Runner mount.

```bash
docker info
ls -l /var/run/docker.sock
```

Do not make the socket world-writable. Use the Docker installation's supported group or rootless configuration.

## Demo state is stale

`scripts/demo.sh` stores reusable IDs in `.nico/demo-state.json`. It automatically creates new resources when the stored Project or Agent no longer exists. To force a fresh Demo Tenant:

```bash
unlink .nico/demo-state.json
scripts/demo.sh
```

## A Run remains pending

Check the Worker log and container state:

```bash
docker compose ps worker
docker compose logs --tail=200 worker
```

The Worker must be connected to PostgreSQL and able to claim a Run. If the stack was rebuilt after configuration changes, recreate the Worker:

```bash
docker compose up --detach --build worker
```

## Provider setup was interrupted

The CLI normally rolls back the temporary Key and releases maintenance in a
`finally` path. If the process or host stopped mid-attempt, recover before retrying:

```bash
nico-service provider-secret recover
nico provider add <provider>
```

`RUNTIME_MAINTENANCE_ACTIVE_RUNS` means at least one Run is not terminal; wait for
or cancel it before changing a local Key. `RUNTIME_MAINTENANCE_LEASE_LOST` means
the bounded lease expired, so the attempt is rolled back. If Worker replacement is
unhealthy, inspect `nico-service logs worker`; unrelated API/Web services are not
restarted. Existing `env:`/`secret:` references do not use the local secret bridge.

## Provider verification fails

Use `nico provider test <provider>` to repeat the bounded completion probe. Stable
errors distinguish authentication, unavailable model, rate limit, timeout, network,
endpoint policy and protocol failures without persisting the upstream response body.
Model discovery failure is non-fatal in the interactive flow: choose a recommended
or exact manual model ID.

## Web search is unavailable or disabled

Start with:

```bash
nico web status
nico web test
nico doctor
```

`WEB_PROVIDER_WRITES_DISABLED` means deployment policy has not enabled Web configuration;
set `NICO_WEB_PROVIDER_WRITES_ENABLED=true` on the local API/Worker before configuring.
`WEB_SEARCH_NOT_CONFIGURED` or an unauthorized status means the selected AgentVersion does
not contain both tenant and version grants. Publish with `nico web configure`; changing tenant
settings alone does not mutate a frozen AgentVersion. `WEB_PROVIDER_RATE_LIMITED` is a bounded
429 and should be retried after the Provider interval; `WEB_PROVIDER_UNAVAILABLE` indicates
network/5xx/DNS failure. For local SearXNG, run `make run WEB_SEARCH=searxng` and confirm its
health before `nico web test`.

`WEB_FETCH_SOURCE_DENIED` means Fetch did not receive the platform `tool_call_id` for a
successful Search in the same Run, or the URL was not in that result/allowlist. Do not weaken
SSRF policy to bypass it. After `nico web disable`, existing frozen Runs may still finish with
their old grant; new Runs must use the newly published Web-disabled version.

## Project Session is read-only or guidance is rejected

`PROJECT_SESSION_READ_ONLY` 表示成员已 paused/removed，或 Project 已 archived。
这是保留历史但阻止新工作的预期行为；使用 `nico project status <name>` 和
`nico project members <name>` 查看状态。恢复成员需要匹配最新 Project/member
revision，归档 Project 不可重新开启。

`PROJECT_SESSION_RUN_NOT_FOUND` 表示所选成员 Session 没有活动 Run，或显式
`--run` 不属于该 Session。空闲成员应直接在 `nico project session` 中发送普通
消息；只有活动 Native ReAct/Plan Run 才接受 `guide`。范围或优先级变化使用：

```bash
nico project escalate <project> --agent <member> "描述需要重新规划的变化"
```

`REVISION_CONFLICT` 表示 Run 或 Intervention 已被 Worker/其他操作者更新。重新运行
`nico project interventions` 或 `nico project timeline` 获取最新状态后再决定，不要
通过旧 revision 强制覆盖。

## Hermes Run fails with `HERMES_NOT_INSTALLED`

The default Worker intentionally does not install Hermes. For an AgentVersion explicitly pinned to Hermes, switch to the optional profile:

```bash
docker compose stop worker
docker compose --profile hermes up --detach --build worker-hermes
```

If the Run reports `RUNTIME_PROVIDER_NOT_FOUND`, the Adapter is disabled; this is fail-closed behavior and Nico will not fall back to Native. `HERMES_VERSION_UNSUPPORTED` means the CLI is not exactly `0.18.2`. Provider credentials are configured separately and must never be placed in AgentVersion JSON or shell history.

## Reset local state

Stop containers and remove named volumes:

```bash
scripts/cleanup.sh --volumes
```

This permanently removes the local PostgreSQL, Redis, MinIO, workspace, and any optional Hermes state volumes. It does not delete `.env` or `.nico/`.
