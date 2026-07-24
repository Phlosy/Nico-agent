# Conversation Continuity Goal F Handoff

Date: 2026-07-23  
Unit: U6 / CC-F compatibility Action dispatch  
Status: Verified  
Next dependency-ready unit: U7 / CC-G durable UserInputRequest backend

## Implemented

- Added one provider-neutral `AgentActionDispatcher` for Native final, Tool,
  Artifact, and Delegation compatibility paths.
- Production dispatch waits until the emitted Action batch is committed by the
  runtime event projector. Each ordinal commits `dispatched` before entering an
  effect handler and advances the durable cursor only after the authoritative
  domain result commits.
- Existing Tool Gateway, Artifact Service, and Coordination Service remain the
  sole owners of their outcomes. Action rows keep only bounded outcome and
  observation references; recovery resolves authoritative DTOs from those
  domain records.
- Resume skips successful ordinals, begins at the first pending ordinal, and
  stops without executing later Actions when the current effect is
  `dispatched`/unknown, failed, or blocked.
- Tool approval suspension releases the pre-effect ordinal back to `pending`,
  preserving the existing approval and resume behavior.
- Direct, ReAct, Plan Step, and citation-repair finals now dispatch their
  normalized content. Structured and legacy final forms produce the same
  authoritative output.
- Endpoints with structured-output support receive a final-only Action schema.
  Other Native phases receive equivalent prompt guidance. `ask_user` remains
  absent from the live contract; an unsolicited structured `ask_user` fails
  with `ACTION_KIND_UNSUPPORTED` and cannot enter a wait state.
- Recovery now reuses completed Action-producing ModelCalls whether the batch
  is missing or already committed, preventing replay billing when a crash
  occurs before the next checkpoint.
- Native-only Action services are injected by the Worker; Mock and Hermes
  Provider v2 paths remain outside Action parsing and dispatch.

## Modified files

- Runtime contract and worker:
  `backend/src/nico_agent/runtime/contracts.py`,
  `backend/src/nico_agent/runtime/executor.py`, and
  `backend/src/nico_agent/runtime/tools.py`.
- Native dispatch and compatibility:
  `backend/src/nico_agent/runtime/native/action_dispatcher.py`,
  `backend/src/nico_agent/runtime/native/action_parser.py`,
  `backend/src/nico_agent/runtime/native/checkpoint.py`,
  `backend/src/nico_agent/runtime/native/context.py`,
  `backend/src/nico_agent/runtime/native/prompts.py`, and
  `backend/src/nico_agent/runtime/native/loop.py`.
- Persistence and recovery:
  `backend/src/nico_agent/runtime/service.py`.
- Tests:
  `backend/tests/unit/test_action_dispatcher.py`,
  `backend/tests/unit/test_agent_actions.py`,
  `backend/tests/unit/test_native_continuity_prompt.py`,
  `backend/tests/unit/test_native_direct_runtime.py`,
  `backend/tests/unit/test_native_react_loop.py`,
  `backend/tests/unit/test_native_plan_loop.py`,
  `backend/tests/integration/test_agent_action_dispatch.py`, and
  `backend/tests/integration/test_agent_action_persistence.py`.
- Documentation/operations:
  `docs/runtime.md`, `docs/progress/goal-status.md`,
  `scripts/verify-conversation-continuity-goal-f.sh`, and this handoff.

## Verification

- Focused Action/Native compatibility suite: 72 passed.
- Full backend unit suite: 731 passed.
- Frontend component suite/build: 11 passed and production build passed.
- Fresh PostgreSQL migration chain, downgrade/reapply, and complete dependency
  integration suite: 162 passed.
- Local workflow, Ruff, format, docs, shell syntax, diff hygiene, Native-only
  isolation, unknown-effect recovery, Secret scan, and evidence manifest:
  passed.
- Parent RH-B2 is Verified from CC-F evidence.
- Final evidence:
  `artifacts/goals/conversation-continuity-f/20260723T174317Z/`.

## Problems and limitations

- The first targeted PostgreSQL run exposed an async-generator construction in
  the Action state projection. It was replaced with an explicit awaited loop;
  the targeted test, full unit suite, and full integration suite then passed.
- A `dispatched` Action without an authoritative outcome intentionally fails
  closed as unknown. U6 does not guess whether an uncertain external effect
  occurred and never dispatches later ordinals.
- `ask_user` remains deliberately non-executable. U7 owns its durable backend,
  U8 owns CLI interaction, and U9 owns the Clarification Gate and live schema
  activation.
- U6 preserves existing Tool-call budget ownership and compatibility behavior;
  centralized budget redesign remains outside this unit.
- Credentialed external model quality was not tested or claimed.

## Recommendation for U7

- Read this handoff plus U7 only. Treat `RuntimeActionHandler` and the persisted
  Action cursor as the input boundary; do not reopen compatibility dispatch.
- Add UserInputRequest persistence and lifecycle transitions before enabling
  `ask_user` in any live schema.
- Make ask persistence, Run suspension, lease release, answer acceptance, wake,
  and resume one crash-safe idempotent chain.
- Preserve the U6 rule that every Action is committed before dispatch and no
  unknown effect is repeated.
