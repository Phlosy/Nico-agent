# Error records

- The first Ruff format check reported both new test files required mechanical
  formatting. Ruff formatted them; lint and the focused unit tests then passed.
- The preliminary full integration run passed all 158 tests. The final focused
  verifier's first run then failed only the new audit test because it assumed a
  top-level `checkpoint.completed` flag. The actual Direct checkpoint contract
  records completion as `checkpoint.loop_state = completed`; the assertion was
  corrected to that documented field and the suite was rerun.
- No functional test failure or discarded Runtime implementation attempt
  occurred. U1 intentionally contains no production fix.
