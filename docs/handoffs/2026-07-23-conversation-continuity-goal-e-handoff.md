# Conversation Continuity Goal E Handoff

Date: 2026-07-23  
Unit: U5 / CC-E durable AgentAction facts and repair relations  
Status: Verified  
Next dependency-ready unit: U6 / CC-F compatibility Action dispatch

## Implemented

- Added linear migration `20260723_0030` with tenant-scoped
  `agent_action_batches`, `agent_actions`, and `agent_action_repairs`.
- A complete ordered batch now binds one completed ModelCall to its
  RuntimeSession, ContextSnapshot, RunStep, and Run before any existing
  final/Tool/Artifact/Delegation handler runs.
- Multi-Action persistence is transactional and idempotent per ModelCall.
  Batch/source/intent identity is immutable; the dispatch cursor can only move
  forward and terminal outcomes cannot be rewritten.
- Persisted Action rows retain deterministic platform identity, ordinal,
  Provider call ID, Tool name, completion metadata, and bounded redacted Intent
  facts. Raw final content, Tool arguments, AskUser question/reason, Prompt, and
  credentials remain outside the Action projection; only their Hashes are
  stored.
- Added append-only bounded repair relations, including an exactly-once
  `post_model_call_commit` relation.
- Recovery detects a completed ModelCall that declared Action persistence but
  has no batch, validates the original request Hash, rebuilds the response,
  persists the same deterministic batch, and continues without another
  Provider call, ModelCall, usage, or cost.
- Added the tenant-scoped
  `GET /api/v1/runs/{run_id}/agent-actions` safe read projection.
- Added `last_action_batch_key` to Direct/ReAct/Plan checkpoints while leaving
  existing handlers authoritative through U5.
- Final, multi-Tool, and AskUser facts, retry identity, atomic rollback, RLS,
  immutable intent, safe API output, migration reapply, and crash recovery are
  covered by PostgreSQL tests.

## Modified files

- Migration/domain:
  `backend/migrations/versions/20260723_0030_agent_actions.py`,
  `backend/src/nico_agent/domain/models.py`, and
  `backend/src/nico_agent/domain/states.py`.
- Runtime:
  `backend/src/nico_agent/runtime/contracts.py`,
  `backend/src/nico_agent/runtime/native/action_parser.py`,
  `backend/src/nico_agent/runtime/native/checkpoint.py`,
  `backend/src/nico_agent/runtime/native/loop.py`, and
  `backend/src/nico_agent/runtime/service.py`.
- Read API:
  `backend/src/nico_agent/model_api.py` and
  `backend/src/nico_agent/model_api_schemas.py`.
- Tests:
  `backend/tests/unit/test_agent_actions.py`,
  `backend/tests/unit/test_native_direct_runtime.py`, and
  `backend/tests/integration/test_agent_action_persistence.py`.
- Documentation/operations:
  `docs/domain-model.md`, `docs/runtime.md`,
  `docs/progress/goal-status.md`,
  `scripts/verify-conversation-continuity-goal-e.sh`, this handoff, and the
  Goal E evidence bundle.

## Verification

- Focused Action and Native compatibility tests: 62 passed.
- Full backend unit suite: 724 passed.
- Fresh PostgreSQL migration chain, downgrade/reapply, and dependency
  integration suite: 161 passed.
- Local workflow, Ruff, format, frontend tests/build, docs, shell syntax, diff
  hygiene, FORCE RLS/immutability checks, Secret scan, and evidence manifest:
  passed.
- Parent RH-B1 is now Verified from the combined valid CC-D and CC-E evidence.
- Final evidence:
  `artifacts/goals/conversation-continuity-e/20260723T171841Z/`.

## Problems and limitations

- The persisted dispatch cursor intentionally remains zero in U5. U6 owns
  moving final, Tool, Artifact, and Delegation effects behind one dispatcher
  and advancing outcomes/cursors.
- `ask_user` can be parsed and persisted but remains unadvertised and
  non-executable; U7-U9 own its durable wait lifecycle, interaction, and Gate.
- Repair relation kinds for clarification and semantic-final correction are
  reserved but are not emitted before their bounded correction loops exist.
- Legacy plain text and structured Plan-step output use shadow compatibility
  normalization so current observable handler behavior does not change.
- Credentialed external model quality was not tested or claimed; deterministic
  fake-provider and real PostgreSQL evidence are sufficient for this protocol
  and persistence unit.

## Recommendation for U6

- Read this handoff plus U6 only; treat the persisted batch and cursor as the
  sole new dispatch input.
- Move one existing handler family at a time behind a narrow dispatcher and
  advance the cursor only after its authoritative domain outcome commits.
- Preserve Tool Gateway, Artifact, and Coordination ownership; never replay an
  uncertain or already committed effect.
- Enable only the live final envelope in shadow mode. Keep `ask_user` absent
  from the advertised Schema until its complete U7-U9 path exists.
- Retain the crash-gap repair path and verify resume begins at the first
  unresolved ordinal without creating a second ModelCall or billing record.
