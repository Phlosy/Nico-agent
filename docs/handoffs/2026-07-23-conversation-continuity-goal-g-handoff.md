# Conversation Continuity Goal G Handoff

Date: 2026-07-23  
Unit: U7 / CC-G durable UserInputRequest backend  
Status: Verified  
Next dependency-ready unit: U8 / CC-H UserInput API and CLI interaction

## Implemented

- Added migration `0031` and the tenant-scoped `UserInputRequest` aggregate,
  bound by composite foreign keys to one Run, RuntimeSession, Action batch, and
  dispatched AskUserAction.
- Persisted bounded question, reason, Draft 2020-12 input schema, redacted
  projection, request Hash, expiry, status, revision, and stable wake identity.
  The protected answer payload is stored separately with only its Hash and
  reference exposed through ordinary projections.
- Added Native `user_input` capability and a lease-bound handler. Request
  creation saves the pre-action checkpoint, enters
  `waiting_for_user_input`, temporarily preserves the handshake lease, and
  returns the same request for an identical retry.
- User-input suspension persists checkpoint, trajectory, and usage before
  releasing the Worker lease. Unlike approval suspension, the Action remains
  `dispatched`; the durable request is already the authoritative effect.
- Answer acceptance locks the Run first, validates JSON Schema, expected
  revision and idempotency, stores exactly one protected answer, completes the
  same Action, advances the batch cursor, and wakes the Run once.
- Recovery resolves the protected answer as an internal
  `trust=untrusted` Observation. Event, Audit, Action and public request
  projections contain IDs, status and Hashes but no answer content.
- Added expiry and answered-but-unwoken reconciliation under the
  least-privilege claimer role. A terminal-Run trigger cancels pending requests
  and blocks late answers from resurrecting a Run.
- Enabled guarded lifecycle transitions for the dedicated UserInput service
  while keeping the generic control-plane transition endpoint unable to enter
  `waiting_for_user_input`.
- Kept `ask_user` absent from the live model Schema. U7 is backend-only; no
  user-input API or CLI interaction was added.

## Modified files

- User-input domain:
  `backend/src/nico_agent/user_inputs/__init__.py`,
  `backend/src/nico_agent/user_inputs/contracts.py`, and
  `backend/src/nico_agent/user_inputs/service.py`.
- Persistence:
  `backend/migrations/versions/20260723_0031_user_input_requests.py`,
  `backend/src/nico_agent/domain/models.py`,
  `backend/src/nico_agent/domain/states.py`, and
  `backend/src/nico_agent/database.py`.
- Runtime lifecycle and recovery:
  `backend/src/nico_agent/runtime/contracts.py`,
  `backend/src/nico_agent/runtime/executor.py`,
  `backend/src/nico_agent/runtime/lifecycle.py`,
  `backend/src/nico_agent/runtime/service.py`,
  `backend/src/nico_agent/runtime/native/action_dispatcher.py`,
  `backend/src/nico_agent/runtime/native/checkpoint.py`, and
  `backend/src/nico_agent/runtime/native/provider.py`.
- Control-plane guard:
  `backend/src/nico_agent/control_plane.py`.
- Tests:
  `backend/tests/unit/test_action_dispatcher.py`,
  `backend/tests/unit/test_runtime_lifecycle.py`,
  `backend/tests/unit/test_user_input_requests.py`,
  `backend/tests/integration/test_infrastructure.py`,
  `backend/tests/integration/test_runtime_lifecycle.py`, and
  `backend/tests/integration/test_user_input_runtime.py`.
- Documentation/operations:
  `docs/runtime.md`, `docs/state-machines.md`,
  `docs/progress/goal-status.md`,
  `scripts/verify-conversation-continuity-goal-g.sh`, and this handoff.

## Verification

- Focused UserInput/dispatcher/lifecycle suite: passed.
- Full backend unit suite: 742 passed.
- Frontend component suite/build: 11 passed and production build passed.
- Fresh PostgreSQL migration chain, downgrade to `0024`, reapply to `0031`,
  FORCE RLS/role checks, and complete dependency integration suite: 169 passed.
- String, choice, structured, invalid, expired, cross-tenant, duplicate,
  conflicting replay, cancellation, request retry, protected-answer,
  high-risk no-Tool-effect, and answered-but-unwoken scenarios passed.
- Ruff, format, docs, shell syntax, diff hygiene, Secret scan, and evidence
  manifest passed.
- Final evidence:
  `artifacts/goals/conversation-continuity-g/20260723T180458Z/`.

## Problems and limitations

- The first migration run exposed SQLAlchemy treating a colon-prefixed SQL
  string fragment as a bind parameter. Migration `0031` rolled back
  transactionally; the fragment was replaced with unambiguous SQL and the full
  upgrade/downgrade/reapply proof passed.
- The first complete integration run exposed two stale migration-head
  assertions, a generic control-plane bypass into the newly executable wait,
  and a test query that was not scoped by Run. Those defects were corrected;
  the apparent Tool Gateway failures were cascading claims of a pending test
  fixture left by the earlier assertion failure, and disappeared on a fresh
  database.
- `ask_user` remains deliberately absent from the live Action Schema until U9
  installs the deterministic Clarification Gate. U8 may exercise the handler
  only with explicit Action fixtures.
- Public list/get/respond routes and CLI arbitration are not part of U7; U8
  owns those interfaces.
- Protected answers currently rely on PostgreSQL/RLS and projection
  separation, not application-level field encryption. Broader encryption/key
  management is outside this plan.
- Credentialed external model behavior was not tested or claimed.

## Recommendation for U8

- Read this handoff plus U8 only. Treat `UserInputService.get/answer`,
  `UserInputRequestRead`, expected revision, and idempotency as the backend
  contract; do not redesign the lifecycle.
- Add tenant-scoped list/get/respond API surfaces without returning
  `answer_payload`.
- Make CLI Agent-question mode visibly and behaviorally distinct from Tool
  approval, queued Conversation Turn, and ordinary composer input.
- Restore a pending question after CLI/Worker restart and drive the same answer
  endpoint from both interactive and explicit commands.
- Keep the model-side `ask_user` Schema disabled until U9.
