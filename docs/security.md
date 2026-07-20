# Security and Deployment Boundaries

This document describes the current implementation, not a production certification. Nico Agent is experimental and the supplied stack is intended for localhost or a trusted network.

## Current deployment posture

The control plane does not implement API Key, JWT, OAuth, mTLS identity, user sessions, rate limits, or a production authorization model. Local/test requests use `X-Tenant-ID` and `X-Actor-ID` to construct a Tenant context. Those headers are not proof of identity.

Do not expose the API, Console, PostgreSQL, Redis, MinIO, or Sandbox Runner directly to the public Internet. The included Compose topology is a local evaluation environment, not a hardened production manifest.

## Tenant isolation

Implemented tenant boundaries include:

- immutable `tenant_id` ownership on tenant resources;
- composite database constraints for same-tenant relationships;
- application transactions carrying `TenantContext`;
- PostgreSQL runtime roles with `FORCE ROW LEVEL SECURITY`;
- tenant-scoped Memory retrieval before vector ranking;
- tenant-bound Run leases, ToolCall persistence, events, and audit records.
- tenant-bound ContextSnapshot and ModelCall rows with same-Run composite keys.

These controls have automated cross-tenant tests, but they do not replace caller authentication. A caller able to choose an arbitrary local Tenant header can impersonate that tenant in the current local mode.

## Runtime and Tool Gateway

Runtime providers receive frozen execution DTOs rather than ORM sessions or database credentials. A Runtime may emit normalized tool intent, but only Tool Gateway can execute a registered tool.

Tool Gateway checks:

- active Run lease and Worker ownership;
- exact tool name and version;
- the intersection of Tenant and immutable AgentVersion policies;
- input/output JSON Schema;
- idempotency, timeout, retry, cancellation, and output limits;
- Secret references and recursive redaction;
- ToolCall, Event, and Audit persistence.

Runtime adapters must not expose their native terminal, browser, file, network, or delegation tools as a bypass.

Hermes is absent from the default provider registry and Compose deployment. The optional `hermes` profile uses a pinned image, isolated state volume, per-Run home, and Nico MCP only. Operators must stop the default unpartitioned Worker before starting the Hermes Worker. Missing, disabled, or incompatible Hermes fails closed; provider resolution never falls back to Nico Native.

## Model Gateway

Nico Native reaches models only through Model Gateway. Endpoint revisions store
logical credential references rather than values. The OpenAI-compatible provider
resolves and validates all endpoint addresses, rejects non-public addresses by
default, pins the validated address for the connection, preserves Host/SNI for
TLS verification, disables redirects and proxy-environment inheritance, bounds
response bytes, and recursively redacts resolved credentials from provider errors.

Private or plain-HTTP model hosts require an explicit deployment allowlist; the
Compose fake model is enabled only through that mechanism. Hard Redis rate limits
fail closed when Redis is unavailable, while a configured soft mode may degrade.
Model endpoint administration is disabled by default outside development/test
unless `NICO_MODEL_ENDPOINT_WRITES_ENABLED` is explicitly enabled.

## Python sandbox and Docker Socket

Python runs in a one-shot container configured with no network, a read-only root filesystem, a non-root user, dropped Linux capabilities, `no-new-privileges`, PID/CPU/memory/time/output limits, and forced cleanup.

The `sandbox-runner` service mounts `/var/run/docker.sock`. Access to that socket is effectively host-sensitive even though created containers are restricted. Keep the Runner on an isolated internal network, restrict who can modify its image or token, and do not treat the sandbox as protection against a compromised Runner service.

## Secret handling

- Tool policies contain logical Secret references, not values.
- Tool Gateway resolves values immediately before authorized execution.
- sensitive keys and known values are recursively redacted from errors and persisted tool output;
- Hermes receives a per-Run `0600` configuration and a short-lived MCP broker credential;
- application logs and API responses must not contain raw Secret values.
- guided Provider setup sends a newly entered Key only over stdin to the attested
  installed `nico-service`; it never places the Key in CLI arguments, profile TOML,
  HTTP requests to Nico, probe rows, or the recovery journal;
- the installed `model-secrets.env` is owner-readable and mounted only into the
  Native Worker. A short maintenance lease blocks new Runs while that Worker is
  replaced; API, Web, Hermes, and Hermes contract workers do not inherit the file;
- Runtime providers receive only narrow Artifact/Coordination handlers and never
  receive database sessions, MinIO credentials, object keys, or lease tokens.

The Console Run Inspector is read-only. It recursively removes fields associated with credentials, authorization, lease/MCP tokens, provider state, and hidden reasoning; user/model text is rendered as React text rather than raw HTML. This is defense in depth, not a substitute for authenticated API access or server-side redaction.

Artifact objects remain private in MinIO. PostgreSQL metadata and
`SharedArtifactLink` are authoritative for access; download revalidates Run
ownership or an active link, verifies SHA-256 and size, and never returns a raw
object URL. Artifacts are untrusted data and are not executed automatically.

Conversation attachments use the same private content-addressed bucket but a
short-lived tenant/Conversation-scoped staging record. The CLI sends bytes and
metadata only; the API never accepts or opens a client filesystem path. Turn
creation consumes staged rows once into Run-owned Artifact metadata. Count,
single-file, cumulative-size, TTL and text-excerpt limits prevent attachment
staging from bypassing context and storage budgets.

The Compose `.env` file still contains infrastructure credentials. Protect it with filesystem permissions, rotate all local defaults on shared hosts, and use a real secret manager in any future production deployment.

Docker-privileged local users remain trusted: anyone who controls the daemon or
the installed Nico directory can inspect containers or replace binaries. The
stdin/permission design prevents accidental exposure and untrusted path substitution;
it is not a security boundary against the host administrator.

## Memory and Skill publication

Run reflection produces Candidate/Draft records only. Publication requires deterministic validation and an independent Approval bound to the same content hash. The requester cannot approve its own candidate. Published versions remain immutable, and active pointers or deployments provide rollback.

This process reduces accidental memory poisoning; it is not a guarantee that approved content is correct. Operators remain responsible for reviewer identity and policy, which are not formally authenticated in the current alpha.

## Not yet provided

- formal identity and role-based authorization;
- public-network deployment guidance;
- Kubernetes manifests and NetworkPolicies;
- rate limits and abuse controls;
- managed encryption keys or a secret manager integration;
- automated backup, restore, and disaster recovery;
- vulnerability scanning or signed release artifacts;
- production monitoring, alerting, and performance baselines;
- complete Artifact preview, malware scanning, retention automation, or public sharing.

Report vulnerabilities according to [../SECURITY.md](../SECURITY.md).
