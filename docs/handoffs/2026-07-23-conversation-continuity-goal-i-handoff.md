# Conversation Continuity Goal I Handoff

Date: 2026-07-23  
Unit: U9 / CC-I deterministic Clarification Gate  
Status: Verified  
Next dependency-ready unit: U10 / CC-J semantic Completion Gate floor

## Implemented

- Added the provider-neutral, versioned `clarification-v1` policy. It evaluates
  only structured intent candidates, confidence margin, ambiguity, missing
  information, safe-partial capability, authoritative risk and pending
  obligations.
- Added three bounded decisions: `allow_ask_user`,
  `answer_with_assumption`, and `continue_with_partial_answer`. No policy path
  searches natural-language output for clarification phrases.
- Activated `ask_user` only when both the Clarification Gate and a durable
  UserInput handler are present. Without that handler the response schema and
  parser remain fail-closed.
- Integrated the Gate into Direct and ReAct. Necessary questions use the
  existing persisted Action/UserInput cursor and suspend; answered input
  returns only as an internal untrusted observation.
- Unnecessary questions are persisted as blocked, followed by one Tool-free
  model correction containing the model's own dominant interpreted intent and
  deterministic policy reason. Runtime never creates the domain answer.
- Added stable `CLARIFICATION_CORRECTION_EXHAUSTED` failure after a repeated
  rejected question.
- Linked every clarification correction to its source Action batch through
  `AgentActionRepair`, including lookup by stable batch Hash. ReAct schema-v2
  checkpoints retain the correction flag, source Hash and bounded observation
  so crash recovery reuses the same named call.
- Kept Plan phase-specific JSON contracts authoritative. U10 owns the shared
  semantic final floor across Direct, ReAct and Plan.

## Modified files

- Runtime policy and execution:
  `backend/src/nico_agent/runtime/clarification.py`,
  `backend/src/nico_agent/runtime/native/action_parser.py`,
  `backend/src/nico_agent/runtime/native/checkpoint.py`,
  `backend/src/nico_agent/runtime/native/context.py`,
  `backend/src/nico_agent/runtime/native/loop.py`,
  `backend/src/nico_agent/runtime/native/prompts.py`, and
  `backend/src/nico_agent/runtime/service.py`.
- Tests:
  `backend/tests/unit/test_agent_actions.py`,
  `backend/tests/unit/test_clarification_gate.py`,
  `backend/tests/unit/test_native_continuity_prompt.py`,
  `backend/tests/unit/test_native_direct_runtime.py`,
  `backend/tests/unit/test_native_react_loop.py`, and
  `backend/tests/integration/test_clarification_runtime.py`.
- Documentation/operations:
  `docs/runtime.md`, `docs/testing.md`, `docs/progress/goal-status.md`,
  `scripts/verify-conversation-continuity-goal-i.sh`, and this handoff.

## Verification

- Focused Clarification/Action/Direct/ReAct tests: 70 passed.
- Full backend unit suite: 766 passed.
- Frontend component suite/build: 11 passed and production build passed.
- Fresh PostgreSQL migration chain, downgrade to `0024`, reapply to `0031`,
  and complete dependency integration suite: 172 passed.
- Docs, Ruff, format, shell syntax, diff hygiene, Secret scan and evidence
  manifest passed.
- Final evidence:
  `artifacts/goals/conversation-continuity-i/20260723T184135Z/`.

## Problems and limitations

- The first complete integration run passed the new clarification persistence
  test but exposed that its fixture left a pending `nico_native` Run for later
  Conversation tests. The fixture is now terminal from creation; the next
  complete run passed all 172 tests.
- The Gate trusts only versioned structured model metadata and frozen Runtime
  facts. Poorly calibrated model confidence remains measurable quality risk,
  not a reason to add language-specific heuristics.
- Direct and ReAct share the live question/correction path. Plan retains its
  phase-specific output schema; U10 will apply the semantic Completion Gate
  floor to all three modes.
- Legacy plain-text final compatibility remains until U10 proves strict final
  metadata enforcement.
- Protected answers retain the U7/U8 PostgreSQL RLS and projection boundary;
  application-level field encryption remains outside this plan.
- No credentialed external model behavior is claimed.

## Recommendation for U10

- Read this handoff plus U10 only. Reuse `AgentActionRepair`, the named bounded
  correction pattern and the existing Action dispatcher.
- Evaluate persisted FinalAction intent/completion metadata before publishing
  authoritative output or completing a Run.
- Keep invalid-final correction separate from clarification correction, but
  route a corrected `ask_user` through this Gate.
- Preserve phase-specific Plan validation and Web citation repair accounting;
  do not let legacy plain text bypass the semantic floor.
