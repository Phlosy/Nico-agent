# Product Roadmap

Nico Agent is an experimental self-hosted Agent Runtime. This roadmap lists product directions without delivery dates. It is not a compatibility or support commitment.

## Available now

- immutable AgentVersion publication and rollback;
- persisted Task/Run execution, cancellation, retry, checkpoint, and trajectory;
- independent Nico Native Direct/ReAct/Plan Runtime, deterministic Mock Runtime, and experimental Hermes `0.18.2` adapter;
- dynamic Parent/Child delegation, bounded budget and permission inheritance, durable messages, suspension/wakeup, recovery and tree cancellation;
- private content-addressed Artifact storage with Child-to-Parent sharing and controlled download;
- policy-constrained Tool Gateway and six built-in tool executors;
- durable medium/high ToolCall approval with once/run scopes, CLI disconnect recovery, timeout reconciliation and full audit;
- first-class Nico CLI for profiles, durable chat, exec/watch, attachments, context compaction and interactive approvals;
- PostgreSQL RLS tenant isolation;
- Memory candidate validation, independent approval, publication, lifecycle, and scoped retrieval;
- SkillVersion validation, approval, canary deployment, promotion, disable, and rollback;
- health endpoints, structured logs, resumable SSE, Event/Audit queries, and a Console with infrastructure status plus a read-only redacted Run Inspector;
- local Docker Compose deployment and credential-free Demo.

## Planned directions

| Direction | Current gap |
| --- | --- |
| API Key/JWT authentication and authorization | Local Tenant headers are not authentication |
| Worker horizontal-scaling acceptance | Lease/concurrency primitives exist; multi-replica deployment is not validated |
| Complete Artifact workbench | Minimal Runtime lifecycle exists; preview, retention, revocation UI and cross-project distribution do not |
| SSE notification latency | Streaming persists and resumes from PostgreSQL; Redis notification fan-out is not implemented |
| Additional Runtime adapters | Nico Native, Mock and optional Hermes exist; other engines have no adapter |
| Authenticated operator workbench | The current Console is read-only and requires local Tenant/Run IDs; it has no mutation or administration controls |
| Policy administration | Policies are API/config objects without an operator UI |
| Kubernetes deployment | No manifests, NetworkPolicies, or Helm chart |
| Backup and restore | No automated database/object-store recovery workflow |
| Production observability | No metrics backend, alert rules, or published performance baseline |
| Production hardening | Auth, rate limits, secret manager integration, release signing, and operational guidance remain open |
| Python and TypeScript SDKs | Consumers currently call HTTP/OpenAPI directly |

## Outside Nico core

Team structure, role collaboration, business Workflow, trading rules, and research processes belong to the domain systems that call Nico. Nico may add generic protocol hooks needed by those systems, but it will not impose one fixed multi-Agent organization model.

Propose roadmap changes with the [Feature Request](https://github.com/Phlosy/Nico-agent/issues/new?template=feature_request.yml) form.
