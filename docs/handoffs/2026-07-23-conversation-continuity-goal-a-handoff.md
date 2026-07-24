# Conversation Continuity Goal A Handoff

Date: 2026-07-23  
Unit: U1 / CC-A executable model-request audit  
Status: Verified  
Next dependency-ready unit: U2 / CC-B message fidelity (not started)

## Delivered evidence

- Added a hermetic unit characterization for the reported incomplete timestamp
  follow-up, typed source markers, final rendered roles, current-input
  occurrence count, and clarification-like text completion.
- Added a PostgreSQL integration characterization that drives Conversation
  creation, two Turns, Runtime preparation, Context selection, Native context
  construction, capture Model Provider, ModelCall, ContextSnapshot, Run,
  RuntimeSession, and Conversation projection.
- Published the evidence-backed root-cause audit at
  `docs/audits/2026-07-23-conversation-continuity-runtime-audit.md`.
- Added the focused verifier
  `scripts/verify-conversation-continuity-goal-a.sh`.
- No production Runtime source, migration, API, or CLI behavior was changed.

## Confirmed causal chain

1. The prior completed Turn is selected correctly, but
   `select_conversation_context` represents its user and assistant values as a
   `recent_conversation_turns` object in `untrusted_context`.
2. Runtime service appends that object to the prepared seed.
3. Native context construction serializes the complete task and the complete
   seed into one `user` message. The previous assistant answer is therefore
   present only as nested JSON, not as an `assistant` message.
4. Conversation task creation stores the current message in `Task.input`, and
   Runtime preparation also copies all of `Task.input` into the `task:input`
   source. Native context serializes both copies, so the reported incomplete
   input occurs twice.
5. Direct execution treats any non-empty response without Tool calls as final.
   The capture provider returned a clarification-like sentence; the Run,
   RuntimeSession, ModelCall, and Conversation Turn all completed after one
   model call, without waiting or correction.

## Redacted observed contract

- Captured message roles: `system`, `user`.
- Previous Turn: selected and present; previous assistant role: absent.
- Current incomplete input occurrence count: 2.
- Conversation and task source labels: present.
- Previous Turn selection: 1 selected, 0 omitted.
- Native truncation: none.
- Persisted snapshot messages and recomputed content hash: equal.
- A credential locator used by the fixture did not enter the snapshot,
  ModelCall request projection, audit, or evidence.

## Non-causes

- Conversation persistence and selection did not lose the prior Turn.
- Context truncation did not fire for the fixture.
- ContextSnapshot replay data did not drift from the captured request.
- The symptom does not require a provider-specific adapter.
- Run lifecycle and Conversation terminal projection remained internally
  consistent.

## Verification

- Ruff lint: passed for both new test files.
- Ruff format check: passed for both new test files.
- Focused unit tests: 2 passed.
- Fresh migration, downgrade/reapply, and full dependency integration suite:
  158 passed.
- Documentation checks, shell syntax, diff hygiene, production-source boundary,
  and evidence Secret scan: passed.
- Focused Goal verifier with `RUN_INTEGRATION=1`: passed.

Final evidence:
`artifacts/goals/conversation-continuity-a/20260723T162052Z/`.

## Remaining work and boundary

- U2 should change the captured message contract so bounded previous Turn
  content retains chronological `user` / `assistant` roles and the current
  message occurs once.
- U3 and later units own continuity Prompt policy, AgentAction, durable user
  input, Clarification Gate, Completion Gate, and provider evaluation.
- No parent Runtime Hardening unit is projected complete by this audit-only
  Goal.
- U1 stops here. U2 is dependency-ready but was not implemented or scaffolded.
