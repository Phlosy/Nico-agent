# Conversation Continuity Goal D Handoff

Date: 2026-07-23  
Unit: U4 / CC-D provider-neutral AgentAction contract and parser  
Status: Verified  
Next dependency-ready unit: U5 / CC-E durable AgentAction facts

## Implemented

- Added immutable schema v1 `FinalAction`, `ToolCallAction`,
  `AskUserAction`, `IntentResolution`, completion, and batch contracts.
- Added bounded question, reason, content, candidates, confidence, ambiguity,
  risk, missing-information, safe-partial-answer, and Tool argument fields.
- Platform `action_id` is a deterministic canonical Hash. Provider Tool call
  IDs remain separately preserved; equivalent Provider facts yield the same
  platform action identity without erasing their original call IDs.
- Added a pure parser for strict JSON final/ask envelopes, ordinary Provider
  Tool calls, and explicitly marked legacy plain-text finals.
- Added stable structural failures for empty/malformed envelopes, unsupported
  kinds, final/effect mixtures, duplicate Provider call IDs, invalid
  confidence/completion metadata, and oversized fields.
- Added capability-filtered non-Tool JSON Schema/`response_format` builders.
  Tool calls remain on each Provider's native Tool interface.
- `ModelResponse` now records whether a response came from a structured-output
  request. Structured and plain JSON parse to equivalent Actions.
- Existing Direct/ReAct/Plan dispatch remains authoritative and untouched by
  the new parser.

## Modified files

- Production:
  `backend/src/nico_agent/runtime/actions.py`,
  `backend/src/nico_agent/runtime/native/action_parser.py`,
  `backend/src/nico_agent/models/contracts.py`,
  `backend/src/nico_agent/models/gateway.py`,
  `backend/src/nico_agent/runtime/contracts.py`,
  `backend/src/nico_agent/runtime/__init__.py`, and the response-format marker
  in `backend/src/nico_agent/runtime/native/loop.py`.
- Tests:
  `backend/tests/unit/test_agent_actions.py`,
  `backend/tests/unit/test_model_provider_adapters.py`,
  `backend/tests/unit/test_native_direct_runtime.py`, and
  `backend/tests/unit/test_native_react_loop.py`.
- Operations:
  `docs/progress/goal-status.md`,
  `scripts/verify-conversation-continuity-goal-d.sh`, this handoff, and the
  Goal D evidence bundle.

## Verification

- Focused Action, adapter, and Native compatibility tests: 66 passed.
- Full backend unit suite: 721 passed.
- Fresh PostgreSQL migration, downgrade/reapply, and dependency integration
  suite: 158 passed.
- Ruff, format, docs, shell syntax, diff hygiene, no-migration/no-live-dispatch
  guards, Secret scan, and evidence manifest: passed.
- Final evidence:
  `artifacts/goals/conversation-continuity-d/20260723T165848Z/`.

## Problems and limitations

- AgentAction batches are not persisted or dispatched in U4. Parent RH-B1 is
  still open until U5 completes durable facts and repair relations.
- `ask_user` exists only as an in-memory contract and is absent from the live
  response schema and dispatcher.
- Legacy plain text is intentionally labeled compatibility input and retains
  the old final semantics through U9.
- No database migration or credentialed provider inference was performed.

## Recommendation for U5

- Allocate the next migration from live head `20260723_0029` and re-check that
  it has not advanced before creating the file.
- Persist a complete immutable Action batch before any dispatch and keep Tool,
  user-input, and repair payload authorities separate.
- Make replay idempotent across ModelCall terminal/Action commit crashes and
  keep batch ordering plus platform/provider identities.
- Do not move live dispatch in U5; U6 owns compatibility activation.
