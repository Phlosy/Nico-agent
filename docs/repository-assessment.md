# Repository implementation status

This is a concise snapshot of the current repository. The status matrix in the
root [README](../README.md#support-status) is the user-facing source of truth.

## Implemented

- FastAPI control plane for Tenant policies, Agent/Task/Run, Runtime, dynamic
  coordination, private Artifact, Tool/Model/Plan facts, Events/Audit, Memory,
  and Skill governance.
- PostgreSQL/pgvector persistence with Alembic migrations and forced tenant RLS.
- Persistent Worker leases, heartbeat, recovery contracts, cancellation, and
  bounded concurrency.
- Independent Nico Native Direct/ReAct/Plan Runtime, deterministic Mock Runtime,
  and an experimental Hermes `0.18.2` CLI adapter.
- Dynamic Parent/Child Run coordination with durable messages, atomic budget
  reservation, permission narrowing, suspension/wakeup, recovery, and tree cancellation.
- PostgreSQL-authoritative, content-addressed private MinIO Artifacts with
  explicit Child-to-Parent sharing and controlled HTTP download.
- Default-deny Tool Gateway with file, report, restricted HTTP, restricted
  database, and isolated Python executors.
- Memory candidate, validation, approval, publication, retrieval, invalidation,
  and deletion lifecycle.
- Versioned Skill validation, approval, publication, canary deployment,
  promotion, rollback, deprecation, and disablement.
- Docker Compose local environment, health-aware Console with a read-only Run
  Inspector, test suites, and a repeatable Demo.

## Important limitations

- No API Key, JWT, OIDC, or other production caller identity.
- No full Artifact workbench, public SDK, Kubernetes manifest, or
  production-hardened deployment profile.
- The default Worker image does not include or register Hermes. A separate pinned
  Compose profile is available for explicit Hermes AgentVersions; credentialed
  Hermes model inference has not been accepted.
- Multiple Worker replicas have not completed deployment acceptance.
- Team structure and business Workflow are deliberately owned by calling domain
  systems, not Nico core.

For deployment boundaries, read [Security](security.md). For planned directions,
read the [Roadmap](roadmap.md).
