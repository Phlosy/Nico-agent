# ADR-0017: Personal conversations and managed Project sessions share one execution model

- Status: Accepted
- Date: 2026-07-21

## Context

Requiring every `nico chat` caller to select both a Project and an Agent exposes an internal
foreign-key boundary when the user only wants an independent conversation. At the same time,
a multi-Agent Project needs explicit membership, a replaceable coordinator, stable member
workspaces, durable supervision and safe operator intervention. Treating all of these as one
ever-growing Conversation would mix product roles with execution state and weaken the existing
AgentVersion and RuntimeSession boundaries.

## Decision

Nico exposes two user-facing modes over the same Conversation → ConversationTurn → Task → Run
→ RuntimeSession execution chain.

An independent Session uses `Project.kind=personal` and `owner_actor_id`. The service resolves
one hidden Personal Project per Tenant/actor, so existing non-null Project foreign keys, Memory,
Artifact and audit boundaries remain intact. Personal Projects are hidden from ordinary Project
lists and do not create ProjectMember records.

A collaborative Project uses `Project.kind=shared`. ProjectMember is the Tenant-scoped
Project↔Agent relationship and carries the `lead` or `member` role. A database partial unique
constraint permits exactly one active Lead. The Lead is replaceable and does not own the
Project lifecycle.

Each active ProjectMember has one stable ProjectSession. The Session groups the member's
Conversation and autonomous Task/Run history. When the Agent publishes a new version, the next
open rotates only the current Conversation pointer; old Conversations and exact AgentVersion
bindings remain readable. ProjectSession never executes a model and never replaces the
one-RuntimeSession-per-Run invariant.

Project supervision is represented by durable ProjectSupervisionCycle rows claimed through
PostgreSQL leases. Each cycle materializes an ordinary bounded Lead Task/Run. Structured facts
come from database records; model output may add a bounded narrative but is not authoritative.

Local run guidance is a persistent RunIntervention bound to ProjectSession, Run and expected
revision. Nico Native ReAct/Plan consumes it once at a safe model boundary as untrusted data.
It cannot alter frozen tools, credentials, budgets, membership or coordination targets. Scope,
priority and cross-Agent dependency changes are routed to a new Lead ConversationTurn.

The Project timeline is a bounded Event-based projection with links to authoritative resources.
It exposes plans, reason summaries, steps, tools, Artifacts, tests, blockers and outcomes, never
raw private model reasoning.

## Consequences

- Bare `nico chat` no longer requires Project knowledge, while existing explicit Project/Agent
  calls remain compatible.
- Project membership becomes an execution authorization check for new shared-Project work and
  delegation; Tenant context alone is insufficient.
- Pausing/removing a member or archiving a Project stops future writes without deleting history.
- CLI disconnects and Worker restarts cannot lose supervision or guidance because PostgreSQL is
  authoritative.
- The current actor header remains a trusted-development context, not human authentication or
  RBAC. Organization membership, invitations and SSO remain future work.

## Rejected alternatives

- Nullable `project_id` for personal chat: duplicates execution, Artifact and knowledge
  isolation paths and weakens existing foreign-key invariants.
- One Conversation for an entire Project: conflates user dialog, member workspaces and runtime
  state, and cannot safely preserve frozen AgentVersion history.
- Project owned by its Lead Agent: makes coordinator replacement destructive to project identity
  and history.
- In-process cadence timers or CLI-owned guidance: not recoverable and not authoritative across
  Worker restarts.
- Exposing model chain-of-thought as a work log: unnecessary for auditability and unsafe; Nico
  exposes persisted actions, evidence and bounded reason summaries instead.
