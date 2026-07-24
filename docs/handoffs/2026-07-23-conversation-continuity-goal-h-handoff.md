# Conversation Continuity Goal H Handoff

Date: 2026-07-23  
Unit: U8 / CC-H UserInput API and CLI interaction  
Status: Verified  
Next dependency-ready unit: U9 / CC-I deterministic Clarification Gate

## Implemented

- Added tenant-scoped UserInput list/get/answer API resources. Answers carry
  expected revision and `Idempotency-Key`, are validated against the request
  JSON Schema, and return stable malformed, stale, resolved, expired and
  cross-tenant behavior without exposing protected answer content.
- Added thin CLI list/get/answer methods and a shared coordinator that preserves
  string answers while decoding structured JSON answers without coercion.
- Added a distinct `Agent question` renderer and `answer ›` prompt. It is not a
  Nico final answer, Tool approval, queued Turn, or ordinary `you ›` composer.
- Interactive chat discovers pending questions during startup refresh and from
  `UserInputRequested` SSE events. The same durable request is restored after
  CLI or Worker interruption.
- Added deterministic input ownership: Agent question, Tool approval, queued
  Turn preview, then normal composer. An interrupt saves and restores the exact
  draft and cursor; a question answer calls only the UserInput endpoint and
  never `create_conversation_turn`.
- Added an explicit-action PTY fixture. The first CLI exits with a pending
  question; a restarted CLI restores and answers it; the fixture proves one
  answer, zero Turn creations, and one authoritative final render.
- Kept `ask_user` absent from the live model Schema. U9 remains the only owner
  of Clarification Gate activation.

## Modified files

- API/backend:
  `backend/src/nico_agent/api.py`,
  `backend/src/nico_agent/user_inputs/api.py`, and
  `backend/src/nico_agent/user_inputs/service.py`.
- CLI:
  `backend/src/nico_agent/cli/client.py`,
  `backend/src/nico_agent/cli/chat_session.py`,
  `backend/src/nico_agent/cli/renderers.py`, and
  `backend/src/nico_agent/cli/user_inputs.py`.
- Tests/fixtures:
  `backend/tests/unit/test_cli_client.py`,
  `backend/tests/unit/test_cli_chat.py`,
  `backend/tests/unit/test_cli_user_input.py`,
  `backend/tests/integration/test_user_input_api.py`, and
  `backend/src/nico_agent/testing/user_input_e2e_server.py`.
- Documentation/operations:
  `docs/cli.md`, `docs/runtime.md`, `docs/progress/goal-status.md`,
  `scripts/e2e-runtime-user-input.sh`,
  `scripts/verify-conversation-continuity-goal-h.sh`, and this handoff.

## Verification

- Focused CLI/API tests: 108 passed.
- Full backend unit suite: 751 passed.
- UserInput PTY restart proof: passed.
- Frontend component suite/build: 11 passed and production build passed.
- Fresh PostgreSQL migration chain, downgrade to `0024`, reapply to `0031`,
  and complete dependency integration suite: 171 passed.
- Docs, Ruff, format, shell syntax, diff hygiene, Secret scan and evidence
  manifest passed.
- Final evidence:
  `artifacts/goals/conversation-continuity-h/20260723T182154Z/`.

## Problems and limitations

- The first local targeted PostgreSQL attempt used the default localhost
  credentials and then an unmigrated development database; both were test
  environment issues, not application failures. The isolated integration
  runner owns a fresh migrated database.
- The first complete integration run exposed an invalid test setup that tried
  to mutate immutable `expires_at`. The test now creates a one-second request
  and waits for natural expiry, preserving the production immutability guard.
- The PTY proof uses an explicit Action HTTP fixture because U8 must not expose
  `ask_user` to live model output before U9. Backend suspend/wake and crash
  recovery remain covered by the real PostgreSQL U7 integration suite.
- Public request projections expose answer Hash/reference after resolution but
  never the protected answer payload.
- Application-level field encryption remains outside this plan; PostgreSQL
  RLS and projection separation are unchanged from U7.
- Credentialed external model behavior is not tested or claimed.

## Recommendation for U9

- Read this handoff plus U9 only. Reuse the existing UserInput API, CLI owner
  arbitration, handler and Action dispatcher; do not add a second question
  transport.
- Implement the deterministic Clarification Gate and bounded correction before
  adding `ask_user` to the live Action Schema.
- Activate `ask_user` only when the Gate and UserInput capability are both
  enabled in the same deployable state.
- Preserve answer recovery as an internal untrusted Observation and keep
  natural-language keyword classification out of policy.
