# Conversation Continuity Goal B Handoff

Date: 2026-07-23  
Unit: U2 / CC-B role-preserving Conversation messages and input deduplication  
Status: Verified  
Next dependency-ready unit: U3 / CC-C continuity Prompt policy

## Implemented

- Added one shared immutable `ConversationContextMessage` contract with role,
  bounded content, Turn source reference, source kind, trust classification,
  sequence, content Hash, and truncation state.
- Conversation context selection now freezes schema v2, projects completed
  Turns into chronological `user` / `assistant` messages, and keeps summaries
  and Artifact references in labeled untrusted source buckets.
- Structured assistant output uses deterministic text extraction for known
  shapes and canonical JSON fallback for unsupported shapes. Projection
  truncation never mutates the persisted ConversationTurn.
- Runtime merges schema v2 selections into ContextSeed schema v3, removes the
  duplicate current message from the `task:input` source, and retains
  Conversation/Turn metadata for audit.
- Native Direct and phase contexts render system constraints, non-Conversation
  envelope, historical role messages, current user input, then current-Run
  assistant/tool history.
- ContextSnapshot schema v2 records the new message contract. Frozen schema v1
  selections retain legacy rendering so recovery does not change an existing
  content Hash.

## Modified files

- Production:
  `backend/src/nico_agent/domain/context.py`,
  `backend/src/nico_agent/conversations/contracts.py`,
  `backend/src/nico_agent/conversations/context.py`,
  `backend/src/nico_agent/runtime/contracts.py`,
  `backend/src/nico_agent/runtime/service.py`, and
  `backend/src/nico_agent/runtime/native/context.py`.
- Tests:
  `backend/tests/unit/test_conversation_context.py`,
  `backend/tests/unit/test_conversation_model_messages.py`,
  `backend/tests/unit/test_runtime_preparation.py`,
  `backend/tests/integration/test_conversation_model_request_audit.py`, and
  `backend/tests/integration/test_native_runtime_persistence.py`.
- Operations and documentation:
  `docs/runtime.md`, `docs/progress/goal-status.md`,
  `scripts/verify-conversation-continuity-goal-b.sh`, this handoff, and the
  Goal B evidence bundle.

## Verification

- Focused Conversation/context and Native-mode unit tests: passed.
- Full backend unit suite: 696 passed.
- Fresh PostgreSQL migration, downgrade/reapply, and dependency integration
  suite: 158 passed.
- Ruff, format, documentation, shell syntax, diff hygiene, evidence Secret
  scan, and manifest validation: passed.
- Final evidence:
  `artifacts/goals/conversation-continuity-b/20260723T164205Z/`.

## Problems and limitations

- U2 deliberately does not change the existing rule that a non-empty,
  no-Tool model response completes as final. U3 owns Prompt policy; U4-U10 own
  structured Action, durable user input, clarification, and completion gates.
- A historical schema v1 frozen context remains flattened during recovery.
  This is an intentional replay-compatibility boundary, not the contract for
  newly claimed Runs.
- No database migration was required: the existing JSON snapshot and manifest
  fields carry the versioned projection.
- Credentialed provider behavior is not claimed by this Unit; U11 owns
  provider-neutral evaluation and optional external inference evidence.

## Recommendation for U3

- Build one shared continuity policy used by Direct and every phase context;
  do not duplicate guidance across loops.
- Preserve the U2 message order and source/trust separation verbatim.
- Guide dominant low-risk interpretation without advertising `ask_user`;
  that action remains unavailable until the U7-U9 persistence and policy path
  exists.
- Keep high-risk confirmation rules descriptive and subordinate to Runtime
  and Tool Gateway authority.
