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
