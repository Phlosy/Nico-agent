# Conversation Continuity Goal C Handoff

Date: 2026-07-23  
Unit: U3 / CC-C incomplete-input and continuity Prompt policy  
Status: Verified  
Next dependency-ready unit: U4 / CC-D AgentAction contract and parser

## Implemented

- Added one versioned, provider-neutral Native continuity policy in
  `runtime/native/prompts.py`.
- Injected that exact policy once into Direct, ReAct, Planner, Plan Step,
  Reflection, Completion Judge, and repair system contexts through the two
  shared Native context builders.
- The policy directs the model to use recent Conversation context for typos,
  mixed language, truncation, omissions, and references; incomplete syntax
  alone is not treated as ambiguity.
- A dominant low-risk reversible interpretation is answered directly, with a
  short assumption when useful and without long low-probability option lists.
- Similar interpretations, materially different answers, or indispensable
  missing facts may produce one blocking question; safe useful content should
  be answered first.
- Ambiguous destructive, write, payment, funds, permission, security, and
  externally visible send effects require explicit confirmation.
- The policy explicitly leaves authority, approval, risk, and budgets with
  Runtime and Tool Gateway and does not advertise `ask_user`.

## Modified files

- Production:
  `backend/src/nico_agent/runtime/native/prompts.py` and
  `backend/src/nico_agent/runtime/native/context.py`.
- Tests:
  `backend/tests/unit/test_native_continuity_prompt.py`,
  `backend/tests/unit/test_runtime_preparation.py`,
  `backend/tests/unit/test_native_direct_runtime.py`,
  `backend/tests/unit/test_native_react_loop.py`, and
  `backend/tests/unit/test_native_plan_loop.py`.
- Operations and documentation:
  `docs/runtime.md`, `docs/progress/goal-status.md`,
  `scripts/verify-conversation-continuity-goal-c.sh`, this handoff, and the
  Goal C evidence bundle.

## Verification

- Focused Prompt and Native-mode tests: 48 passed.
- Full backend unit suite: 699 passed.
- Fresh PostgreSQL migration, downgrade/reapply, and dependency integration
  suite: 158 passed.
- Ruff, format, docs, shell syntax, diff hygiene, case-fixture/action scan,
  evidence Secret scan, and manifest validation: passed.
- Final evidence:
  `artifacts/goals/conversation-continuity-c/20260723T164925Z/`.

## Problems and limitations

- Prompt guidance improves model behavior but is not a deterministic policy
  gate. U9 and U10 still own clarification and semantic completion enforcement.
- The live response contract remains plain-text compatible and the existing
  final branch is unchanged; U4-U6 own Action parsing and dispatch migration.
- No `ask_user` capability is advertised because its persistence, answer, and
  wake path does not exist until U7-U9.
- No extra Intent Resolver call or credentialed provider inference was added or
  claimed.

## Recommendation for U4

- Define immutable provider-neutral Action DTOs and a pure parser without
  moving live effect dispatch.
- Preserve plain-text final as a compatibility input and parse no natural
  language by clarification keywords.
- Keep `ask_user` in the in-memory contract only; do not attach it to live
  response schemas or execute it in U4.
- Characterize structured-output capable and plain JSON endpoints without
  changing current Direct/ReAct/Plan behavior.
