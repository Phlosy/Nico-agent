# Runtime Hardening Goal A Handoff

Date: 2026-07-23
Unit: U1 / RH-A lifecycle authority
Status: Verified
Next dependency-ready unit: U2 / RH-B1 (not started in this Goal)

## Delivered behavior

- `RunLifecycleAuthority` is the transaction-aware application authority for
  Runtime-controlled Run transitions. It accepts the caller's AsyncSession,
  TenantContext and already locked Run and never begins or commits a nested
  transaction.
- Successful transitions validate source, optional revision and lease owner/token,
  then atomically update status, lifecycle reason/metadata/revision, timestamps,
  lease claimability, one Run Event and one AuditRecord.
- Terminal states are immutable. Late wake and stale-owner writes fail before
  revision or Event mutation. Metadata is bounded and recursively redacts
  credential, token, raw argument and prompt-shaped fields.
- The application writers in Control Plane, Runtime service, Tool Gateway,
  Tool approval and Coordination now route Run status changes through the
  authority. An executable source inventory rejects newly introduced direct
  assignments in those files.
- `claim_next_run`, approval expiry wake and Coordination child wake remain named
  PostgreSQL ports. Migration 0027 exposes a shared SQL transition matrix and
  claimability predicate and makes both reconcilers emit lifecycle Event/Audit.
  The raw parameterized transition helper remains owner-only; the cross-tenant
  Worker claimer can execute only the fixed-purpose claim/reconcile ports.
- Existing lifecycle Event payloads retain `status` as a compatibility alias for
  the richer `source` and `target` fields.
- Existing Conversation queue head ordering, priority, abnormal-head pause,
  Tool Gateway, approval, child wake, Runtime Provider v2, frozen manifests and
  expired Tool lease recovery remain in place.

## State and recovery contract

The public RunStatus remains coarse. RuntimeLoopState carries initializing,
planning, reasoning/executing, observing/reflecting, finalizing, cancelling,
wait detail and budget exhaustion. `waiting_for_user_input` is only additive
schema/read preparation for U3; no ask-user handler or general external wait is
implemented or advertised by U1.

Approval and subagent waits release the lease and wake once through their durable
condition. During the short approval-request-to-suspension handshake, an early
decision preserves the still-live Worker lease until the checkpoint, usage and
trajectory recovery boundary is committed; suspension then releases it. A Tool
wait retains its active lease and becomes recoverably claimable only after expiry.
Cancellation is terminal and wins a concurrent or late wake.

## Data and migration

- Predecessor: `20260722_0026` (unchanged).
- Head: `20260723_0027`.
- Additive columns: nullable `runs.lifecycle_reason`, non-null
  `lifecycle_metadata = {}`, and non-null `lifecycle_revision = 0`.
- The Run status check expands with `waiting_for_user_input`; existing rows need no
  backfill and old binaries ignore all new columns/status unless a new writer
  actually enters the reserved state.
- SQL functions are least-privilege ports for `nico_worker_claimer`; a PostgreSQL
  role test proves the raw lifecycle function is denied while both reconcilers
  remain executable. No new table or RLS bypass was introduced.
- A representative 0026 Run upgraded with `{}`/`0` defaults intact. 0027→0026→0027
  rehearsal also passed in an isolated database.
- Production rollback is code-only. New reason/Event/Audit facts remain in the
  expanded schema and must be forward-fixed, not deleted by a destructive
  migration downgrade.

Constraint tightening is deliberately deferred: 0027 does not add a trigger that
would reject an older binary's direct status write during the mixed-version
window. The executable application allowlist and parity tests govern new writers
until a later contract migration can safely enforce the invariant at the table.

## Verification evidence

- Repository lint/format and backend unit suite: 665 passed.
- Frontend component suite: 11 passed; TypeScript and Vite production build
  passed.
- Fresh PostgreSQL migration, 0027 downgrade/reapply rehearsal and complete
  dependency integration suite: 152 passed.
- Previous application `b44639c4d3a3c7c9db354c5a009a8195bc9a1c59`
  against an independently migrated 0027 schema: 7 queue, approval and
  Coordination tests passed.
- Focused Goal A verifier with `RUN_INTEGRATION=1`: passed, including the invoked
  mixed-version proof, immutable 0026 blob check and 0027 object probes.
- Documentation publication checks, shell syntax and staged diff checks: passed.
- Eleven formal review lenses produced ten merged P1 candidates. Independent
  validation rejected three false positives and confirmed seven; all seven were
  fixed and covered before the final gates.

Final evidence:
`artifacts/goals/runtime-hardening-a/20260723T111151Z/`.

## Remaining risks and handoff

- The executable writer inventory is intentionally bounded to the current
  lifecycle writer files; future writer surfaces must extend the allowlist.
- Cancellation and approval transactions still merit a later deterministic
  lock-order/deadlock audit, but no U1 acceptance path is left failing.
- Constraint tightening remains deferred until mixed-version writers can be
  retired safely.
- U1 stops here. U2 is dependency-ready but was not entered, implemented or
  partially scaffolded in this Goal.
