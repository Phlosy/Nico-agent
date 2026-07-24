# Conversation Continuity Runtime Audit

Date: 2026-07-23  
Goal: CC-A / U1  
Scope: executable request-path audit only; no production Runtime behavior changed

## Conclusion

The reported behavior has three confirmed causes in the current Native path:

1. Selected Conversation history reaches the model as JSON inside one `user`
   message, so the prior assistant response loses its `assistant` role.
2. The current Conversation input is rendered twice in that same message: once
   through `RuntimeSessionRequest.task_input` and once through the
   `task:input` item already present in `ContextSeed.untrusted_context`.
3. Direct execution classifies every non-empty response without Tool calls as
   final. A response whose content is only a clarification question therefore
   completes the Run and Conversation Turn instead of entering a user-input
   wait.

The bounded Conversation rows, source references, ContextSnapshot, and
ModelCall facts are present and internally consistent. The failure is in the
model-facing representation and completion protocol, not missing persistence.

## Executable Reproduction

`backend/tests/integration/test_conversation_model_request_audit.py` creates a
real project, AgentVersion, Conversation, two Conversation Turns, Runtime
Worker, capture Model Provider, ModelCall, and ContextSnapshot in PostgreSQL.
The first turn persists a timestamp explanation. The second turn uses the
reported truncated input shape and the provider returns a clarification-like
sentence with no Tool call.

The captured second request and persisted facts showed:

| Observation | Result |
| --- | --- |
| Final message roles | `system`, `user` |
| Prior user text selected | yes |
| Prior assistant output selected | yes, nested inside the `user` message |
| Prior assistant represented by an `assistant` message | no |
| Current input occurrences in rendered content | 2 |
| Conversation source marker | `recent_conversation_turns` |
| Task source marker | `task:input` |
| Selected previous turns | 1 |
| Omitted previous turns | 0 |
| Native tail truncation | none |
| Model calls for the affected turn | 1 |
| Final Run / RuntimeSession / Turn state | completed / completed / completed |
| User-input wait | absent |
| Correction call | absent |

The unit characterization also supplies bounded Memory, Skill, Tool, and
external-data fixtures. Their source labels remain visible, but they share the
same serialized user envelope as Conversation history.

### Bounded fixture hashes

| Fixture field | SHA-256 |
| --- | --- |
| Prior user input | `8a7cf0c968345fc2e162a2fafefb0f404558e0e955af43fe9e756883429f0c73` |
| Prior assistant output | `b5cd319c238fe48e3350d404aefc4a429cec43509d1ed00ad5ce8966626946e8` |
| Current incomplete input | `6f182ed2f4b5058eaa3df0064fc107cd61ef7be61d8d52f75ef75ef610a3fae3` |
| Clarification-like output | `f64f15632a5b3278e6fcc43e19f995548c49db581079b1e47369e9df9e87f3b1` |

No credential locator, credential value, API key, unrelated personal data, or
unbounded prompt body is copied into this audit or its evidence projection.

## Confirmed Causal Chain

### 1. Conversation history loses message roles

1. Conversation Turn creation stores the current text in both
   `ConversationTurn.user_input` and `Task.input.message`
   (`backend/src/nico_agent/conversations/service.py`).
2. `select_conversation_context` selects completed previous Turns, converts
   each to a `{user, assistant}` payload, and appends the list as
   `kind=recent_conversation_turns` under `untrusted_context`
   (`backend/src/nico_agent/conversations/context.py`).
3. Runtime preparation appends that Conversation `untrusted_context` to the
   existing knowledge seed (`backend/src/nico_agent/runtime/service.py`).
4. `build_native_context` serializes the complete task, trusted context, and
   untrusted context into one JSON payload and places it in one `user`
   `ModelMessage` (`backend/src/nico_agent/runtime/native/context.py`).
5. The capture provider consequently receives only `system` and `user` roles.
   The prior assistant text is present but has no conversational role.

### 2. Current input is duplicated

1. The Conversation service writes the current text to
   `Task.input["message"]`.
2. `RuntimePreparationService._context_seed` copies the complete `Task.input`
   into the `task:input` untrusted source.
