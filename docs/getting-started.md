# Getting Started

This guide starts Nico Agent locally and produces a real persisted Task/Run result through the HTTP API, PostgreSQL-backed Worker lease, and Mock Runtime.

> Nico is experimental. The local tenant headers are not authentication, so keep the control plane on localhost or a trusted network.

## Requirements

- Docker Engine
- Docker Compose v2
- `curl`
- Python 3 for `scripts/demo.sh`
- permission to access the Docker Socket on Linux

The Docker Socket is required by `sandbox-runner`, the only service allowed to create the one-shot Python sandbox containers.

## Start Nico

```bash
cp .env.example .env
scripts/dev.sh --detach
```

The command builds the API, Worker, Sandbox Runner, and Console; starts PostgreSQL/pgvector, Redis, and MinIO; applies Alembic migrations; and waits for the API and Console health checks.

Confirm readiness:

```bash
curl http://localhost:18000/api/v1/health/ready
```

A ready response reports `postgres`, `redis`, and `minio` as `up`.

## Run the local demo

```bash
scripts/demo.sh
```

The script uses the credential-free Mock Runtime. It:

1. creates a Demo Tenant, Project, Agent, and immutable AgentVersion on the first run;
2. publishes the AgentVersion;
3. creates a Task and Run;
4. waits for the Worker to complete the Run;
5. prints the Run result and API query URLs.

The reusable resource IDs are stored in `.nico/demo-state.json`, which is excluded from Git. Later invocations reuse those resources and create a new Task/Run. If the database was reset, the script detects stale state and recreates the resources.

Expected result shape:

```text
Nico Demo completed
-------------------
Status:    completed
Runtime:   mock 1.0
Result:
{
  "message": "Nico completed a recoverable, auditable demo Run.",
  "runtime": "mock",
  "summary": "AgentVersion -> Task -> Run -> Worker -> Result"
}
Artifact:  not produced by this Mock Runtime demo
```

The exact Run ID changes each time. The result is stored in PostgreSQL and can be queried using the Tenant headers printed by the script.

## Service endpoints

| Service | Default URL |
| --- | --- |
| Console status and read-only Run Inspector | <http://localhost:18080> |
| Swagger UI | <http://localhost:18000/docs> |
| OpenAPI document | <http://localhost:18000/openapi.json> |
| Readiness | <http://localhost:18000/api/v1/health/ready> |
| MinIO Console | <http://localhost:19011> |

The Console includes infrastructure status plus a read-only Run Inspector. Paste the Tenant ID and Run ID printed by `scripts/demo.sh`, or open `/?tenant=<tenant UUID>&run=<run UUID>#run-inspector`. It reads redacted Run facts and has no mutation controls. Agent, Memory, Skill, and authenticated administration views are not implemented.

## API context in local mode

`POST /api/v1/tenants/bootstrap` creates a Tenant without a Tenant header. Other control-plane calls require:

```http
X-Tenant-ID: <tenant UUID>
X-Actor-ID: <local actor name>
```

These headers establish local/test context only. They do not authenticate a caller and are rejected when the application environment is set to `production`.

See [api.md](api.md) for the implemented resources and use Swagger UI for the current request/response schemas.

## Stop or reset

Stop containers while preserving named volumes:

```bash
scripts/cleanup.sh
```

Stop containers and delete PostgreSQL, Redis, MinIO, and Worker state volumes:

```bash
scripts/cleanup.sh --volumes
```

The `.nico/demo-state.json` file is local client state. It can remain after a volume reset because `scripts/demo.sh` detects invalid stored IDs. Delete `.nico/` manually if you want to discard it immediately.

For failures, see [troubleshooting.md](troubleshooting.md).
