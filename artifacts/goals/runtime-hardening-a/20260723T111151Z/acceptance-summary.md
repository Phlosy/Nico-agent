# Runtime Hardening Goal A acceptance summary

- One caller-transaction-aware Run lifecycle authority: passed.
- Source, revision, terminal and Worker lease guards: passed.
- One lifecycle Event and Audit fact per committed transition: passed.
- Python and PostgreSQL transition/claimability parity: passed.
- `waiting_for_user_input` remains schema/read-only and cannot be entered in U1:
  passed.
- Approval decision-before-suspension preserves the recovery checkpoint and then
  releases the transient handshake lease: passed.
- Raw lifecycle SQL execution is denied to `nico_worker_claimer`; fixed claim and
  reconcile ports remain executable: passed.
- Existing lifecycle Event `payload.status` compatibility: passed.
- Immutable migration 0026 blob and additive migration 0027: passed.
- Fresh migration plus 0027 downgrade/reapply rehearsal: passed.
- Full backend unit, frontend test/build and dependency integration suites:
  passed.
- Previous application queue, approval and Coordination paths on schema 0027:
  passed.
- Documentation checks and formal review resolution: passed.
- U2 was not entered.