3. `build_native_context` independently writes the same complete input under
   `user_payload.task.input` while also serializing every untrusted source.
4. The captured current input therefore occurs exactly twice. The duplicated
   incomplete phrase receives more prompt weight while using additional
   context budget.

### 3. Clarification-like text is completed as final

1. The provider-neutral response contains non-empty text and no Tool calls.
2. Direct execution records the decision as `final`, marks its checkpoint
   complete, and rejects only Tool calls or empty text
   (`backend/src/nico_agent/runtime/native/loop.py`).
3. It emits `RUN_COMPLETED` with the raw text as output.
4. PostgreSQL projects that terminal result to the Conversation Turn. There is
   no AgentAction, UserInputRequest, waiting state, or correction relation in
   this path.

## Secondary Factors

- Stable Native system guidance does not currently describe typo recovery,
  omitted referents, dominant low-risk intent, partial answers, or the
  distinction between incomplete syntax and ambiguous intent. This can affect
  provider behavior, but the hermetic audit does not claim a provider-quality
  causal effect.
- Flattening every observed source into one user envelope weakens the
  Conversation structure even though source and trust labels remain visible.
- Repeating the incomplete current input can overemphasize its surface form.
  The audit confirms the repetition but does not claim a measured model-quality
  effect; that belongs to the later evaluation unit.

## Verified Non-Causes

- **Missing Conversation persistence:** not a cause. Both Turns persisted and
  the second snapshot selected both the prior and current Turn identifiers.
- **History selection omission:** not a cause for this fixture. One previous
  Turn was selected and none were omitted.
- **Context truncation:** not a cause for this fixture. Native truncation was
  empty.
- **Snapshot drift:** not a cause. Captured `ModelRequest.messages`,
  `ContextSnapshot.rendered_messages`, and the recomputed content hash matched.
- **Provider-specific parsing:** not required to reproduce the final-state
  problem. A provider-neutral fake response follows the same Native loop
  branch.
- **Run lifecycle corruption:** not a cause. Run, RuntimeSession, ModelCall, and
  Conversation Turn reached mutually consistent terminal states.

## Change Map for Later Goals

No file in this section was changed by U1.

| Concern | Future owner | Likely modules |
| --- | --- | --- |
| Preserve prior `user` / `assistant` roles and remove current-input duplication | CC-B / U2 | `conversations/contracts.py`, `conversations/context.py`, `runtime/contracts.py`, `runtime/service.py`, `runtime/native/context.py` |
| Add incomplete-input and continuity guidance | CC-C / U3 | `runtime/native/prompts.py`, `runtime/native/context.py` |
| Represent final, Tool, and user-input intent structurally | CC-D through CC-I / U4-U9 | AgentAction contracts/parser/persistence/dispatcher, UserInput service, Clarification Gate |
| Prevent pseudo finals from authoritatively completing | CC-J / U10 | Native Completion Gate and loop integration |

## Reusable Existing Seams

- `select_conversation_context` already provides bounded chronological selection,
  source references, truncation facts, and selected Turn identifiers.
- `ContextSeed` already separates platform instructions, trusted metadata,
  untrusted sources, Memory references, Skill references, and effect metadata.
- `ContextSnapshot.rendered_messages` and `ModelCall.request_redacted` provide
  durable, comparable model-request evidence.
- `ModelGateway` and fake providers allow provider-neutral request capture
  without external credentials.
- Runtime lifecycle and Conversation projection already make terminal state
  observable and consistent.

## Test Coverage and Limits

- `backend/tests/unit/test_conversation_model_messages.py` characterizes source
  labels, message roles, duplicate input, and implicit final classification.
- `backend/tests/integration/test_conversation_model_request_audit.py` proves the
  same request and completion shape through PostgreSQL and the real Worker path.
- The full dependency integration suite passed with the new test.
- U1 does not test corrected role behavior, `ask_user`, clarification policy, or
  semantic completion because those are explicitly owned by later independent
  Goals.
- No credentialed provider inference was run or claimed. Provider-quality
  comparison remains owned by CC-K / U11.
